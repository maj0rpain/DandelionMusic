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
from musicbot import utils
from musicbot.utils import (
    CheckError,
    chunks,
    dj_check,
    get_audiocontroller,
    get_settings,
    owner_check,
    join_needed,
    join_voice,
    play_check,
    voice_check,
)
from discord_fakes import PLAYER_BUTTONS, FakeInteraction, VoiceGuild, make_bot
from helpers import run


def make_ctx(bot, guild=None, author_id=1):
    return types.SimpleNamespace(
        bot=bot, guild=guild, author=types.SimpleNamespace(id=author_id)
    )


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
    monkeypatch.setattr(config, "EXTRA_OWNERS", extra_owners)
    ctx = make_ctx(make_bot(is_owner=is_owner), author_id=author_id)

    if allowed:
        assert run(owner_check(ctx)) is True
    else:
        with pytest.raises(NotOwner):
            run(owner_check(ctx))


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


COMMAND_CHANNEL = 7


def voice_ctx(
    guild,
    author_in=None,
    rule=True,
    command_channel=None,
    dj_role=None,
    admin=False,
    owner=False,
    roles=(),
):
    sett = types.SimpleNamespace(
        command_channel=command_channel,
        user_must_be_in_vc=rule,
        dj_role=dj_role,
        vc_timeout=True,  # get_or_create() does not autojoin
    )
    bot = make_bot(
        {guild: "controller"}, is_owner=owner, settings={guild: sett}
    )
    author = types.SimpleNamespace(
        id=1,
        roles=[types.SimpleNamespace(id=r) for r in roles],
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
        run(coro)
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
        assert run(play_check(voice_ctx(guild, here))) is True
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
        assert run(play_check(ctx)) is True
        assert guild.events == []

    def test_admits_a_bots_only_channel_without_moving(self):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b", humans=1))
        assert run(play_check(ctx)) is True
        assert guild.events == []


class TestDjCheck:
    def ctx(self, **kwargs):
        return voice_ctx(VoiceGuild(), **kwargs)

    def test_admits_when_no_dj_role_is_set(self):
        assert run(dj_check(self.ctx())) is True

    def test_admits_a_dj_role_holder(self):
        ctx = self.ctx(dj_role=99, roles=[99])
        assert run(dj_check(ctx)) is True

    def test_refuses_a_member_without_the_set_dj_role(self):
        ctx = self.ctx(dj_role=99, roles=[5])
        assert refusal(dj_check(ctx)) == config.NOT_A_DJ


class TestVoiceCheckBypass:
    def ctx(self, **kwargs):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", humans=1))
        return voice_ctx(guild, guild.channel("b", humans=1), **kwargs)

    def test_refuses_a_member_when_no_dj_role_is_set(self):
        ctx = self.ctx()
        assert refusal(voice_check(ctx)) == config.USER_NOT_IN_VC_MESSAGE

    def test_admits_an_admin(self):
        assert run(voice_check(self.ctx(admin=True))) is True

    def test_admits_the_bot_owner(self):
        assert run(voice_check(self.ctx(owner=True))) is True

    def test_admits_a_dj_role_holder(self):
        ctx = self.ctx(dj_role=99, roles=[99])
        assert run(voice_check(ctx)) is True

    def test_refuses_a_member_without_the_set_dj_role(self):
        ctx = self.ctx(dj_role=99, roles=[5])
        assert refusal(voice_check(ctx)) == config.USER_NOT_IN_VC_MESSAGE

    def test_extra_owners_get_no_bypass(self, monkeypatch):
        monkeypatch.setattr(config, "EXTRA_OWNERS", [1])
        assert refusal(voice_check(self.ctx())) == (
            config.USER_NOT_IN_VC_MESSAGE
        )


class TestJoinVoice:
    def test_connects_when_the_bot_is_out_of_voice(self):
        guild = VoiceGuild()
        run(join_voice(voice_ctx(guild, guild.channel("a"))))
        assert guild.events == [("connect", "a")]

    def test_moves_out_of_a_bots_only_channel_under_the_rule(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        run(join_voice(voice_ctx(guild, guild.channel("b"))))
        assert guild.events == [("move", "b")]

    def test_does_not_move_out_of_a_bots_only_channel_without_the_rule(
        self,
    ):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b"), rule=False)
        run(join_voice(ctx))
        assert guild.events == []

    def test_does_not_move_out_of_a_channel_with_people(self):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", humans=1))
        run(join_voice(voice_ctx(guild, guild.channel("b"))))
        assert guild.events == []

    def test_does_nothing_when_already_with_the_user(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        run(join_voice(voice_ctx(guild, here)))
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
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("connect", "a")]

    def test_defers_before_moving(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b"))
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("move", "b")]

    def test_neither_defers_nor_joins_without_a_join(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        ctx = slash_ctx(guild, here)
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []
        assert ctx.audiocontroller.command_channel is ctx

    def test_a_second_defer_in_the_body_does_not_raise(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))

        async def scenario():
            await Music(None).cog_before_invoke(ctx)
            await ctx.defer()

        run(scenario())
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
    run(join_voice(ctx))
    assert guild.events == [("connect", "a")]


def test_connect_defers_before_connecting_while_idle():
    """/connect defers publicly before the slow join."""
    guild = VoiceGuild()
    ctx = slash_ctx(guild, guild.channel("a"))
    sent = recording_send(ctx)
    run(General._connect.callback(General(None), ctx))
    assert guild.events == [("defer", False), ("connect", "a")]
    assert sent == ["Connected."]


def test_connect_moves_the_bot_out_of_a_bots_only_channel(no_sleep):
    guild = VoiceGuild()
    guild.put_bot_in(guild.channel("a", bots=1))
    ctx = slash_ctx(guild, guild.channel("b", humans=1))
    ctx.command = command_of(General, "connect")
    sent = []

    async def send(content, **kwargs):
        sent.append(content)

    ctx.send = send

    async def scenario():
        assert await voice_check(ctx)
        await General._connect.callback(General(None), ctx)

    run(scenario())
    assert guild.events == [("defer", False), ("move", "b")]
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
    run(queue_songs(ctx, interaction, [("A", "B", "c.mp3")], "browse"))
    assert replies == ["Queueing...", config.VOICE_CONNECT_FAILED]
    assert guild.events == [("connect", "a")]


# --- joining commands ------------------------------------------------
#
# Only a joining command (GLOSSARY.md) brings the bot into voice: one
# carrying the opt-in marker; every other music command answers
# without connecting or moving the bot.


JOINING = {"play", "playnext", "search", "playlist load", "restore", "prev"}


def test_the_joining_music_commands_are_exactly_the_marked_ones():
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
        ctx.command = command_of(Music, "history")
        assert run(play_check(ctx)) is True
        assert guild.events == []

    def test_skips_the_voice_permission_check_while_idle(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a", connect=False, speak=False))
        ctx.command = command_of(Music, "playlist list")
        assert run(play_check(ctx)) is True

    def test_does_not_connect_while_idle(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a", humans=1))
        ctx.command = command_of(Music, "history")
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []

    def test_does_not_move_out_of_a_bots_only_channel(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b", humans=1))
        ctx.command = command_of(Music, "pause")
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []

    def test_a_playlist_subcommand_other_than_load_does_not_connect(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = command_of(Music, "playlist show")
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == []


class TestJoiningCommand:
    def test_defers_and_connects_while_idle(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = command_of(Music, "play")
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("connect", "a")]

    def test_defers_and_moves_out_of_a_bots_only_channel(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b"))
        ctx.command = command_of(Music, "prev")
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("move", "b")]

    def test_playlist_load_connects(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = command_of(Music, "playlist load")
        run(Music(None).cog_before_invoke(ctx))
        assert guild.events == [("defer", False), ("connect", "a")]

    def test_still_refuses_a_user_outside_voice_while_idle(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, None)
        ctx.command = command_of(Music, "search")
        assert refusal(play_check(ctx)) == config.USER_NOT_IN_VC_MESSAGE


def click(button, ctx, custom_id):
    """What discord.py does with a component click: the context it
    builds for one has no command."""
    ctx.command = None
    return run(button.callback(FakeInteraction(ctx, custom_id)))


def recording_send(ctx):
    """Replaces ctx.send with one recording each reply's text."""
    sent = []

    async def send(content, **kwargs):
        sent.append(str(content))

    ctx.send = send
    return sent


def press(ctx, custom_id):
    """Clicks a MusicButton: (what was sent, what ran)."""
    sent = recording_send(ctx)
    ran = []
    click(MusicButton(ran.append, custom_id=custom_id), ctx, custom_id)
    return sent, ran


class TestButtons:
    @pytest.mark.parametrize("custom_id", PLAYER_BUTTONS)
    def test_a_player_button_does_not_connect(self, custom_id):
        """With no voice client a player button is refused instead:
        a stale player view must not act on a bot out of voice."""
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a"), admin=True)
        sent, ran = press(ctx, custom_id)
        assert sent == [config.NOT_CONNECTED_MESSAGE]
        assert ran == []
        assert guild.events == []

    @pytest.mark.parametrize("custom_id", PLAYER_BUTTONS)
    def test_a_player_button_runs_on_a_connected_bot_without_joining(
        self, custom_id
    ):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        ctx = voice_ctx(guild, here, admin=True)
        _, ran = press(ctx, custom_id)
        assert ran == [ctx]
        assert guild.events == []

    @pytest.mark.parametrize("custom_id", PLAYER_BUTTONS)
    def test_a_player_button_does_not_move(self, custom_id, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b"), admin=True)
        _, ran = press(ctx, custom_id)
        assert ran == [ctx]
        assert guild.events == []

    @pytest.mark.parametrize("custom_id", PLAYER_BUTTONS)
    def test_a_player_button_ignores_voice_permissions_it_never_needs(
        self, custom_id
    ):
        """A player button never moves the bot, so the user's channel
        lacking connect/speak for the bot does not refuse it."""
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(
            guild, guild.channel("b", connect=False, speak=False), admin=True
        )
        sent, ran = press(ctx, custom_id)
        assert sent == []
        assert ran == [ctx]

    def test_a_player_button_refuses_a_user_outside_voice_while_idle(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, None)
        sent, ran = press(ctx, "queue")
        assert sent == [config.NOT_CONNECTED_MESSAGE]
        assert ran == []

    def test_a_player_button_obeys_the_command_channel(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        ctx = voice_ctx(guild, here, command_channel=COMMAND_CHANNEL + 1)
        sent, ran = press(ctx, "queue")
        assert sent == [config.WRONG_CHANNEL_MESSAGE]
        assert ran == []

    def test_a_player_button_obeys_the_voice_rule(self):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", humans=1))
        # with a DJ role set, a non-DJ is not let through as one
        ctx = voice_ctx(guild, guild.channel("b"), dj_role=99)
        sent, ran = press(ctx, "queue")
        assert sent == [config.USER_NOT_IN_VC_MESSAGE]
        assert ran == []

    def test_a_player_button_obeys_the_dj_check(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        ctx = voice_ctx(guild, here, dj_role=99)
        sent, ran = press(ctx, "next")
        assert sent == [config.NOT_A_DJ]
        assert ran == []

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


SUBCOMMANDS = [
    "load",
    "remove",
    "add_song",
    "remove_song",
    "move_song",
    "show",
    "list",
    "save",
]


def commands_under_gate():
    group = Music._playlist
    subs = {c.name: c for c in group.commands}
    assert set(SUBCOMMANDS) <= set(subs)
    return [group] + [subs[n] for n in SUBCOMMANDS]


def registered(cmd):
    # the registered cog holds per-instance copies of its commands
    cog = Music(None)
    group = cog._playlist
    subs = {c.name: c for c in group.commands}
    return cog, (group if cmd.name == group.name else subs[cmd.name])


@pytest.fixture
def admitting_play_check(monkeypatch):
    # every check behind the playlist gate admits
    async def passing(ctx):
        return True

    monkeypatch.setattr(utils, "get_audiocontroller", lambda ctx: None)
    monkeypatch.setattr(utils, "play_check", passing)


@pytest.mark.parametrize("cmd", commands_under_gate(), ids=lambda c: c.name)
def test_cog_check_gates_before_other_refusals(cmd, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", False)
    cog, cmd = registered(cmd)
    # no guild, channel or author: any other refusal would break differently
    ctx = types.SimpleNamespace(command=cmd)
    with pytest.raises(CheckError) as exc:
        run(cog.cog_check(ctx))
    assert str(exc.value) == config.PLAYLISTS_ARE_DISABLED


@pytest.mark.parametrize("cmd", commands_under_gate(), ids=lambda c: c.name)
def test_gate_admits_while_enabled(cmd, monkeypatch, admitting_play_check):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", True)
    cog, cmd = registered(cmd)
    ctx = types.SimpleNamespace(command=cmd)
    assert run(cog.cog_check(ctx)) is True


@pytest.mark.parametrize("cmd", commands_under_gate(), ids=lambda c: c.name)
def test_no_playlist_command_carries_the_gate_decorator(cmd):
    assert all(c.__name__ != "playlists_enabled" for c in cmd.checks)


def test_autocomplete_offers_nothing_while_disabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", False)
    cog = types.SimpleNamespace(bot=None)
    interaction = types.SimpleNamespace(guild=None)
    result = run(Music._playlist_autocomplete(cog, interaction, ""))
    assert result == []


def test_cog_check_admits_a_button_context_with_no_command(
    monkeypatch, admitting_play_check
):
    # a component interaction's context carries no command
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", False)
    ctx = types.SimpleNamespace(command=None)
    assert run(Music(None).cog_check(ctx)) is True
