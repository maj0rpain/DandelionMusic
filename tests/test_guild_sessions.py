"""GuildSessions, the per-guild registry of settings and controllers.

Built on fake guilds, a fake controller factory and a fake settings
loader: the registry only ever calls the factory, the loader and the
controller's dispose() and register_voice_channel(), so nothing here
needs Discord or a database.
"""

import asyncio

import pytest

from musicbot.sessions import GuildSessions


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.name = f"guild {guild_id}"
        self.voice_channels = []

    def get_channel(self, channel_id):
        return None


class FakeSettings:
    def __init__(self, guild):
        self.guild = guild
        # a vc_timeout means no autojoin when the controller is built
        self.vc_timeout = True
        self.start_voice_channel = None


class FakeController:
    def __init__(self, guild):
        self.guild = guild
        self.disposed = False

    async def dispose(self):
        self.disposed = True


class Factory:
    """Builds FakeControllers, or raises while `fail` is set."""

    def __init__(self):
        self.fail = False
        self.built = []

    def __call__(self, guild):
        if self.fail:
            raise RuntimeError("controller factory failed")
        controller = FakeController(guild)
        self.built.append(controller)
        return controller


async def load_settings(guilds):
    return {guild: FakeSettings(guild) for guild in guilds}


@pytest.fixture
def factory():
    return Factory()


@pytest.fixture
def sessions(factory):
    return GuildSessions(factory, load_settings)


def test_get_or_create_registers_settings_and_a_controller(sessions):
    guild = FakeGuild(1)

    controller = asyncio.run(sessions.get_or_create(guild))

    assert sessions.controller(guild) is controller
    assert sessions.settings(guild).guild is guild


def test_get_or_create_returns_the_existing_controller(sessions, factory):
    guild = FakeGuild(1)

    async def run():
        return (
            await sessions.get_or_create(guild),
            await sessions.get_or_create(guild),
        )

    first, second = asyncio.run(run())

    assert first is second
    assert factory.built == [first]


def test_a_failing_factory_leaves_the_guild_settings_only(sessions, factory):
    guild = FakeGuild(1)
    factory.fail = True

    with pytest.raises(RuntimeError):
        asyncio.run(sessions.get_or_create(guild))

    assert sessions.controller(guild) is None
    assert sessions.settings(guild).guild is guild


def test_load_settings_fills_the_settings_half_only(sessions):
    guilds = [FakeGuild(1), FakeGuild(2)]

    asyncio.run(sessions.load_settings(guilds))

    for guild in guilds:
        assert sessions.settings(guild).guild is guild
        assert sessions.controller(guild) is None
    assert list(sessions) == []


def test_discard_disposes_the_controller_and_drops_both_halves(
    controller, monkeypatch
):
    # a real AudioController (tests/controller_seam.py), so dispose()
    # is the real one: its timer and pending tasks must not survive
    from config import config

    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)
    guild = controller.guild
    sessions = GuildSessions(lambda g: controller, load_settings)

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await sessions.get_or_create(guild)
        await controller.timer.start()
        timer_task = controller.timer._task
        controller.add_task(asyncio.sleep(3600))
        (pending,) = controller._tasks

        await sessions.discard(guild)
        for _ in range(10):
            await asyncio.sleep(0)
        return timer_task, pending

    timer_task, pending = asyncio.run(run())

    assert timer_task.cancelled()
    assert pending.cancelled()
    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is None


def test_discard_drops_both_halves_even_when_dispose_raises(sessions):
    guild = FakeGuild(1)

    async def failing_dispose():
        raise RuntimeError("dispose failed")

    async def run():
        controller = await sessions.get_or_create(guild)
        controller.dispose = failing_dispose
        await sessions.discard(guild)

    with pytest.raises(RuntimeError):
        asyncio.run(run())

    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is None


def test_discard_of_a_settings_only_guild_drops_its_settings(sessions):
    guild = FakeGuild(1)

    async def run():
        await sessions.load_settings([guild])
        await sessions.discard(guild)

    asyncio.run(run())

    assert sessions.settings(guild) is None


def test_iteration_no_longer_yields_a_discarded_controller(sessions):
    kept, dropped = FakeGuild(1), FakeGuild(2)

    async def run():
        kept_controller = await sessions.get_or_create(kept)
        await sessions.get_or_create(dropped)
        await sessions.discard(dropped)
        return kept_controller

    kept_controller = asyncio.run(run())

    assert list(sessions) == [kept_controller]


def test_reset_replaces_the_controller_and_keeps_the_settings(sessions):
    guild = FakeGuild(1)

    async def run():
        old = await sessions.get_or_create(guild)
        settings = sessions.settings(guild)
        new = await sessions.reset(guild)
        return old, settings, new

    old, settings, new = asyncio.run(run())

    assert old.disposed
    assert new is not old
    assert sessions.controller(guild) is new
    assert sessions.settings(guild) is settings


def test_a_reset_whose_factory_fails_leaves_the_guild_settings_only(
    sessions, factory
):
    guild = FakeGuild(1)

    async def run():
        old = await sessions.get_or_create(guild)
        factory.fail = True
        with pytest.raises(RuntimeError):
            await sessions.reset(guild)
        return old

    old = asyncio.run(run())

    assert old.disposed
    assert sessions.controller(guild) is None
    assert sessions.settings(guild) is not None
