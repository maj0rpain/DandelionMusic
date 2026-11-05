"""Request admission (GLOSSARY.md): whether a request may act now, and
the join that follows it.

`check(ctx, kind)` decides and returns an `Admission`; `await
admission.enter()` then joins, only when a join is needed, and returns
the guild's current controller. The checks run before any join, so a
refused request never makes the bot join.
"""

from __future__ import annotations

import asyncio
import enum
import sys
from traceback import print_exc
from typing import TYPE_CHECKING, Optional

import discord

from config import config
from musicbot import utils
from musicbot.utils import (
    CheckError,
    check_voice_permissions,
    connect_to,
    dj_check,
    get_audiocontroller,
    get_settings,
)

if TYPE_CHECKING:
    from musicbot.audiocontroller import AudioController
    from musicbot.bot import Context


class RequestKind(enum.Enum):
    # a plain command: never joins
    COMMAND = enum.auto()
    # a command marked @joining_command: may join
    JOINING_COMMAND = enum.auto()
    # a player button: never joins; refused while the bot is out of
    # voice; the playback buttons need a DJ
    PLAYER_BUTTON = enum.auto()
    # a search or library pick, acting in a joining command's name
    PICK = enum.auto()
    # the button plugin's reaction; admitted as a joining command
    # today, kept apart so its rules can diverge
    REACTION = enum.auto()


_JOINING_KINDS = frozenset(
    {RequestKind.JOINING_COMMAND, RequestKind.PICK, RequestKind.REACTION}
)

# the custom ids of the player's playback buttons, which need a DJ
_DJ_BUTTONS = frozenset(
    {
        "prev",
        "pause",
        "next",
        "loop",
        "shuffle",
        "stop",
        "volume_down",
        "volume_up",
    }
)


def _check_command_channel(ctx: Context, sett):
    """Refuses a request made outside the guild's command channel,
    when one is set."""
    cm_channel = sett.command_channel
    if cm_channel is not None and int(cm_channel) != ctx.channel.id:
        raise CheckError(config.WRONG_CHANNEL_MESSAGE)


async def _voice_check(ctx: Context):
    """Check if the user can use the bot now. Only refuses: a bot
    sitting in a channel with only bots admits the user, and the join
    moves it later."""
    bot_vc = ctx.guild.voice_client
    if not bot_vc:
        # the bot is free
        return True

    author_voice = ctx.author.voice
    if author_voice:
        if author_voice.channel == bot_vc.channel:
            return True

        if all(m.bot for m in bot_vc.channel.members):
            # current channel doesn't have any user in it
            return True

    # Admins and the application owner always pass; a DJ passes only
    # when the guild has set a DJ role and the member holds it. With no
    # DJ role set, nobody else bypasses (EXTRA_OWNERS included).
    if await utils._admin_or_owner(ctx):
        return True
    if utils._holds_role(ctx, get_settings(ctx).dj_role):
        return True

    raise CheckError(config.USER_NOT_IN_VC_MESSAGE)


def _marked_joining(ctx: Context) -> bool:
    """Whether ctx's command is a joining command. A context with no
    command (a button's) reads as one: it decides in its own code
    whether to ask at all."""
    command = getattr(ctx, "command", None)
    return command is None or getattr(
        command.callback, "_joining_command", False
    )


def _join_needed(ctx: Context) -> bool:
    """Whether the bot has to join the user's voice channel: it has no
    voice client (a connect), or user_must_be_in_vc is on, its channel
    has only bots and the user is in another one (a move)."""
    bot_vc = ctx.guild.voice_client
    if not bot_vc:
        return True
    if not get_settings(ctx).user_must_be_in_vc:
        return False
    author_voice = ctx.author.voice
    return (
        author_voice is not None
        and author_voice.channel != bot_vc.channel
        and all(m.bot for m in bot_vc.channel.members)
    )


def _join_target(ctx: Context) -> Optional[discord.VoiceChannel]:
    """The channel the bot has to join or move to, or None when no
    join is needed. Raises CheckError (USER_NOT_IN_VC_MESSAGE) when a
    join is needed but the author is not in voice."""
    if not _join_needed(ctx):
        return None
    author_voice = ctx.author.voice
    if not author_voice:
        raise CheckError(config.USER_NOT_IN_VC_MESSAGE)
    return author_voice.channel


async def _player_check(ctx: Context):
    """Refuses a player button that may not run: never one while the
    bot has no voice client."""
    if not ctx.guild.voice_client:
        raise CheckError(config.NOT_CONNECTED_MESSAGE)

    sett = get_settings(ctx)
    _check_command_channel(ctx, sett)

    if sett.user_must_be_in_vc:
        return await _voice_check(ctx)

    return True


async def _connect(ctx: Context, target: discord.VoiceChannel):
    """Connects or moves the bot to `target`. A failed connect or
    move is logged and becomes a CheckError."""
    try:
        await connect_to(ctx.guild, target)
    except (asyncio.TimeoutError, discord.ClientException):
        print_exc(file=sys.stderr)
        raise CheckError(config.VOICE_CONNECT_FAILED)


class Admission:
    """The decision `check` made for one request."""

    def __init__(
        self,
        ctx: Context,
        audiocontroller: AudioController,
        target: Optional[discord.VoiceChannel],
    ):
        self._ctx = ctx
        # the guild's controller when the request was checked
        self.audiocontroller = audiocontroller
        self._target = target

    @property
    def join_needed(self) -> bool:
        return self._target is not None

    async def enter(self) -> AudioController:
        """Joins when a join is needed, and returns the guild's
        current controller."""
        if self._target is not None:
            await _connect(self._ctx, self._target)
        return get_audiocontroller(self._ctx)


async def check(ctx: Context, kind: Optional[RequestKind] = None):
    """Decides whether the request may act now. Raises CheckError on
    refusal; never touches voice. `kind` is None for a command, whose
    kind is read off its @joining_command marker."""
    if kind in (RequestKind.COMMAND, RequestKind.JOINING_COMMAND):
        raise ValueError(f"{kind} is derived from the command, not passed")
    # first, as Music.cog_check always resolved it first
    controller = get_audiocontroller(ctx)
    if kind is None:
        kind = (
            RequestKind.JOINING_COMMAND
            if _marked_joining(ctx)
            else RequestKind.COMMAND
        )
    sett = get_settings(ctx)

    if kind is RequestKind.PLAYER_BUTTON:
        await _player_check(ctx)
        if ctx.interaction.data.get("custom_id") in _DJ_BUTTONS:
            await dj_check(ctx)
        return Admission(ctx, controller, None)

    _check_command_channel(ctx, sett)
    target = None
    if kind in _JOINING_KINDS:
        target = _join_target(ctx)
        if target is not None:
            check_voice_permissions(ctx.guild, target)
    if ctx.guild.voice_client and sett.user_must_be_in_vc:
        await _voice_check(ctx)
    return Admission(ctx, controller, target)
