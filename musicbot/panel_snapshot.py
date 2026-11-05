"""`PanelSnapshot`: the controller's read-only state the player panel
draws from. Its own module so the controller can build one without
importing the panel (and so `discord.ui`); `musicbot.player_panel`
re-exports it."""

from dataclasses import dataclass
from typing import Optional

from musicbot.playlist import LoopMode
from musicbot.song import Song


@dataclass(frozen=True)
class PanelSnapshot:
    """What the player buttons show. Named for the panel, not
    `PlayerState`, so it is not confused with `PlaybackState`."""

    active: bool
    paused: bool
    has_previous: bool
    has_next: bool
    queue_empty: bool
    loop_mode: str
    volume: int
    current_song: Optional[Song]

    @classmethod
    def inactive(cls, volume: int = 0) -> "PanelSnapshot":
        """Nothing is playing or paused: the panel shows no buttons."""
        return cls(
            active=False,
            paused=False,
            has_previous=False,
            has_next=False,
            queue_empty=True,
            loop_mode=LoopMode.OFF,
            volume=volume,
            current_song=None,
        )
