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
from musicbot.message_link import CheckedTrack
from musicbot.bot import Context
from musicbot.commands.general import General
from musicbot.commands.library import Library, queue_songs
from musicbot.views import MusicButton
from musicbot.commands.music import Music, SearchView, SongButton
from musicbot import admission
from musicbot.utils import (
    CheckError,
    chunks,
    dj_check,
    get_audiocontroller,
    get_settings,
    owner_check,
)
from discord_fakes import (
    ADMISSION_DJ_ROLE,
    HERE,
    admission_settings,
    fake_controller,
    situation,
    FakeInteraction,
    VoiceGuild,
    check_error_message,
    command_of,
    context_client,
    invoke_music_hooks,
    make_bot,
    run_command_checks,
)
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
    moved the KeyError one line down into the admission check."""

    def test_returns_the_registered_settings(self):
        guild = Guild()
        bot = make_bot(settings={guild: "sett"})
        assert get_settings(make_ctx(bot, guild)) == "sett"

    def test_controller_only_guild_gets_admission_settings(self):
        guild = Guild()
        bot = make_bot(controllers={guild: object()})
        sett = get_settings(make_ctx(bot, guild))
        assert (
            sett.command_channel,
            sett.user_must_be_in_vc,
            sett.dj_role,
            sett.vc_timeout,
        ) == (None, True, None, True)

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
        with pytest.raises(NotOwner, match="You do not own this bot."):
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
    dj_role=None,
    admin=False,
    owner=False,
    roles=(),
):
    sett = admission_settings(dj_role=dj_role)
    bot = make_bot(
        {guild: fake_controller()},
        is_owner=owner,
        settings={guild: sett},
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


async def join(ctx):
    await (await admission.check(ctx)).enter()


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
        assert check_error_message(dj_check(ctx)) == config.NOT_A_DJ

    def test_admits_an_admin_with_a_dj_role_set(self):
        ctx = self.ctx(dj_role=99, roles=[5], admin=True)
        assert run(dj_check(ctx)) is True

    def test_admits_the_application_owner_with_a_dj_role_set(self):
        ctx = self.ctx(dj_role=99, roles=[5], owner=True)
        assert run(dj_check(ctx)) is True


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
    ctx.command = None

    async def ctx_defer(**kw):
        return await Context.defer(ctx, **kw)

    ctx.defer = ctx_defer
    return ctx


class TestMusicBeforeInvoke:
    """Music.cog_before_invoke directs the player panel to the
    command's channel and never defers or joins: a joining command
    does that in its body, after its input checks (see
    tests/test_joining_after_checks.py)."""

    def test_directs_the_panel_to_the_channel_not_the_context(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        run(invoke_music_hooks(Music(None), ctx))
        assert ctx.bot.sessions.panel(guild).directed == [(ctx.channel, True)]

    def test_a_second_defer_in_the_body_does_not_raise(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = command_of(Music, "play")

        async def scenario():
            cog = Music(None)
            await invoke_music_hooks(cog, ctx)
            await cog._enter(ctx)
            await ctx.defer()

        run(scenario())
        assert guild.events == [("defer", False), ("connect", "a")]


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
    run(join(ctx))
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
        await General._connect.callback(General(None), ctx)

    run(scenario())
    assert guild.events == [("defer", False), ("move", "b")]
    assert sent == ["Moved to your voice channel."]


def test_a_failed_join_from_the_library_says_so():
    """Queueing from the library browser joins voice itself, after
    the admission check; a failed connect is reported as one, not as a
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
        client=context_client(ctx),
        response=types.SimpleNamespace(
            is_done=lambda: False, send_message=send_message
        ),
        edit_original_response=edit_original_response,
    )
    run(queue_songs(interaction, [("A", "B", "c.mp3")], "browse"))
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
        if run(admission.check(ctx)).join_needed:
            joining.add(cmd.qualified_name)
    assert joining == JOINING


