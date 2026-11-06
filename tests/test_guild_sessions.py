"""GuildSessions, the per-guild registry of settings and controllers.

Built on fake guilds, a fake controller factory and a fake settings
loader: the registry only ever calls the factory, the loader and the
controller's dispose() and register_voice_channel(), so nothing here
needs Discord or a database.
"""

import asyncio

import discord
import pytest

from musicbot.sessions import GuildSessions
from controller_helpers import sleep_forever
from discord_fakes import FakePanel
from helpers import run


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.name = f"guild {guild_id}"
        self.voice_channels = []
        self.voice_client = None
        self.me = None

    def get_channel(self, channel_id):
        return None


class FakeSettings:
    def __init__(self, guild):
        self.guild = guild
        # a vc_timeout means no autojoin when the controller is built
        self.vc_timeout = True
        self.start_voice_channel = None
        self.command_channel = None
        self.user_must_be_in_vc = False


class FakeController:
    def __init__(self, guild, panel=None):
        self.guild = guild
        self.panel = panel
        self.disposed = False

    async def dispose(self, reason="left guild"):
        self.disposed = True


class Factory:
    """Builds FakeControllers, or raises while `fail` is set."""

    def __init__(self):
        self.fail = False
        self.built = []

    def __call__(self, guild, panel):
        if self.fail:
            raise RuntimeError("controller factory failed")
        controller = FakeController(guild, panel)
        self.built.append(controller)
        return controller


async def load_settings(guilds):
    return {guild: FakeSettings(guild) for guild in guilds}


@pytest.fixture
def factory():
    return Factory()


@pytest.fixture
def sessions(factory):
    return GuildSessions(lambda guild: FakePanel(), factory, load_settings)


def test_get_or_create_registers_settings_and_a_controller(sessions):
    guild = FakeGuild(1)

    controller = run(sessions.get_or_create(guild))

    assert sessions.controller(guild) is controller
    assert sessions.settings(guild).guild is guild


def test_get_or_create_returns_the_existing_controller(sessions, factory):
    guild = FakeGuild(1)

    async def scenario():
        return (
            await sessions.get_or_create(guild),
            await sessions.get_or_create(guild),
        )

    first, second = run(scenario())

    assert first is second
    assert factory.built == [first]


def test_a_failing_factory_leaves_the_guild_settings_only(sessions, factory):
    guild = FakeGuild(1)
    factory.fail = True

    with pytest.raises(RuntimeError):
        run(sessions.get_or_create(guild))

    assert sessions.controller(guild) is None
    assert sessions.settings(guild).guild is guild


def test_load_settings_fills_the_settings_half_only(sessions):
    guilds = [FakeGuild(1), FakeGuild(2)]

    run(sessions.load_settings(guilds))

    for guild in guilds:
        assert sessions.settings(guild).guild is guild
        assert sessions.controller(guild) is None
    assert list(sessions) == []


def test_discard_disposes_the_controller_and_drops_both_halves(controller):
    # a real AudioController (tests/controller_seam.py), so dispose()
    # is the real one: its timer and pending tasks must not survive
    guild = controller.guild
    sessions = GuildSessions(
        lambda guild: FakePanel(), lambda g, panel: controller, load_settings
    )

    async def scenario():
        controller.bot.loop = asyncio.get_running_loop()
        await sessions.get_or_create(guild)
        await controller.timer.start()
        timer_task = controller.timer._task
        started = []

        controller.add_task(sleep_forever(started))
        await asyncio.sleep(0)
        (pending,) = started

        await sessions.discard(guild)
        for _ in range(10):
            await asyncio.sleep(0)
        return timer_task, pending

    timer_task, pending = run(scenario())

    assert timer_task.cancelled()
    assert pending.cancelled()
    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is None


def test_discard_drops_both_halves_even_when_dispose_raises(sessions):
    guild = FakeGuild(1)

    async def failing_dispose():
        raise RuntimeError("dispose failed")

    async def scenario():
        controller = await sessions.get_or_create(guild)
        controller.dispose = failing_dispose
        await sessions.discard(guild)

    with pytest.raises(RuntimeError):
        run(scenario())

    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is None


