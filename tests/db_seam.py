"""An in-memory database with the real schema, for tests.

Shared by the saved-playlists tests and the AudioController tests, so
the StaticPool detail lives in one place.
"""

import contextlib
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@contextlib.asynccontextmanager
async def in_memory_db(settings) -> AsyncIterator[sessionmaker]:
    """Yields an AsyncSession sessionmaker over a fresh in-memory sqlite
    database migrated by `settings.run_migrations`; disposes it on
    exit. StaticPool keeps the single connection the in-memory database
    lives in alive across sessions."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(settings.run_migrations)
        yield sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def seed_whitelist(factory, *ids):
    """Commit a `WhitelistedGuild` row for each id."""
    from musicbot.settings import WhitelistedGuild

    async with factory() as session:
        for guild_id in ids:
            session.add(WhitelistedGuild(guild_id=str(guild_id)))
        await session.commit()


async def seed_settings(factory, *ids):
    """Commit a default `GuildSettings` row for each id."""
    from musicbot.settings import GuildSettings, default_settings

    async with factory() as session:
        for guild_id in ids:
            session.add(
                GuildSettings(guild_id=str(guild_id), **default_settings())
            )
        await session.commit()


def with_db(test):
    """Run `test(factory)` in one event loop against a fresh in-memory
    database and return its result."""
    from helpers import run
    from musicbot import settings

    async def main():
        async with in_memory_db(settings) as factory:
            return await test(factory)

    return run(main())


class FailingCommit:
    """Wraps a session factory so each session's `commit` raises `error`
    (an exception instance, or a zero-argument callable making one)
    while `failing` is true, otherwise commits. Counts the attempts."""

    def __init__(self, factory, error):
        self.factory = factory
        self.error = error
        self.failing = True
        self.attempts = 0

    def __call__(self):
        session = self.factory()
        commit = session.commit

        async def commit_or_fail():
            self.attempts += 1
            if self.failing:
                raise self.error() if callable(self.error) else self.error
            await commit()

        session.commit = commit_or_fail
        return session


class FailingSessionFactory:
    """Wraps a real sessionmaker; the named session method (`"commit"`
    or `"flush"`) of every session it makes raises `error`."""

    def __init__(self, inner, method, error):
        self._inner = inner
        self._method = method
        self._error = error

    def __call__(self):
        session = self._inner()
        setattr(session, self._method, self._fail)
        return session

    async def _fail(self, *args, **kwargs):
        raise self._error
