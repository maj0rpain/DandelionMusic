from dataclasses import dataclass
from enum import Enum
from typing import Optional

import discord

from config import config
from musicbot.song import Song
from musicbot.truncation import Truncation


class OutcomeKind(Enum):
    # this request's song started playing
    STARTED = "started"
    # this request claimed the start, but it did not start
    START_FAILED = "start_failed"
    # the song was placed behind a starting or active track
    QUEUED = "queued"
    # a batch of songs was added as one request
    PLAYLIST = "playlist"
    # files picked in the library browser were added as one request
    LIBRARY_PICK = "library_pick"
    # the bot had left voice before the request's songs arrived
    REFUSED_NOT_IN_VOICE = "refused_not_in_voice"


@dataclass(frozen=True)
class BatchReport:
    """A request that reports as a batch (a PLAYLIST outcome), with the
    `truncation` of a cut-short Spotify playlist, if any."""

    truncation: Optional[Truncation] = None


@dataclass(frozen=True)
class EnqueueOutcome:
    """What adding a request's songs to the queue did. `position`
    (QUEUED only) numbers the queue as `d!queue` does: the current
    track is #1. `skipped` lists, for a LIBRARY_PICK, the inputs that
    could not be loaded (possibly empty)."""

    kind: OutcomeKind
    song: Optional[Song] = None
    count: int = 1
    position: Optional[int] = None
    truncation: Optional[Truncation] = None
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reply:
    """What to send for an outcome: text and/or an embed."""

    text: Optional[str] = None
    embed: Optional[discord.Embed] = None


def _library_report(outcome: EnqueueOutcome) -> str:
    skipped = outcome.skipped
    message = config.LIBRARY_PICK_QUEUED.format(count=outcome.count)
    if skipped:
        message += config.LIBRARY_PICK_SKIPPED.format(
            skipped=len(skipped), names=", ".join(skipped[:5])
        )
        if len(skipped) > 5:
            message += config.LIBRARY_PICK_SKIPPED_MORE.format(
                more=len(skipped) - 5
            )
        # refresh is owner-only, so this can't tell whoever hit it to
        # just run it themselves. Appended as-is: its prefix is already
        # filled in, and may hold braces that format would choke on
        message += config.LIBRARY_PICK_STALE_INDEX
    return message


def render(outcome: EnqueueOutcome, announce_songs: bool) -> Optional[Reply]:
    """The reply for `outcome`, or `None` when it earns none."""
    kind = outcome.kind
    if kind is OutcomeKind.START_FAILED:
        return None
    if kind is OutcomeKind.STARTED:
        if announce_songs:
            # the player announces it itself
            return None
        return Reply(
            embed=outcome.song.format_output(config.SONGINFO_NOW_PLAYING)
        )
    if kind is OutcomeKind.QUEUED:
        title = config.SONGINFO_QUEUE_POSITION.format(
            position=outcome.position
        )
        return Reply(embed=outcome.song.format_output(title))
    if kind is OutcomeKind.REFUSED_NOT_IN_VOICE:
        return Reply(text=config.LEFT_VOICE_MESSAGE)
    if kind is OutcomeKind.LIBRARY_PICK:
        return Reply(text=_library_report(outcome))
    if outcome.truncation is not None:
        return Reply(text=outcome.truncation.notice(outcome.count))
    return Reply(text=config.SONGINFO_PLAYLIST_QUEUED)


async def send_outcome(ctx, outcome: EnqueueOutcome):
    """Sends through `ctx.send` only what `render` returns, announcing
    songs as the context guild's `announce_songs` setting says (off when
    the guild has no settings)."""
    settings = ctx.bot.sessions.settings(ctx.guild)
    announce_songs = settings is not None and settings.announce_songs
    reply = render(outcome, announce_songs)
    if reply is None:
        return
    if reply.embed is not None:
        await ctx.send(reply.text, embed=reply.embed)
    else:
        await ctx.send(reply.text)
