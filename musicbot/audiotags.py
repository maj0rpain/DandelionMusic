import sys
from pathlib import Path
from typing import NamedTuple, Optional

from mutagen import File as MutagenFile


class AudioTags(NamedTuple):
    title: Optional[str]
    artist: Optional[str]
    duration: Optional[int]
    album: Optional[str]
    album_artist: Optional[str]


def read_tags(path: Path) -> AudioTags:
    """Best-effort tag read for a local library file. Never raises -
    an unreadable/corrupt file or a format mutagen can't parse just
    yields all-None, so callers fall back to filename-derived data."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as e:
        print(f"audiotags: failed to read {path}: {e}", file=sys.stderr)
        return AudioTags(
            title=None,
            artist=None,
            duration=None,
            album=None,
            album_artist=None,
        )

    if audio is None:
        return AudioTags(
            title=None,
            artist=None,
            duration=None,
            album=None,
            album_artist=None,
        )

    title = (audio.get("title") or [None])[0]
    artist = (audio.get("artist") or [None])[0]
    album = (audio.get("album") or [None])[0]
    album_artist = (audio.get("albumartist") or [None])[0]
    duration = (
        round(audio.info.length)
        if audio.info is not None and audio.info.length
        else None
    )

    return AudioTags(
        title=title,
        artist=artist,
        duration=duration,
        album=album,
        album_artist=album_artist,
    )


def nice_title(tags: AudioTags, fallback: str) -> str:
    if tags.artist and tags.title:
        return f"{tags.artist} - {tags.title}"
    if tags.title:
        return tags.title
    return fallback
