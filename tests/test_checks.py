"""Permission and readiness checks.

These decide who may run owner-only commands (d!execute runs arbitrary
Python) and what happens to a command that arrives before the bot has
finished registering its guilds.
"""

import asyncio
import contextlib
import types

import discord
import pytest
from discord.ext.commands import NotOwner

from config import config
from musicbot.bot import Context
from musicbot.commands.general import General
from musicbot.commands.library import Library, queue_songs
from musicbot.audiocontroller import MusicButton
from musicbot.commands.music import Music, SearchView, SongButton
from musicbot.sessions import GuildSessions
from musicbot.utils import (
    CheckError,
    chunks,
    get_audiocontroller,
    get_settings,
    owner_check,
    join_needed,
    join_voice,
    play_check,
    voice_check,
)


def make_ctx(bot, guild=None, author_id=1):
    return types.SimpleNamespace(
        bot=bot, guild=guild, author=types.SimpleNamespace(id=author_id)
    )


def make_bot(controllers=None, is_owner=False, settings=None):
    """A bot whose registry is seeded through its own interface:
    `settings` alone are bulk-loaded, and each of `controllers` is
    built by get_or_create() over stand-in settings."""
    controllers = controllers or {}
    settings = settings or {}

    async def _is_owner(_user):
        return is_owner

    async def load(guilds):
        # a vc_timeout means get_or_create() does not autojoin
        return {
            g: settings.get(g, types.SimpleNamespace(vc_timeout=True))
            for g in guilds
        }

    async def seed():
        await sessions.load_settings(settings)
        for guild in controllers:
            await sessions.get_or_create(guild)

    sessions = GuildSessions(controllers.__getitem__, load)
    asyncio.run(seed())
    return types.SimpleNamespace(sessions=sessions, is_owner=_is_owner)


class Guild:
    pass


class TestGetAudiocontroller:
    def test_returns_the_registered_controller(self):
        guild = Guild()
        bot = make_bot({guild: "controller"})
        assert get_audiocontroller(make_ctx(bot, guild)) == "controller"

    def test_unregistered_guild_raises_check_error(self):
        """on_ready registers guilds in a loop that awaits a network
        call apiece, and application commands are not gated behind it
        - so this used to be a bare KeyError with nothing sent back to
        the user."""
        bot = make_bot({Guild(): "controller"})
        with pytest.raises(CheckError) as exc:
            get_audiocontroller(make_ctx(bot, Guild()))
        assert str(exc.value) == config.BOT_NOT_READY

    def test_no_guild_raises_check_error(self):
        """Context.send can be reached with guild None outside a guild
        entirely."""
        with pytest.raises(CheckError):
            get_audiocontroller(make_ctx(make_bot(), None))


class TestGetSettings:
    """The twin of get_audiocontroller. Until on_ready has loaded a
    guild's settings, a path that finds its controller missing finds
    its settings missing too, so guarding only the controller just
    moved the KeyError one line down into play_check()."""

    def test_returns_the_registered_settings(self):
        guild = Guild()
        bot = make_bot(settings={guild: "sett"})
        assert get_settings(make_ctx(bot, guild)) == "sett"

    def test_unregistered_guild_raises_check_error(self):
        bot = make_bot(settings={Guild(): "sett"})
        with pytest.raises(CheckError) as exc:
            get_settings(make_ctx(bot, Guild()))
        assert str(exc.value) == config.BOT_NOT_READY

    def test_no_guild_raises_check_error(self):
        with pytest.raises(CheckError):
            get_settings(make_ctx(make_bot(), None))