def test_discard_of_a_settings_only_guild_drops_its_settings(sessions):
    guild = FakeGuild(1)

    async def scenario():
        await sessions.load_settings([guild])
        await sessions.discard(guild)

    run(scenario())

    assert sessions.settings(guild) is None


def test_iteration_no_longer_yields_a_discarded_controller(sessions):
    kept, dropped = FakeGuild(1), FakeGuild(2)

    async def scenario():
        kept_controller = await sessions.get_or_create(kept)
        await sessions.get_or_create(dropped)
        await sessions.discard(dropped)
        return kept_controller

    kept_controller = run(scenario())

    assert list(sessions) == [kept_controller]


def test_reset_replaces_the_controller_and_keeps_the_settings(sessions):
    guild = FakeGuild(1)

    async def scenario():
        old = await sessions.get_or_create(guild)
        settings = sessions.settings(guild)
        new = await sessions.reset(guild)
        return old, settings, new

    old, settings, new = run(scenario())

    assert old.disposed
    assert new is not old
    assert sessions.controller(guild) is new
    assert sessions.settings(guild) is settings


def test_a_reset_whose_factory_fails_leaves_the_guild_settings_only(
    sessions, factory
):
    guild = FakeGuild(1)

    async def scenario():
        old = await sessions.get_or_create(guild)
        factory.fail = True
        with pytest.raises(RuntimeError):
            await sessions.reset(guild)
        return old

    old = run(scenario())

    assert old.disposed
    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is not None


def test_on_guild_remove_discards_the_guilds_session(sessions):
    """Leaving a guild (kicked, banned, or a guild.leave() of our own)
    must not leak its session: MusicBot.on_guild_remove discards it."""
    import types

    from musicbot.bot import MusicBot

    guild = FakeGuild(1)
    bot = types.SimpleNamespace(sessions=sessions)

    async def scenario():
        controller = await sessions.get_or_create(guild)
        await MusicBot.on_guild_remove(bot, guild)
        return controller

    controller = run(scenario())

    assert controller.disposed
    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is None


class ResettableController(FakeController):
    """Enough of a controller for d!reset: dispose() disconnects."""

    def __init__(self, guild, panel=None):
        super().__init__(guild, panel)
        self.disconnects = []

    async def udisconnect(self, reason):
        self.disconnects.append(reason)
        return False

    async def dispose(self, reason="left guild"):
        # as AudioController.dispose(): disconnecting is part of it
        await self.udisconnect(reason)
        await super().dispose(reason)


class CountingVoiceChannel:
    """The user's voice channel: connect() joins it, or raises `fail`."""

    def __init__(self, guild, name="General", fail=None):
        self.guild = guild
        self.name = name
        self.fail = fail
        self.connects = 0

    def permissions_for(self, member):
        import types

        return types.SimpleNamespace(connect=True, speak=True)

    async def connect(self, **kwargs):
        self.connects += 1
        if self.fail is not None:
            raise self.fail
        self.guild.voice_client = object()


def run_reset(guild, channel):
    """Runs d!reset in `guild` for a user in `channel`; returns the
    old controller, its settings, the replies, and the CheckError
    message reset raised (None if it raised nothing)."""
    import types

    from musicbot.commands.general import General
    from musicbot.utils import CheckError

    sessions = GuildSessions(
        lambda guild: FakePanel(), ResettableController, load_settings
    )
    sent = []

    async def defer():
        pass

    async def send(message):
        sent.append(message)

    async def scenario():
        old = await sessions.get_or_create(guild)
        settings = sessions.settings(guild)
        bot = types.SimpleNamespace(sessions=sessions)
        ctx = types.SimpleNamespace(
            bot=bot,
            guild=guild,
            defer=defer,
            send=send,
            author=types.SimpleNamespace(
                voice=types.SimpleNamespace(channel=channel)
            ),
        )
        try:
            await General._reset.callback(General(bot), ctx)
        except CheckError as e:
            return old, settings, str(e)
        return old, settings, None

    old, settings, refusal = run(scenario())
    return sessions, old, settings, sent, refusal


