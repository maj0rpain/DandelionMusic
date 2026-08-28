import sys
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4


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


def read_artwork(
    path: Path, max_bytes: int = 5 * 1024 * 1024
) -> Optional[Tuple[bytes, str]]:
    """Best-effort embedded cover art extraction. Never raises - any
    failure, absence, or oversized result (larger than max_bytes, e.g.
    a multi-MB liner-note scan) just yields None. Needs its own
    non-easy-mode parse - easy mode (used by read_tags()) doesn't
    expose embedded pictures for MP3 (EasyID3 has no APIC access)."""
    try:
        audio = MutagenFile(path)
    except Exception as e:
        print(
            f"audiotags: failed to read artwork from {path}: {e}",
            file=sys.stderr,
        )
        return None

    data: Optional[bytes] = None
    mime: Optional[str] = None

    if isinstance(audio, MP3):
        apics = audio.tags.getall("APIC") if audio.tags else []
        if apics:
            data, mime = apics[0].data, apics[0].mime
    elif isinstance(audio, FLAC):
        if audio.pictures:
            data, mime = audio.pictures[0].data, audio.pictures[0].mime
    elif isinstance(audio, MP4):
        covr = audio.tags.get("covr") if audio.tags else None
        if covr:
            cover = covr[0]
            mime = (
                "image/png"
                if cover.imageformat == cover.FORMAT_PNG
                else "image/jpeg"
            )
            data = bytes(cover)

    if not data or not mime or len(data) > max_bytes:
        return None

    return data, mime.rpartition("/")[2]
