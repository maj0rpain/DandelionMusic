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
    """A partly fetched Spotify playlist: `total` tracks exist, and
    `reason` is why fewer may have been loaded."""

    total: int
    reason: TruncationReason

    def cuts(self, loaded: int) -> bool:
        """Whether loading `loaded` tracks left some out."""
        return self.total > loaded

    def notice(self, queued: int) -> str:
        """The "only N of M tracks" message for the user."""
        return config.SPOTIFY_PLAYLIST_TRUNCATED.format(
            queued=queued, total=self.total, reason=self.reason.text
        )