class TestNonJoiningCommand:
    """An unmarked music command never touches voice: it does not
    connect or move the bot. Its lack of a voice requirement is
    covered by the tests/test_admission.py table."""

    def test_does_not_connect_while_idle(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a", humans=1))
        ctx.command = command_of(Music, "history")
        run(invoke_music_hooks(Music(None), ctx))
        assert guild.events == []

    def test_does_not_move_out_of_a_bots_only_channel(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b", humans=1))
        ctx.command = command_of(Music, "pause")
        run(invoke_music_hooks(Music(None), ctx))
        assert guild.events == []

    def test_a_playlist_subcommand_other_than_load_does_not_connect(self):
        guild = VoiceGuild()
        ctx = slash_ctx(guild, guild.channel("a"))
        ctx.command = command_of(Music, "playlist show")
        run(invoke_music_hooks(Music(None), ctx))
        assert guild.events == []


class TestJoiningCommand:
    """A joining command's body, through _enter, defers and moves the bot
    out of a bots-only channel (the hooks never join: see
    tests/test_admission.py)."""

    def test_enter_defers_and_moves_out_of_a_bots_only_channel(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = slash_ctx(guild, guild.channel("b"))
        ctx.command = command_of(Music, "prev")
        cog = Music(None)
        run(invoke_music_hooks(cog, ctx))
        run(cog._enter(ctx))
        assert guild.events == [("defer", False), ("move", "b")]


def click(button, ctx, custom_id):
    """What discord.py does with a component click: the context it
    builds for one has no command."""
    ctx.command = None
    inter = FakeInteraction(ctx, custom_id)
    if not hasattr(ctx, "interaction"):
        ctx.interaction = inter
    return run(button.callback(inter))


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
    def test_a_player_button_runs_on_a_connected_bot_without_joining(self):
        guild = VoiceGuild()
        here = guild.channel("a", humans=1)
        guild.put_bot_in(here)
        ctx = voice_ctx(guild, here, admin=True)
        _, ran = press(ctx, "queue")
        assert ran == [ctx]
        assert guild.events == []

    def test_a_player_button_does_not_move(self, no_sleep):
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(guild, guild.channel("b"), admin=True)
        _, ran = press(ctx, "queue")
        assert ran == [ctx]
        assert guild.events == []

    def test_a_player_button_ignores_voice_permissions_it_never_needs(self):
        """A player button never moves the bot, so the user's channel
        lacking connect/speak for the bot does not refuse it."""
        guild = VoiceGuild()
        guild.put_bot_in(guild.channel("a", bots=1))
        ctx = voice_ctx(
            guild, guild.channel("b", connect=False, speak=False), admin=True
        )
        sent, ran = press(ctx, "queue")
        assert sent == []
        assert ran == [ctx]

    def test_a_search_pick_connects(self):
        guild = VoiceGuild()
        ctx = voice_ctx(guild, guild.channel("a"))

        @contextlib.asynccontextmanager
        async def typing():
            yield

        ctx.channel.typing = typing
        ctx.interaction = None
        played = []

        async def play_song(ctx, checked):
            played.append(checked.track)

        async def checked_track(ctx, track):
            return CheckedTrack(track)

        cog = types.SimpleNamespace(
            _play_song=play_song, _checked_track=checked_track
        )
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
def admitting_admission(monkeypatch):
    # every check behind the playlist gate admits
    async def admit(ctx, kind=None):
        return types.SimpleNamespace(audiocontroller=None)

    monkeypatch.setattr(admission, "check", admit)


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
def test_gate_admits_while_enabled(cmd, monkeypatch, admitting_admission):
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
    monkeypatch, admitting_admission
):
    # a component interaction's context carries no command
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", False)
    ctx = types.SimpleNamespace(command=None)
    assert run(Music(None).cog_check(ctx)) is True


def test_the_library_hook_directs_the_panel_to_the_channel():
    guild = VoiceGuild()
    ctx = slash_ctx(guild, guild.channel("a"))
    ctx.audiocontroller = types.SimpleNamespace()

    run(Library(None).cog_before_invoke(ctx))

    assert ctx.bot.sessions.panel(guild).directed == [(ctx.channel, True)]


@pytest.fixture
def library_path(monkeypatch):
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", "/x")


def test_a_library_command_in_an_unregistered_guild_is_not_ready(
    library_path,
):
    ctx = make_ctx(make_bot(), Guild())
    with pytest.raises(CheckError) as exc:
        run(Library(None).cog_check(ctx))
    assert str(exc.value) == config.BOT_NOT_READY
    assert not hasattr(ctx, "audiocontroller")


def test_a_library_command_in_a_registered_guild_is_admitted(library_path):
    guild = Guild()
    ctx = make_ctx(make_bot({guild: "controller"}), guild)
    assert run(Library(None).cog_check(ctx)) is True
    assert not hasattr(ctx, "audiocontroller")


@pytest.mark.parametrize("registered", [True, False])
def test_a_library_command_without_a_library_path_is_not_configured(
    monkeypatch, registered
):
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", "")
    guild = Guild()
    bot = make_bot({guild: "controller"} if registered else {})
    ctx = make_ctx(bot, guild)
    with pytest.raises(CheckError) as exc:
        run(Library(None).cog_check(ctx))
    assert str(exc.value) == config.LIBRARY_NOT_CONFIGURED


# --- which commands need a DJ ------------------------------------------
#
# With a DJ role set, a command that changes shared playback, removes or
# reorders what is queued, or changes saved playlists needs a DJ; a
# read-only command, or one that only adds songs or brings the bot in,
# does not. The table test pins the set; the behaviour tests show a
# refused non-DJ leaves everything untouched.

NEEDS_A_DJ = {
    # Music
    "loop",
    "shuffle",
    "pause",
    "stop",
    "move",
    "remove",
    "skip",
    "restore",
    "clear",
    "prev",
    "volume",
    "playlist save",
    "playlist remove",
    "playlist add_song",
    "playlist remove_song",
    "playlist move_song",
    # General
    "disconnect",
    "reset",
}

# name -> (cog, request kind, bot channel, author) for the gated tests
GATED_COMMANDS = {
    "stop": (Music, admission.RequestKind.COMMAND, HERE, "a"),
    "disconnect": (General, admission.RequestKind.COMMAND, HERE, "a"),
    "reset": (General, admission.RequestKind.JOINING_COMMAND, None, "b"),
}


def test_the_commands_needing_a_dj_are_exactly_the_listed_ones(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", True)
    refused = set()
    for cog in (Music, General):
        for cmd in cog(None).walk_commands():
            _, ctx = situation(
                admission.RequestKind.COMMAND, dj_role=ADMISSION_DJ_ROLE
            )
            ctx.audiocontroller = types.SimpleNamespace(is_active=lambda: True)
            refusal = check_error_message(run_command_checks(cmd, ctx))
            if refusal == config.NOT_A_DJ:
                refused.add(cmd.qualified_name)
            if cmd.qualified_name in NEEDS_A_DJ:
                assert dj_check in cmd.checks, cmd.qualified_name
    assert refused == NEEDS_A_DJ


def gated_command_context(name, role=None):
    """(guild, ctx, calls, deferred) for the gated command `name`."""
    cog, kind, bot_channel, author = GATED_COMMANDS[name]
    guild, ctx = situation(
        kind,
        bot=bot_channel,
        author=author,
        role=role,
        dj_role=ADMISSION_DJ_ROLE,
    )
    ctx.command = command_of(cog, name)
    calls = []
    deferred = []
    sent = []

    async def send(content=None, **kwargs):
        sent.append(content)

    async def defer(**kwargs):
        deferred.append(True)

    async def udisconnect(reason):
        calls.append(("udisconnect", reason))
        return True

    async def dispose(reason):
        calls.append(("dispose", reason))

    controller = ctx.controllers[guild]
    controller.udisconnect = udisconnect
    controller.dispose = dispose
    controller.stop = lambda: calls.append(("stop",))
    ctx.audiocontroller = controller
    ctx.send = send
    ctx.defer = defer
    ctx.sent = sent
    return guild, ctx, calls, deferred


def invoke_gated(name, ctx):
    """Check-then-invoke; the refusal message, or None."""
    cog = GATED_COMMANDS[name][0]
    cb = getattr(cog, f"_{name}").callback

    async def scenario():
        await run_command_checks(ctx.command, ctx)
        await cb(cog(None), ctx)

    return check_error_message(scenario())


@pytest.mark.parametrize("name", list(GATED_COMMANDS))
def test_a_non_dj_is_refused_and_nothing_happens(name):
    guild, ctx, calls, deferred = gated_command_context(name)
    controller = ctx.controllers[guild]
    assert invoke_gated(name, ctx) == config.NOT_A_DJ
    assert calls == []
    assert deferred == []
    assert ctx.sent == []
    assert ctx.bot.sessions.controller(guild) is controller
    assert guild.events == []


@pytest.mark.parametrize("name", list(GATED_COMMANDS))
def test_a_dj_role_holder_is_admitted(name, no_sleep):
    _, ctx, calls, _ = gated_command_context(name, role="dj")
    ctx.guild.events.clear()
    assert invoke_gated(name, ctx) is None
    assert calls