def test_reset_command_replaces_the_controller_and_keeps_settings():
    """d!reset goes through the registry: the old controller is
    disposed of, the bot joins the user's channel, the settings stay."""
    guild = FakeGuild(1)
    channel = CountingVoiceChannel(guild)

    sessions, old, settings, sent, refusal = run_reset(guild, channel)

    assert refusal is None
    assert old.disposed
    # once, through dispose(): a second udisconnect() would back the
    # already-cleared queue up over the one d!restore reloads
    assert old.disconnects == ["reset command"]
    assert sessions.controller(guild) is not old
    assert sessions.settings(guild) is settings
    assert channel.connects == 1
    assert guild.voice_client is not None
    assert sent == [":white_check_mark: Connected to General"]


@pytest.mark.parametrize(
    "error",
    [asyncio.TimeoutError(), discord.ClientException("x")],
)
def test_reset_tells_the_user_when_the_voice_connect_fails(error, capsys):
    from config import config

    guild = FakeGuild(1)
    channel = CountingVoiceChannel(guild, fail=error)

    _, _, _, sent, refusal = run_reset(guild, channel)

    assert refusal == config.VOICE_CONNECT_FAILED
    assert channel.connects == 1
    assert sent == []


def test_reset_refuses_when_the_bot_is_still_in_voice(monkeypatch):
    """A voice client the reset did not clear: reset refuses rather
    than claim it connected, and connects nothing."""
    from config import config

    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    guild = FakeGuild(1)
    guild.voice_client = object()
    channel = CountingVoiceChannel(guild)

    _, _, _, sent, refusal = run_reset(guild, channel)

    assert refusal == config.ALREADY_CONNECTED_MESSAGE
    assert channel.connects == 0
    assert sent == []


def test_button_plugin_ignores_a_message_from_an_unregistered_guild():
    """A message can land before on_ready has loaded the guild's
    settings; the reaction button then has nothing to read."""
    import types

    from musicbot.plugins.button import Button

    guild = FakeGuild(1)
    reactions = []

    async def add_reaction(emoji):
        reactions.append(emoji)

    async def scenario():
        absolutely_ready = asyncio.get_running_loop().create_future()
        absolutely_ready.set_result(True)
        bot = types.SimpleNamespace(
            sessions=GuildSessions(
                lambda guild: FakePanel(), FakeController, load_settings
            ),
            user=object(),
            absolutely_ready=absolutely_ready,
        )
        message = types.SimpleNamespace(
            guild=guild,
            author=object(),
            content="https://youtu.be/dQw4w9WgXcQ",
            add_reaction=add_reaction,
        )
        await Button(bot).on_message(message)

    run(scenario())

    assert reactions == []


def test_the_panel_is_built_before_the_controller_and_passed_to_it(
    sessions, factory
):
    guild = FakeGuild(1)

    controller = run(sessions.get_or_create(guild))

    assert controller.panel is sessions.panel(guild)
    assert isinstance(controller.panel, FakePanel)


def test_a_guild_without_a_session_has_no_panel(sessions):
    assert sessions.panel(FakeGuild(1)) is None
    assert list(sessions.panels()) == []


def test_panels_iterates_every_registered_panel(sessions):
    guilds = [FakeGuild(1), FakeGuild(2)]

    async def scenario():
        for guild in guilds:
            await sessions.get_or_create(guild)

    run(scenario())

    assert list(sessions.panels()) == [sessions.panel(g) for g in guilds]


def test_direct_panel_directs_the_registered_panel(sessions):
    guild = FakeGuild(1)
    run(sessions.get_or_create(guild))

    sessions.direct_panel(guild, "chan")
    sessions.direct_panel(guild, "other", replace=False)

    assert sessions.panel(guild).directed == [
        ("chan", True),
        ("other", False),
    ]


