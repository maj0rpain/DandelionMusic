"""The message-link gate: a Discord message URL given as a track is
played only for a member who may read that message in the request's
guild.

The extractor imports `MESSAGE_LINK_PATTERN` at module level and the
loader loads the extractors through `musicbot.linkutils`, so this module
imports nothing from `musicbot.loader` or `musicbot.linkutils` at module
level (`SongError` and `normalize_track` are imported inside the check).
"""

import re
from dataclasses import dataclass, field
from typing import Optional

import discord

from config import config

MESSAGE_LINK_PATTERN = (
    r"^https?://(?:canary\.)?discord\.com"
    r"/channels/(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
)

_message_link = re.compile(MESSAGE_LINK_PATTERN)


@dataclass(frozen=True)
class CheckedTrack:
    """A track the gate let through: `track` as the member typed it,
    plus `admitted_link`, the (normalized) message link the gate
    admitted when the track is one, else `None`. The constructor takes
    only the track; only `check_message_link` (through `_admitted`)
    gives one an admitted link."""

    track: str
    admitted_link: Optional[str] = field(default=None, init=False)


def _admitted(track: str, link: str) -> CheckedTrack:
    """The one maker of a `CheckedTrack` with an admitted link."""
    checked = CheckedTrack(track)
    object.__setattr__(checked, "admitted_link", link)
    return checked


def _match(track: str):
    """The one message-link match: `track` normalized, and its match
    (`None` when it is not a Discord message URL)."""
    from musicbot.linkutils import normalize_track

    link = normalize_track(track)
    return link, _message_link.match(link)


def is_message_link(track: str) -> bool:
    """The one "is this a message link" rule: whether `track`, once
    normalized, is a Discord message URL."""
    return _match(track)[1] is not None


async def check_message_link(track: str, guild, user) -> CheckedTrack:
    """Returns the track's `CheckedTrack`: with no admitted link for a
    track that is not a message link. Otherwise raises
    `SongError(config.MESSAGE_LINK_FORBIDDEN)` unless the link points
    into `guild`, its channel or thread resolves there and `user` can
    view it and read its history (and, for a private thread, is in it
    or can manage threads); the admitted link is then the link."""
    from musicbot.loader import SongError

    link, match = _match(track)
    if match is None:
        return CheckedTrack(track)
    if not await _may_read(match, guild, user):
        raise SongError(config.MESSAGE_LINK_FORBIDDEN)
    return _admitted(track, link)


async def _may_read(match, guild, user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if int(match.group("guild_id")) != guild.id:
        return False
    channel = guild.get_channel_or_thread(int(match.group("channel_id")))
    if channel is None:
        return False
    perms = channel.permissions_for(user)
    if not (perms.view_channel and perms.read_message_history):
        return False
    is_private = getattr(channel, "is_private", None)
    if is_private is not None and is_private():
        if perms.manage_threads:
            return True
        try:
            await channel.fetch_member(user.id)
        except discord.HTTPException:
            return False
    return True
