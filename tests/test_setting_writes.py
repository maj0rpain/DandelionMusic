"""Setting writes on a real in-memory database: a change either lands
in the row and in memory or in neither, and concurrent ones serialise."""

import asyncio
import types

from sqlalchemy.exc import OperationalError
from sqlalchemy import select

from musicbot.settings import GuildSettings, SETTINGS, default_settings

from db_seam import FailingSessionFactory, seed_settings, with_db

GUILD = "42"
FAILED_COMMIT = OperationalError("COMMIT", {}, Exception("boom"))


def ctx_for(factory):
    return types.SimpleNamespace(bot=types.SimpleNamespace(DbSession=factory))


async def seeded(factory, guild=GUILD):
    await seed_settings(factory, guild)
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


def test_a_failed_commit_returns_false_and_stores_nothing():
    async def test(factory):
        sett = await seeded(factory)
        failing = FailingSessionFactory(factory, "commit", FAILED_COMMIT)
        ctx = ctx_for(failing)

        assert (
            await sett.update_setting(SETTINGS["announce_songs"], True, ctx)
            is False
        )
        failing.failing = False
        assert (
            await sett.update_setting(SETTINGS["default_volume"], 50, ctx)
            is True
        )

        assert sett.announce_songs is False
        stored = await row(factory)
        assert stored.announce_songs is False
        assert stored.default_volume == 50

    with_db(test)


def test_an_update_for_a_guild_without_a_row_returns_false():
    async def test(factory):
        sett = GuildSettings(guild_id="nope", **default_settings())

        assert (
            await sett.update_setting(
                SETTINGS["default_volume"], 50, ctx_for(factory)
            )
            is False
        )
        assert sett.default_volume == 100

    with_db(test)


def test_a_failed_reset_returns_false_and_stores_nothing():
    async def test(factory):
        sett = await seeded(factory)
        ctx = ctx_for(factory)
        assert (
            await sett.update_setting(SETTINGS["default_volume"], 50, ctx)
            is True
        )

        assert (
            await sett.reset_setting(
                SETTINGS["default_volume"],
                ctx_for(
                    FailingSessionFactory(factory, "commit", FAILED_COMMIT)
                ),
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
            sett.update_setting(SETTINGS["announce_songs"], True, ctx),
            sett.update_setting(SETTINGS["default_volume"], 30, ctx),
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
            sett.update_setting(SETTINGS["default_volume"], 10, ctx)
        )
        await first_committing.wait()
        second = asyncio.create_task(
            sett.update_setting(SETTINGS["default_volume"], 20, ctx)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()

        assert await asyncio.gather(first, second) == [True, True]
        assert sett.default_volume == (await row(factory)).default_volume == 20

    with_db(test)
