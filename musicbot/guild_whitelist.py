"""The guild whitelist: which guilds the bot may stay in.

Every function takes a session factory (the bot's ``DbSession``), never
the bot, and the module deals only in guild ids, like
``musicbot.playlists``. The ``WhitelistedGuild`` and ``BotState`` tables
stay in ``musicbot.settings`` with the rest of the schema.
"""

from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import config
from musicbot.settings import (
    ENV_WHITELIST_IMPORTED,
    BotState,
    WhitelistedGuild,
)

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class Whitelist:
    """A snapshot of the listed guild ids. ``admits`` is the one place
    the admission rule is written."""

    ids: FrozenSet[int] = frozenset()

    @property
    def enabled(self) -> bool:
        return bool(self.ids)

    def admits(self, guild_id: int) -> bool:
        """An empty whitelist admits every guild, otherwise only the
        listed ones."""
        return not self.ids or guild_id in self.ids

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(self.ids))


async def load(session_factory: SessionFactory) -> Whitelist:
    """Read the whitelist from the database, once.

    Read on every use rather than cached: a cache would be a second
    copy to keep in step with the commands that edit the table.
    """
    async with session_factory() as session:
        rows = (
            (await session.execute(select(WhitelistedGuild.guild_id)))
            .scalars()
            .all()
        )
    return Whitelist(frozenset(int(guild_id) for guild_id in rows))


async def import_env_whitelist(session_factory: SessionFactory):
    """Seed the whitelist table from the deprecated GUILD_WHITELIST
    environment variable, once.

    Guarded by a marker row rather than by the table being empty:
    emptying the whitelist through d!guild_whitelist is a legitimate
    state, and re-importing whenever the table is empty would resurrect
    every id on the next restart - the same failure the move to the
    database is meant to end.
    """
    async with session_factory() as session:
        if await session.get(BotState, ENV_WHITELIST_IMPORTED) is not None:
            return
        # Skip ids the table already holds rather than assuming a
        # missing marker means an empty table. Two processes sharing
        # one database can both pass the check above, and a restored
        # backup can have rows without the marker; inserting blindly
        # then raises IntegrityError out of MusicBot.start() and the
        # bot never logs in.
        existing = set(
            (await session.execute(select(WhitelistedGuild.guild_id)))
            .scalars()
            .all()
        )
        for guild_id in config.GUILD_WHITELIST:
            if str(guild_id) not in existing:
                session.add(WhitelistedGuild(guild_id=str(guild_id)))
        session.add(
            BotState(
                key=ENV_WHITELIST_IMPORTED,
                value=str(len(config.GUILD_WHITELIST)),
            )
        )
        await session.commit()
    if config.GUILD_WHITELIST:
        print(
            f"Imported {len(config.GUILD_WHITELIST)} guild(s) from the"
            " GUILD_WHITELIST environment variable into the database."
            " That variable is no longer read after this point - manage"
            " the whitelist with d!guild_whitelist and remove it from"
            " your .env."
        )
