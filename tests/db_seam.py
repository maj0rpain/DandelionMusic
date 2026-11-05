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
