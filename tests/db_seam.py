"""An in-memory database with the real schema, for tests.

Shared by the saved-playlists tests and the AudioController tests, so
the StaticPool detail lives in one place. It also holds the
real-`MusicBot` harness (`run_musicbot`, `MusicBotRun`) that the
`on_ready` and whitelist-admission tests share.
"""

import asyncio
import contextlib
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from helpers import run


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


class MusicBotRun:
    """What `run_musicbot` saw: the ids `register` was called with, the
    panel-refresh starts, and whether `absolutely_ready` is done."""

    def __init__(self):
        self.registered = []
        self.started = []
        self.ready = False


def run_musicbot(
    monkeypatch,
    listed=(),
    guilds=None,
    scenario=None,
    fail_register=(),
    fail_settings=False,
):
    """Build a real `MusicBot` over an in-memory database whitelisting the
    ids in `listed`, and run `await scenario(bot)` (default
    `bot.on_ready()`). `guilds`, when given, is patched onto
    `MusicBot.guilds`. `register` is recorded and raises for an id in
    `fail_register`; `sessions.load_settings` raises when `fail_settings`.
    The panel-refresh loop reads as not running and its `start` is
    recorded. Returns a `MusicBotRun`."""
    import discord

    from musicbot import settings
    from musicbot.bot import MusicBot

    if guilds is not None:
        monkeypatch.setattr(MusicBot, "guilds", property(lambda self: guilds))
    out = MusicBotRun()

    async def go():
        async with in_memory_db(settings) as factory:
            await seed_whitelist(factory, *listed)
            bot = MusicBot(
                [], command_prefix="d!", intents=discord.Intents.default()
            )
            bot.DbSession = factory
            bot.absolutely_ready = asyncio.get_running_loop().create_future()

            async def register(guild):
                if guild.id in fail_register:
                    raise RuntimeError("db down")
                out.registered.append(guild.id)

            async def load_settings(guilds):
                if fail_settings:
                    raise RuntimeError("settings down")

            monkeypatch.setattr(bot, "register", register)
            monkeypatch.setattr(bot.sessions, "load_settings", load_settings)
            panels = type(bot.refresh_panels)
            monkeypatch.setattr(panels, "is_running", lambda self: False)
            monkeypatch.setattr(
                panels, "start", lambda self: out.started.append(True)
            )
            await (scenario or (lambda b: b.on_ready()))(bot)
            out.ready = bot.absolutely_ready.done()

    run(go())
    return out
