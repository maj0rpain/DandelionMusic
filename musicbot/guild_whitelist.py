"""The guild whitelist: which guilds the bot may stay in.

Every function takes a session factory (the bot's ``DbSession``), never
the bot, and the module deals only in guild ids, like
``musicbot.playlists``. The ``WhitelistedGuild`` and ``BotState`` tables
stay in ``musicbot.settings`` with the rest of the schema.
"""

import enum
from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterable, Iterator, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
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
        return Whitelist(await _listed(session))


class Outcome(enum.Enum):
    ALREADY_LISTED = "already_listed"
    NOT_LISTED = "not_listed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UPDATED = "updated"
    WOULD_LEAVE_HERE = "would_leave_here"


@dataclass(frozen=True)
class WhitelistChange:
    """What a change meant: its outcome, and the present guilds the
    whitelist admitted before it and does not admit after it."""

    outcome: Outcome
    to_leave: Tuple[int, ...] = ()


async def _listed(session) -> FrozenSet[int]:
    rows = (
        (await session.execute(select(WhitelistedGuild.guild_id)))
        .scalars()
        .all()
    )
    return frozenset(int(guild_id) for guild_id in rows)


def _to_leave(
    before: FrozenSet[int], after: FrozenSet[int], present: Iterable[int]
) -> Tuple[int, ...]:
    was, now = Whitelist(before), Whitelist(after)
    return tuple(
        guild_id
        for guild_id in present
        if was.admits(guild_id) and not now.admits(guild_id)
    )


async def add(
    session_factory: SessionFactory,
    guild_id: int,
    present_guild_ids: Iterable[int],
    protect: Optional[int] = None,
) -> WhitelistChange:
    """List a guild. ``protect`` is the guild the command was sent from
    (``None`` from a DM): an add that would make the bot leave it is
    refused and rolled back."""
    present = list(present_guild_ids)
    async with session_factory() as session:
        if guild_id in await _listed(session):
            return WhitelistChange(Outcome.ALREADY_LISTED)
        session.add(WhitelistedGuild(guild_id=str(guild_id)))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return WhitelistChange(Outcome.ALREADY_LISTED)
        # Decided from the read after our own write, inside the write
        # transaction, so a concurrent add cannot also see an empty list.
        after = await _listed(session)
        before = after - {guild_id}
        to_leave = _to_leave(before, after, present)
        if protect is not None and protect in to_leave:
            await session.rollback()
            return WhitelistChange(Outcome.WOULD_LEAVE_HERE)
        await session.commit()
    outcome = Outcome.UPDATED if before else Outcome.ENABLED
    return WhitelistChange(outcome, to_leave)


async def remove(
    session_factory: SessionFactory,
    guild_id: int,
    present_guild_ids: Iterable[int],
) -> WhitelistChange:
    """Unlist a guild. Removing the last id disables the whitelist."""
    present = list(present_guild_ids)
    async with session_factory() as session:
        result = await session.execute(
            delete(WhitelistedGuild).where(
                WhitelistedGuild.guild_id == str(guild_id)
            )
        )
        if result.rowcount == 0:
            await session.rollback()
            return WhitelistChange(Outcome.NOT_LISTED)
        # Decided from our own DELETE and the read after it, so two
        # concurrent removes of one id cannot both report a change.
        after = await _listed(session)
        before = after | {guild_id}
        await session.commit()
    outcome = Outcome.UPDATED if after else Outcome.DISABLED
    return WhitelistChange(outcome, _to_leave(before, after, present))


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
        existing = await _listed(session)
        for guild_id in config.GUILD_WHITELIST:
            if int(guild_id) not in existing:
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
