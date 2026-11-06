"""The message-link gate: a Discord message URL given as a track is
played only for a member who may read that message in the request's
guild.

The extractor imports `MESSAGE_LINK_PATTERN` at module level and the
loader loads the extractors through `musicbot.linkutils`, so this module
imports nothing from `musicbot.loader` or `musicbot.linkutils` at module
level (`SongError` and `normalize_track` are imported inside the check).
"""

import re

import discord

from config import config

MESSAGE_LINK_PATTERN = (
    r"^https?://(?:canary\.)?discord\.com"
    r"/channels/(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
)

_message_link = re.compile(MESSAGE_LINK_PATTERN)


async def check_message_link(track: str, guild, user) -> None:
    """Does nothing for a track that is not a message link. Otherwise
    raises `SongError(config.MESSAGE_LINK_FORBIDDEN)` unless the link
    points into `guild`, its channel or thread resolves there and `user`
    can view it and read its history (and, for a private thread, is in
    it or can manage threads)."""
    from musicbot.linkutils import normalize_track
    from musicbot.loader import SongError

    match = _message_link.match(normalize_track(track))
    if match is None:
        return
    if not await _may_read(match, guild, user):
        raise SongError(config.MESSAGE_LINK_FORBIDDEN)


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
