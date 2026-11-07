"""GuildSettings.load_many tolerates another loader creating a row
between its select and its insert."""

import types

import pytest
from sqlalchemy.exc import IntegrityError

from musicbot.settings import GuildSettings
from db_seam import FailingCommit, seed_settings, with_db


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
                await seed_settings(factory, *racers)
            return result

        session.execute = execute
        return session

    return types.SimpleNamespace(DbSession=make)


def test_mixed_request_returns_all_in_input_order():
    async def test(factory):
        await seed_settings(factory, 3)
        bot = racing_bot(factory, ["2"])
        guilds = [FakeGuild(2), FakeGuild(1), FakeGuild(3)]
        result = await GuildSettings.load_many(bot, guilds)
        assert [s.guild_id for s in result.values()] == ["2", "1", "3"]
        assert list(result) == guilds

    with_db(test)


def test_second_integrity_error_propagates_without_third_try():
    async def test(factory):
        failing = FailingCommit(
            factory, lambda: IntegrityError("x", {}, Exception())
        )
        bot = types.SimpleNamespace(DbSession=failing)
        with pytest.raises(IntegrityError):
            await GuildSettings.load_many(bot, [FakeGuild(7)])
        assert failing.attempts == 2

    with_db(test)


def test_load_is_gone():
    assert not hasattr(GuildSettings, "load")