def test_direct_panel_without_a_registered_panel_does_nothing(sessions):
    sessions.direct_panel(FakeGuild(1), "chan")


def test_discard_drops_the_panel(sessions):
    guild = FakeGuild(1)

    async def scenario():
        await sessions.get_or_create(guild)
        await sessions.discard(guild)

    run(scenario())

    assert sessions.panel(guild) is None


def test_reset_replaces_the_panel_with_the_controller(sessions):
    guild = FakeGuild(1)

    async def scenario():
        await sessions.get_or_create(guild)
        old_panel = sessions.panel(guild)
        new = await sessions.reset(guild)
        return old_panel, new

    old_panel, new = run(scenario())

    assert sessions.panel(guild) is not old_panel
    assert new.panel is sessions.panel(guild)


def test_a_failing_controller_factory_registers_no_panel(sessions, factory):
    guild = FakeGuild(1)
    factory.fail = True

    with pytest.raises(RuntimeError):
        run(sessions.get_or_create(guild))

    assert sessions.panel(guild) is None


class CountingLoader:
    """A settings loader that waits for `release`, counts its calls and
    raises on the first `failures` calls."""

    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures
        self.release = None
        self.entered = asyncio.Event()

    async def __call__(self, guilds):
        self.calls += 1
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.calls <= self.failures:
            raise RuntimeError("load failed")
        return {guild: FakeSettings(guild) for guild in guilds}


def _counting_sessions(loader):
    return GuildSessions(
        lambda guild: FakePanel(), lambda g, panel: FakeController(g), loader
    )


def test_concurrent_ensures_share_one_load():
    loader = CountingLoader()
    sessions = _counting_sessions(loader)
    guild = FakeGuild(1)

    async def scenario():
        loader.release = asyncio.Event()
        calls = [
            asyncio.ensure_future(sessions._ensure_settings(guild)),
            asyncio.ensure_future(sessions.get_or_create(guild)),
            asyncio.ensure_future(sessions._ensure_settings(guild)),
        ]
        await loader.entered.wait()
        loader.release.set()
        await asyncio.gather(*calls)
        return calls[0].result(), calls[2].result()

    first, third = run(scenario())

    assert loader.calls == 1
    assert first is third is sessions.settings(guild)


def test_load_settings_keeps_registered_settings():
    loader = CountingLoader()
    sessions = _counting_sessions(loader)
    guild, other = FakeGuild(1), FakeGuild(2)

    async def scenario():
        await sessions.load_settings([guild])
        registered = sessions.settings(guild)
        await sessions.load_settings([guild, other])
        return registered

    registered = run(scenario())

    assert sessions.settings(guild) is registered
    assert sessions.settings(other) is not None


def test_a_failed_shared_load_reaches_every_waiter_and_is_retried():
    loader = CountingLoader(failures=1)
    sessions = _counting_sessions(loader)
    guild = FakeGuild(1)

    async def scenario():
        loader.release = asyncio.Event()
        calls = [
            asyncio.ensure_future(sessions._ensure_settings(guild))
            for _ in range(2)
        ]
        await loader.entered.wait()
        loader.release.set()
        results = await asyncio.gather(*calls, return_exceptions=True)
        unregistered = sessions.settings(guild) is None
        retried = await sessions._ensure_settings(guild)
        return results, unregistered, retried

    results, unregistered, retried = run(scenario())

    assert all(isinstance(r, RuntimeError) for r in results)
    assert unregistered
    assert retried is sessions.settings(guild)
    assert loader.calls == 2


def test_a_guild_discarded_during_its_load_is_not_registered():
    loader = CountingLoader()
    sessions = _counting_sessions(loader)
    guild = FakeGuild(1)

    async def scenario():
        loader.release = asyncio.Event()
        load = asyncio.ensure_future(sessions.load_settings([guild]))
        await loader.entered.wait()
        await sessions.discard(guild)
        loader.release.set()
        await load

    run(scenario())

    assert sessions.settings(guild) is None
