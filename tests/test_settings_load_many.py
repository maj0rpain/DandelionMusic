"""GuildSettings.load_many tolerates another loader creating a row
between its select and its insert."""

import types

import pytest
from sqlalchemy.exc import IntegrityError

from musicbot import settings
from musicbot.settings import GuildSettings, default_settings
from db_seam import in_memory_db
from helpers import run


class FakeGuild:
    def __init__(self, id):
        self.id = id


def racing_bot(factory, racers):
    """A bot whose first session, after its first select, has `racers`
    inserted by someone else (committed through the plain factory)."""
    state = {"n": 0}

    def make():
        session = factory()
        real = session.execute

        async def execute(*a, **kw):
            result = await real(*a, **kw)
            state["n"] += 1
            if state["n"] == 1:
                async with factory() as other:
                    for gid in racers:
                        other.add(
                            GuildSettings(guild_id=gid, **default_settings())
                        )
                    await other.commit()
            return result

        session.execute = execute
        return session

    return types.SimpleNamespace(DbSession=make)


async def seed(factory, *ids):
    async with factory() as s:
        for i in ids:
            s.add(GuildSettings(guild_id=str(i), **default_settings()))
        await s.commit()


def test_mixed_request_returns_all_in_input_order():
    async def go():
        async with in_memory_db(settings) as factory:
            await seed(factory, 3)
            bot = racing_bot(factory, ["2"])
            guilds = [FakeGuild(2), FakeGuild(1), FakeGuild(3)]
            result = await GuildSettings.load_many(bot, guilds)
            assert [s.guild_id for s in result.values()] == ["2", "1", "3"]
            assert list(result) == guilds

    run(go())


def test_second_integrity_error_propagates_without_third_try():
    async def go():
        async with in_memory_db(settings) as factory:
            attempts = {"n": 0}
            real = factory

            def make():
                session = real()

                async def failing_commit():
                    attempts["n"] += 1
                    raise IntegrityError("x", {}, Exception())

                session.commit = failing_commit
                return session

            bot = types.SimpleNamespace(DbSession=make)
            with pytest.raises(IntegrityError):
                await GuildSettings.load_many(bot, [FakeGuild(7)])
            assert attempts["n"] == 2

    run(go())


def test_load_is_gone():
    assert not hasattr(GuildSettings, "load")
