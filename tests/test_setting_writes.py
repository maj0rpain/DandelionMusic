"""Setting writes on a real in-memory database: a change either lands
in the row and in memory or in neither, and concurrent ones serialise."""

import asyncio
import types

from sqlalchemy.exc import OperationalError
from sqlalchemy import select

from musicbot import settings
from musicbot.settings import GuildSettings, default_settings

from db_seam import in_memory_db
from helpers import run

GUILD = "42"


def ctx_for(factory):
    return types.SimpleNamespace(bot=types.SimpleNamespace(DbSession=factory))


async def seeded(factory, guild=GUILD):
    async with factory() as session:
        sett = GuildSettings(guild_id=guild, **default_settings())
        session.add(sett)
        await session.commit()
    async with factory() as session:
        return (
            await session.execute(
                select(GuildSettings).where(GuildSettings.guild_id == guild)
            )
        ).scalar_one()


async def row(factory, guild=GUILD):
    async with factory() as session:
        return (
            await session.execute(
                select(GuildSettings)
                .where(GuildSettings.guild_id == guild)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()


def with_db(test):
    async def main():
        async with in_memory_db(settings) as factory:
            return await test(factory)

    return run(main())


class FailingCommit:
    "A session factory whose commits raise while `failing` is set"

    def __init__(self, factory):
        self.factory = factory
        self.failing = True

    def __call__(self):
        session = self.factory()
        commit = session.commit

        async def commit_or_fail():
            if self.failing:
                raise OperationalError("COMMIT", {}, Exception("boom"))
            await commit()

        session.commit = commit_or_fail
        return session


def test_a_failed_commit_returns_false_and_stores_nothing():
    async def test(factory):
        sett = await seeded(factory)
        failing = FailingCommit(factory)
        ctx = ctx_for(failing)

        assert await sett.update_setting("announce_songs", True, ctx) is False
        failing.failing = False
        assert await sett.update_setting("default_volume", 50, ctx) is True

        assert sett.announce_songs is False
        stored = await row(factory)
        assert stored.announce_songs is False
        assert stored.default_volume == 50

    with_db(test)


def test_an_update_for_a_guild_without_a_row_returns_false():
    async def test(factory):
        sett = GuildSettings(guild_id="nope", **default_settings())

        assert (
            await sett.update_setting("default_volume", 50, ctx_for(factory))
            is False
        )
        assert sett.default_volume == 100

    with_db(test)


def test_a_failed_reset_returns_false_and_stores_nothing():
    async def test(factory):
        sett = await seeded(factory)
        ctx = ctx_for(factory)
        assert await sett.update_setting("default_volume", 50, ctx) is True

        assert (
            await sett.reset_setting(
                "default_volume", ctx_for(FailingCommit(factory))
            )
            is False
        )

        assert sett.default_volume == 50
        assert (await row(factory)).default_volume == 50

    with_db(test)


def test_gathered_updates_of_different_settings_both_persist():
    async def test(factory):
        sett = await seeded(factory)
        ctx = ctx_for(factory)

        results = await asyncio.gather(
            sett.update_setting("announce_songs", True, ctx),
            sett.update_setting("default_volume", 30, ctx),
        )

        assert results == [True, True]
        stored = await row(factory)
        assert (stored.announce_songs, stored.default_volume) == (True, 30)
        assert (sett.announce_songs, sett.default_volume) == (True, 30)

    with_db(test)


def test_concurrent_updates_of_one_setting_leave_memory_equal_to_the_row():
    async def test(factory):
        sett = await seeded(factory)
        first_committing = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        def holding():
            nonlocal calls
            calls += 1
            session = factory()
            if calls == 1:
                commit = session.commit

                async def slow_commit():
                    first_committing.set()
                    await release.wait()
                    await commit()

                session.commit = slow_commit
            return session

        ctx = ctx_for(holding)
        first = asyncio.create_task(
            sett.update_setting("default_volume", 10, ctx)
        )
        await first_committing.wait()
        second = asyncio.create_task(
            sett.update_setting("default_volume", 20, ctx)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()

        assert await asyncio.gather(first, second) == [True, True]
        assert sett.default_volume == (await row(factory)).default_volume == 20

    with_db(test)
