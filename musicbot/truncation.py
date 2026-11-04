from enum import Enum
from dataclasses import dataclass

from config import config


class TruncationReason(Enum):
    """Why a Spotify playlist loaded fewer tracks than it has. Each
    value is the key of the reason's message in the language file."""

    NO_API = "SPOTIFY_TRUNCATED_NO_API"
    API_ERROR = "SPOTIFY_TRUNCATED_API_ERROR"

    @property
    def text(self) -> str:
        # looked up when called, never at import, so the active
        # language's messages are used
        return getattr(config, self.value)


@dataclass(frozen=True)
class Truncation:
    """A Spotify playlist that was cut short: `total` tracks exist, and
    `reason` is why fewer were loaded. Built by `of`, only when tracks
    were left out."""

    total: int
    reason: TruncationReason

    @classmethod
    def of(
        cls, total: int | None, loaded: int, reason: TruncationReason
    ) -> "Truncation | None":
        """A truncation when loading `loaded` of `total` tracks left some
        out, else `None`. No reported total (`None`) means no
        truncation."""
        if total is None:
            return None
        return cls(total, reason) if total > loaded else None

    def notice(self, queued: int) -> str:
        """The "only N of M tracks" message for the user."""
        return config.SPOTIFY_PLAYLIST_TRUNCATED.format(
            queued=queued, total=self.total, reason=self.reason.text
        )