@pytest.mark.parametrize(
    "extra_owners, author_id, is_owner, allowed",
    [
        ([], 1, True, True),  # the application owner
        ([], 1, False, False),  # nobody
        ([42], 42, False, True),  # configured extra owner
        ([42], 43, False, False),  # a different user
        # the id that used to be hardcoded in owner_check
        ([], 150861087976194048, False, False),
    ],
)
def test_owner_check(monkeypatch, extra_owners, author_id, is_owner, allowed):
    import asyncio

    monkeypatch.setattr(config, "EXTRA_OWNERS", extra_owners)
    ctx = make_ctx(make_bot(is_owner=is_owner), author_id=author_id)

    if allowed:
        assert asyncio.run(owner_check(ctx)) is True
    else:
        with pytest.raises(NotOwner):
            asyncio.run(owner_check(ctx))


class TestChunks:
    def test_splits_evenly(self):
        assert list(chunks([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]

    def test_keeps_a_short_final_chunk(self):
        assert list(chunks([1, 2, 3], 2)) == [[1, 2], [3]]

    def test_empty(self):
        assert list(chunks([], 5)) == []


# --- voice -----------------------------------------------------------
#
# The fakes below stand in for discord's voice objects, the system
# boundary. A connect (channel.connect) and a move
# (voice_client.move_to) are recorded separately, in one shared event
# log, so a test can tell which happened and in what order.


class FakeVoiceClient:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel

    async def move_to(self, channel):
        self.guild.events.append(("move", channel.name))
        if self.guild.fail is not None:
            raise self.guild.fail
        self.channel = channel


class FakeVoiceChannel:
    def __init__(self, guild, name, members=(), connect=True, speak=True):
        self.guild = guild
        self.name = name
        self.members = list(members)
        self.perms = types.SimpleNamespace(connect=connect, speak=speak)

    def permissions_for(self, member):
        assert member is self.guild.me
        return self.perms

    async def connect(self, **kwargs):
        self.guild.events.append(("connect", self.name))
        if self.guild.fail is not None:
            raise self.guild.fail
        self.guild.voice_client = FakeVoiceClient(self.guild, self)


class VoiceGuild:
    """A guild whose bot is in `bot_in` (a channel name) or out of
    voice (None). `fail` is raised by the next connect or move."""

    def __init__(self):
        self.me = types.SimpleNamespace(bot=True)
        self.voice_client = None
        self.events = []
        self.fail = None

    def channel(self, name, humans=0, bots=0, **perms):
        members = [types.SimpleNamespace(bot=False)] * humans
        members += [types.SimpleNamespace(bot=True)] * bots
        return FakeVoiceChannel(self, name, members, **perms)

    def put_bot_in(self, channel):
        channel.members.append(self.me)
        self.voice_client = FakeVoiceClient(self, channel)


COMMAND_CHANNEL = 7


def voice_ctx(
    guild,
    author_in=None,
    rule=True,
    command_channel=None,
    dj_role=None,
    admin=False,
):
    sett = types.SimpleNamespace(
        command_channel=command_channel,
        user_must_be_in_vc=rule,
        dj_role=dj_role,
        vc_timeout=True,  # get_or_create() does not autojoin
    )
    bot = make_bot({guild: "controller"}, settings={guild: sett})
    author = types.SimpleNamespace(
        id=1,
        roles=[],
        voice=(
            types.SimpleNamespace(channel=author_in) if author_in else None
        ),
    )
    channel = types.SimpleNamespace(
        id=COMMAND_CHANNEL,
        permissions_for=lambda m: types.SimpleNamespace(administrator=admin),
    )
    return types.SimpleNamespace(
        bot=bot, guild=guild, author=author, channel=channel
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """A move waits a second before returning; time is a boundary."""

    async def sleep(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", sleep)


def refusal(coro):
    with pytest.raises(CheckError) as exc:
        asyncio.run(coro)
    return str(exc.value)


class TestPlayCheck:
    """play_check only refuses: it never connects or moves."""

    def test_refuses_the_wrong_command_channel(self):
        guild = VoiceGuild()
        ctx = voice_ctx(
            guild, guild.channel("a"), command_channel=COMMAND_CHANNEL + 1
        )
        assert refusal(play_check(ctx)) == config.WRONG_CHANNEL_MESSAGE
        assert guild.events == []

    def test_refuses_a_user_not_in_voice_when_a_join_is_needed(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, None)
        assert refusal(play_check(ctx)) == config.USER_NOT_IN_VC_MESSAGE
        assert guild.events == []

    @pytest.mark.parametrize("perms", [{"connect": False}, {"speak": False}])
    def test_refuses_missing_voice_permissions_when_a_join_is_needed(
        self, perms
    ):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a", humans=1, **perms))
        assert refusal(play_check(ctx)) == config.VOICE_PERMISSIONS_MISSING
        assert guild.events == []

    def test_refuses_missing_permissions_for_a_move(self):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a"))
        ctx = voice_ctx(guild, guild.channel("b", humans=1, speak=False))
        assert refusal(play_check(ctx)) == config.VOICE_PERMISSIONS_MISSING
        assert guild.events == []

    def test_does_not_refuse_missing_permissions_without_a_join(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1, connect=False, speak=False)
        guild.put_bot_in(here)
        assert asyncio.run(play_check(voice_ctx(guild, here))) is True
        assert guild.events == []

    def test_refuses_the_voice_rule(self):
        """The bot is with someone else, and the user is not a DJ."""
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", humans=1))
        ctx = voice_ctx(guild, guild.channel("b", humans=1), dj_role=99)
        assert refusal(play_check(ctx)) == config.USER_NOT_IN_VC_MESSAGE
        assert guild.events == []

    def test_admits_without_connecting(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a", humans=1))
        assert asyncio.run(play_check(ctx)) is True
        assert guild.events == []

    def test_admits_a_bots_only_channel_without_moving(self):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b", humans=1))
        assert asyncio.run(play_check(ctx)) is True
        assert guild.events == []


class TestJoinVoice:
    def test_connects_when_the_bot_is_out_of_voice(self):
        guild = VoiceGuild()
        asyncio.run(join_voice(voice_ctx(guild, guild.channel("a"))))
        assert guild.events == [("connect", "a")]

    def test_moves_out_of_a_bots_only_channel_under_the_rule(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        asyncio.run(join_voice(voice_ctx(guild, guild.channel("b"))))
        assert guild.events == [("move", "b")]

    def test_does_not_move_out_of_a_bots_only_channel_without_the_rule(
        self,
    ):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b"), rule=False)
        asyncio.run(join_voice(ctx))
        assert guild.events == []

    def test_does_not_move_out_of_a_channel_with_people(self):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", humans=1))
        asyncio.run(join_voice(voice_ctx(guild, guild.channel("b"))))
        assert guild.events == []

    def test_does_nothing_when_already_with_the_user(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        asyncio.run(join_voice(voice_ctx(guild, here)))
        assert guild.events == []

    def test_refuses_a_user_who_left_voice(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, None)
        assert refusal(join_voice(ctx)) == config.USER_NOT_IN_VC_MESSAGE
        assert guild.events == []

    @pytest.mark.parametrize(
        "error", [asyncio.TimeoutError(), discord.ClientException("x")]
    )
    def test_a_failed_connect_is_a_check_error_and_is_logged(
        self, capsys, error
    ):
        guild = VoiceGuild()
        guild.fail = error
        ctx = voice_ctx(guild, guild.channel("a"))
        assert refusal(join_voice(ctx)) == config.VOICE_CONNECT_FAILED
        assert type(error).__name__ in capsys.readouterr().err

    def test_a_failed_move_is_a_check_error_and_is_logged(self, capsys):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        guild.fail = discord.ClientException("Not connected to voice")
        ctx = voice_ctx(guild, guild.channel("b"))
        assert refusal(join_voice(ctx)) == config.VOICE_CONNECT_FAILED
        assert guild.events == [("move", "b")]
        assert "Not connected to voice" in capsys.readouterr().err


def slash_ctx(guild, author_in, **kwargs):
    """voice_ctx as a slash invocation, through the real
    Context.defer. A defer is logged in the guild's event log, so its
    order against a connect or move shows."""
    ctx = voice_ctx(guild, author_in, **kwargs)
    deferred = []

    async def defer(ephemeral=False):
        if deferred:
            raise discord.InteractionResponded(None)
        deferred.append(ephemeral)
        guild.events.append(("defer", ephemeral))

    ctx.interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(
            defer=defer, is_done=lambda: bool(deferred)
        )
    )
    ctx.audiocontroller = types.SimpleNamespace(command_channel=None)

    async def ctx_defer(**kw):
        return await Context.defer(ctx, **kw)

    ctx.defer = ctx_defer
    return ctx


class TestMusicBeforeInvoke:
    """Music.cog_before_invoke defers a slash command just before a
    join it needs, so a slow connect cannot time the interaction
    out, and leaves every other command as it was."""

    def test_defers_publicly_before_connecting(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("connect", "a")]

    def test_defers_before_moving(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b"))
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("move", "b")]

    def test_neither_defers_nor_joins_without_a_join(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        ctx = slash_ctx(guild, here)
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []
        assert ctx.audiocontroller.command_channel is ctx

    def test_a_second_defer_in_the_body_does_not_raise(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))

        async def scenario():
            await Music(None).cog_before_invoke(ctx)
            await ctx.defer()

        asyncio.run(scenario())
        assert guild.events == [("defer", False), ("connect", "a")]


def command_of(cog, name):
    """A cog's command by its qualified name."""
    return {c.qualified_name: c for c in cog(None).walk_commands()}[name]


@pytest.mark.parametrize(
    "cog, name",
    [
        (General, "connect"),
        (General, "reset"),
        (Library, "library browse"),
        (Library, "library search"),
    ],
)
def test_commands_outside_the_music_cog_that_join_keep_joining(cog, name):
    """d!connect and d!reset join voice, and so do picks from the
    library views, whose context is the command that opened them."""
    guild = VoiceGuild()
    ctx = voice_ctx(guild, guild.channel("a"))
    ctx.command = command_of(cog, name)
    asyncio.run(join_voice(ctx))
    assert guild.events == [("connect", "a")]


def test_connect_moves_the_bot_out_of_a_bots_only_channel(no_sleep):
    guild = VoiceGuild()
    guild.put_bot_in(guild.channel("a", bots=1))
    ctx = voice_ctx(guild, guild.channel("b", humans=1))
    ctx.command = command_of(General, "connect")
    sent = []

    async def send(content, **kwargs):
        sent.append(content)

    ctx.send = send

    async def scenario():
        assert await voice_check(ctx)
        await General._connect.callback(General(None), ctx)

    asyncio.run(scenario())
    assert guild.events == [("move", "b")]
    assert sent == ["Connected."]


def test_a_failed_join_from_the_library_says_so():
    """Queueing from the library browser joins voice itself, after
    play_check; a failed connect is reported as one, not as a
    song-info error."""
    guild = VoiceGuild()
    guild.fail = asyncio.TimeoutError()
    ctx = voice_ctx(guild, guild.channel("a"))
    ctx.command = command_of(Library, "library browse")
    replies = []

    async def send_message(content, **kwargs):
        replies.append(content)

    async def edit_original_response(content):
        replies.append(content)

    interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(
            is_done=lambda: False, send_message=send_message
        ),
        edit_original_response=edit_original_response,
    )
    asyncio.run(queue_songs(ctx, interaction, [("A", "B", "c.mp3")], "browse"))
    assert replies == ["Queueing...", config.VOICE_CONNECT_FAILED]
    assert guild.events == [("connect", "a")]


# --- joining commands ------------------------------------------------
#
# Only a joining command (GLOSSARY.md) brings the bot into voice: one
# that starts playback carries the opt-in marker; every other music
# command answers without connecting or moving the bot.


def music_command(name):
    return command_of(Music, name)


JOINING = {"play", "playnext", "search", "playlist load", "restore", "prev"}


def test_the_joining_music_commands_are_exactly_those_starting_playback():
    joining = set()
    for cmd in Music(None).walk_commands():
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a"))
        ctx.command = cmd
        if join_needed(ctx):
            joining.add(cmd.qualified_name)
    assert joining == JOINING


class TestNonJoiningCommand:
    """An unmarked music command never touches voice, so while the bot
    is idle it has no voice requirement to refuse on."""

    def test_admits_a_user_outside_voice_while_idle(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, None)
        ctx.command = music_command("history")
        assert asyncio.run(play_check(ctx)) is True
        assert guild.events == []

    def test_skips_the_voice_permission_check_while_idle(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a", connect=False, speak=False))
        ctx.command = music_command("playlist list")
        assert asyncio.run(play_check(ctx)) is True

    def test_does_not_connect_while_idle(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a", humans=1))
        ctx.command = music_command("history")
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []

    def test_does_not_move_out_of_a_bots_only_channel(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b", humans=1))
        ctx.command = music_command("pause")
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []

    def test_a_playlist_subcommand_other_than_load_does_not_connect(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = music_command("playlist show")
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []


class TestJoiningCommand:
    def test_defers_and_connects_while_idle(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = music_command("play")
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("connect", "a")]

    def test_defers_and_moves_out_of_a_bots_only_channel(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b"))
        ctx.command = music_command("prev")
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("move", "b")]

    def test_playlist_load_connects(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = music_command("playlist load")
        asyncio.run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("connect", "a")]

    def test_still_refuses_a_user_outside_voice_while_idle(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, None)
        ctx.command = music_command("search")
        assert refusal(play_check(ctx)) == config.USER_NOT_IN_VC_MESSAGE


def click(button, ctx, custom_id):
    """What discord.py does with a component click: the context it
    builds for one has no command."""
    ctx.command = None

    async def defer(**kwargs):
        pass

    async def get_context(inter):
        return ctx

    inter = types.SimpleNamespace(
        response=types.SimpleNamespace(defer=defer),
        data={"custom_id": custom_id},
        guild=ctx.guild,
        client=types.SimpleNamespace(
            get_context=get_context,
            sessions=types.SimpleNamespace(controller=lambda guild: None),
        ),
    )
    asyncio.run(button.callback(inter))


PLAYER_BUTTONS = [
    "prev",
    "pause",
    "next",
    "loop",
    "shuffle",
    "stop",
    "volume_down",
    "volume_up",
    "current_song",
    "queue",
]


class TestButtons:
    @pytest.mark.parametrize("custom_id", PLAYER_BUTTONS)
    def test_a_player_button_does_not_connect(self, custom_id):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a"), admin=True)
        ran = []
        click(MusicButton(ran.append, custom_id=custom_id), ctx, custom_id)
        assert ran == [ctx]
        assert guild.events == []

    @pytest.mark.parametrize("custom_id", PLAYER_BUTTONS)
    def test_a_player_button_does_not_move(self, custom_id, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b"), admin=True)
        ran = []
        click(MusicButton(ran.append, custom_id=custom_id), ctx, custom_id)
        assert ran == [ctx]
        assert guild.events == []

    def test_a_search_pick_connects(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a"))

        @contextlib.asynccontextmanager
        async def typing():
            yield

        ctx.channel.typing = typing
        ctx.interaction = None
        played = []

        async def play_song(ctx, track):
            played.append(track)

        cog = types.SimpleNamespace(cog_check=play_check, _play_song=play_song)
        view = SearchView(ctx)
        view.add_item(SongButton(cog, 1, "https://example.com/a"))
        click(view.children[0], ctx, "pick")
        assert guild.events == [("connect", "a")]
        assert played == ["https://example.com/a"]
