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
    from musicbot.settings import GuildSettings


class RequestKind(enum.Enum):
    # a plain command: never joins
    COMMAND = enum.auto()
    # a command marked @joining_command: may join
    JOINING_COMMAND = enum.auto()
    # a player button: never joins; refused while the bot is out of
    # voice
    PLAYER_BUTTON = enum.auto()
    # a player button that also needs a DJ
    DJ_PLAYER_BUTTON = enum.auto()
    # a search or library pick, acting in a joining command's name
    PICK = enum.auto()
    # the button plugin's reaction; admitted as a joining command
    # today, kept apart so its rules can diverge
    REACTION = enum.auto()


_JOINING_KINDS = frozenset(
    {RequestKind.JOINING_COMMAND, RequestKind.PICK, RequestKind.REACTION}
)

_PLAYER_KINDS = frozenset(
    {RequestKind.PLAYER_BUTTON, RequestKind.DJ_PLAYER_BUTTON}
)

# the kinds whose kind is derived from the command, never passed
COMMAND_KINDS = frozenset({RequestKind.COMMAND, RequestKind.JOINING_COMMAND})


def _check_command_channel(ctx: Context, sett: GuildSettings):
    """Refuses a request made outside the guild's command channel,
    when one is set."""
    cm_channel = sett.command_channel
    if cm_channel is not None and int(cm_channel) != ctx.channel.id:
        raise CheckError(config.WRONG_CHANNEL_MESSAGE)


def _bot_alone(bot_vc: discord.VoiceProtocol) -> bool:
    """Whether the bot's channel has only bots in it."""
    return all(m.bot for m in bot_vc.channel.members)


async def _voice_check(ctx: Context, sett: GuildSettings):
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

        if _bot_alone(bot_vc):
            # current channel doesn't have any user in it
            return True

    # Admins and the application owner always pass; a DJ passes only
    # when the guild has set a DJ role and the member holds it. With no
    # DJ role set, nobody else bypasses (EXTRA_OWNERS included).
    if await utils.admin_or_owner(ctx):
        return True
    if utils.holds_role(ctx, sett.dj_role):
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


class Join(enum.Enum):
    """The join a request will make."""

    # the bot has no voice client and a join is needed
    CONNECT = enum.auto()
    # the bot has a voice client and a join is needed
    MOVE = enum.auto()
    # no join is needed
    NONE = enum.auto()


def _join_kind(ctx: Context) -> Join:
    """The join the bot has to make into the user's voice channel: a
    connect when it has no voice client, a move when its channel has
    only bots and the user is in another one, whatever
    user_must_be_in_vc says."""
    bot_vc = ctx.guild.voice_client
    if not bot_vc:
        return Join.CONNECT
    author_voice = ctx.author.voice
    if (
        author_voice is not None
        and author_voice.channel != bot_vc.channel
        and _bot_alone(bot_vc)
    ):
        return Join.MOVE
    return Join.NONE


def _join_target(ctx: Context, join: Join) -> Optional[discord.VoiceChannel]:
    """The channel the bot has to join or move to, or None when no
    join is needed. Raises CheckError (USER_NOT_IN_VC_MESSAGE) when a
    join is needed but the author is not in voice."""
    if join is Join.NONE:
        return None
    author_voice = ctx.author.voice
    if not author_voice:
        raise CheckError(config.USER_NOT_IN_VC_MESSAGE)
    return author_voice.channel


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
        join: Join,
    ):
        self._ctx = ctx
        # the guild's controller when the request was checked
        self.audiocontroller = audiocontroller
        self._target = target
        # the join enter() will make
        self.join = join

    @property
    def join_needed(self) -> bool:
        return self.join is not Join.NONE

    async def enter(self) -> AudioController:
        """Joins when a join is needed, and returns the guild's
        current controller."""
        if self.join_needed:
            assert self._target is not None
            await _connect(self._ctx, self._target)
        return get_audiocontroller(self._ctx)


async def check(
    ctx: Context,
    kind: Optional[RequestKind] = None,
    *,
    allow_disconnecting: bool = False,
):
    """Decides whether the request may act now. Raises CheckError on
    refusal; never touches voice. `kind` is None for a command, whose
    kind is read off its @joining_command marker. A joining kind is
    refused while the controller is disconnecting, unless
    `allow_disconnecting` (d!reset's first check, which recovers)."""
    if kind in COMMAND_KINDS:
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

    if kind in _PLAYER_KINDS and not ctx.guild.voice_client:
        raise CheckError(config.NOT_CONNECTED_MESSAGE)
    _check_command_channel(ctx, sett)
    if (
        kind in _JOINING_KINDS
        and not allow_disconnecting
        and controller.is_disconnecting()
    ):
        raise CheckError(config.DISCONNECTING_MESSAGE)
    target = None
    join = Join.NONE
    if kind in _JOINING_KINDS:
        join = _join_kind(ctx)
        target = _join_target(ctx, join)
        if target is not None:
            check_voice_permissions(ctx.guild, target)
    if ctx.guild.voice_client and sett.user_must_be_in_vc:
        await _voice_check(ctx, sett)
    if kind is RequestKind.DJ_PLAYER_BUTTON:
        await dj_check(ctx)
    return Admission(ctx, controller, target, join)
