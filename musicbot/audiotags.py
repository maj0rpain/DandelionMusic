import sys
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4


class AudioTags(NamedTuple):
    title: Optional[str]
    artist: Optional[str]
    duration: Optional[int]
    album: Optional[str]
    album_artist: Optional[str]


_EMPTY_TAGS = AudioTags(
    title=None,
    artist=None,
    duration=None,
    album=None,
    album_artist=None,
)


def read_tags(path: Path) -> AudioTags:
    """Best-effort tag read for a local library file. Never raises -
    an unreadable/corrupt file or a format mutagen can't parse just
    yields all-None, so callers fall back to filename-derived data."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as e:
        print(f"audiotags: failed to read {path}: {e}", file=sys.stderr)
        return _EMPTY_TAGS

    if audio is None:
        return _EMPTY_TAGS

    try:
        title = (audio.get("title") or [None])[0]
        artist = (audio.get("artist") or [None])[0]
        album = (audio.get("album") or [None])[0]
        album_artist = (audio.get("albumartist") or [None])[0]
        duration = (
            round(audio.info.length)
            if audio.info is not None and audio.info.length
            else None
        )
    except Exception as e:
        print(
            f"audiotags: failed to parse tags of {path}: {e}", file=sys.stderr
        )
        return _EMPTY_TAGS

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


# maps the picture-frame mime values seen in real files onto the
# file extension the artwork gets uploaded to Discord under
_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "jpeg": "jpg",
    "jpg": "jpg",
    "image/png": "png",
    "png": "png",
    "image/webp": "webp",
    "webp": "webp",
    "image/gif": "gif",
    "gif": "gif",
}


def _front_cover(pictures):
    """Picks the front cover out of a file's embedded pictures. Taking
    the first one blindly shows a 32x32 "file icon" (picture type 1) or
    a back cover as the album art in files that store one of those
    first; falls back to the first picture when none is marked."""
    for picture in pictures:
        if picture.type == PictureType.COVER_FRONT:
            return picture
    return pictures[0] if pictures else None


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

    try:
        if isinstance(audio, MP3):
            apics = audio.tags.getall("APIC") if audio.tags else []
            picture = _front_cover(apics)
            if picture is not None:
                data, mime = picture.data, picture.mime
        elif isinstance(audio, FLAC):
            picture = _front_cover(audio.pictures)
            if picture is not None:
                data, mime = picture.data, picture.mime
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
    except Exception as e:
        print(
            f"audiotags: failed to extract artwork from {path}: {e}",
            file=sys.stderr,
        )
        return None

    if not data or not mime or len(data) > max_bytes:
        return None

    # the mime a picture frame carries is not reliably a real mime type
    # in the wild - ID3 APIC values like "PNG"/"JPG" are common, and
    # "-->" means the frame body is a URL rather than image bytes - so
    # only recognized image types yield an extension
    extension = _IMAGE_EXTENSIONS.get(mime.strip().lower())
    if extension is None:
        print(
            f"audiotags: unsupported artwork type {mime!r} in {path}",
            file=sys.stderr,
        )
        return None

    return data, extension
