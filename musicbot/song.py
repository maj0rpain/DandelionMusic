from __future__ import annotations
import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Union

import discord

from config import config
from musicbot.duration import whole_seconds
from musicbot.linkutils import SiteTypes
from musicbot.truncation import Truncation

if TYPE_CHECKING:
    # type-only: song -> playlists -> settings -> utils -> song is an
    # import cycle, so an unconditional import fails at import time
    from musicbot.playlists import SavedEntry


class Song:
    # class-level, so a Song unpickled from a queue backup written
    # before songs carried a SavedEntry reads as having none
    saved_entry: Optional[SavedEntry] = None
    # when the loader extracted this song's stream url; class-level for
    # the same reason - a song from an old backup reads as unstamped
    extracted_at: Optional[datetime.datetime] = None

    def __init__(
        self,
        host: SiteTypes,
        webpage_url: str,
        url: Optional[str] = None,
        title: Optional[str] = None,
        uploader: Optional[str] = None,
        duration: Optional[int] = None,
        thumbnail: Optional[str] = None,
        saved_entry: Optional[SavedEntry] = None,
    ):
        self.host = host
        self.webpage_url = webpage_url
        self.url = url
        self.title = title
        self.uploader = uploader
        self.duration = duration
        self.thumbnail = thumbnail
        self.saved_entry = saved_entry

    @property
    def duration(self) -> Optional[int]:
        """Whole seconds, or None when unknown. Read through the rule
        too, so a song restored from an old backup (whose instance dict
        holds the raw value) reads correctly."""
        return whole_seconds(self.__dict__.get("duration"))

    @duration.setter
    def duration(self, value: Optional[float]) -> None:
        self.__dict__["duration"] = whole_seconds(value)

    @property
    def display_name(self) -> str:
        """The title, else the webpage URL: how a song is named to a
        user or in history."""
        return self.title or self.webpage_url

    def format_output(self, playtype: str) -> discord.Embed:
        if self.host == SiteTypes.LOCAL_LIBRARY:
            # file:// isn't a scheme Discord renders as a clickable
            # link in embed markdown - show plain title text instead of
            # a dead-looking [title](file://...) link.
            description = self.display_name
        else:
            description = "[{}]({})".format(
                self.display_name, self.webpage_url
            )
        embed = discord.Embed(
            title=playtype,
            description=description,
            color=config.EMBED_COLOR,
        )

        if self.thumbnail is not None:
            embed.set_thumbnail(url=self.thumbnail)

        embed.add_field(
            name=config.SONGINFO_UPLOADER,
            value=self.uploader or config.SONGINFO_UNKNOWN,
            inline=False,
        )

        embed.add_field(
            name=config.SONGINFO_DURATION,
            value=(
                str(datetime.timedelta(seconds=self.duration))
                if self.duration is not None
                else config.SONGINFO_UNKNOWN
            ),
            inline=False,
        )

        return embed

    def update(self, data: Union[dict, "Song"]):
        if isinstance(data, Song):
            data = data.__dict__

        thumbnails = data.get("thumbnails")
        if thumbnails:
            # last thumbnail has the best resolution
            data["thumbnail"] = thumbnails[-1]["url"]

        for k, v in data.items():
            if hasattr(self, k) and v:
                setattr(self, k, v)


@dataclass
class LoadResult:
    """What loading one input produced: `songs`; `single`, when the input
    named one track rather than a playlist (a truncated playlist is never
    single); and `truncation`, when a Spotify playlist was cut short.
    Module-level so it pickles across the worker process boundary."""

    songs: List[Song]
    single: bool
    truncation: Optional[Truncation] = None

    @classmethod
    def of_song(cls, song: Song) -> "LoadResult":
        """The result of an input that named one track."""
        return cls([song], True)
