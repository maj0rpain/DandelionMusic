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


@dataclass(frozen=True)
class EnqueueOutcome:
    """What adding a request's songs to the queue did. `position`
    (QUEUED only) numbers the queue as `d!queue` does: the current
    track is #1. `skipped` is `None` except for a library pick's, where
    it lists the inputs that could not be loaded (possibly empty)."""

    kind: OutcomeKind
    song: Optional[Song] = None
    count: int = 1
    position: Optional[int] = None
    truncation: Optional[Truncation] = None
    skipped: Optional[list[str]] = None


@dataclass(frozen=True)
class Reply:
    """What to send for an outcome: text and/or an embed."""

    text: Optional[str] = None
    embed: Optional[discord.Embed] = None


def _library_report(outcome: EnqueueOutcome) -> str:
    skipped = outcome.skipped or []
    message = f"Queued {outcome.count} song(s)."
    if skipped:
        shown = ", ".join(skipped[:5])
        message += f" {len(skipped)} skipped (file not found): {shown}"
        if len(skipped) > 5:
            message += f", and {len(skipped) - 5} more"
        # refresh is owner-only, so this can't tell whoever hit it to
        # just run it themselves
        message += ". The index may be stale - ask the bot owner to run"
        message += " `d!lib refresh`."
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
    if outcome.skipped is not None:
        return Reply(text=_library_report(outcome))
    if outcome.truncation is not None:
        return Reply(text=outcome.truncation.notice(outcome.count))
    return Reply(text=config.SONGINFO_PLAYLIST_QUEUED)
