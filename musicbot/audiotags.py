import re
import sys
from pathlib import Path
from typing import Callable, Dict, NamedTuple, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE


class AudioTags(NamedTuple):
    title: Optional[str]
    artist: Optional[str]
    duration: Optional[int]
    album: Optional[str]
    album_artist: Optional[str]
    genre: Optional[str]
    year: Optional[int]
    bitrate: Optional[int]  # bits per second, lossy formats
    sample_rate: Optional[int]  # Hz
    bit_depth: Optional[int]  # bits per sample, lossless formats


_EMPTY_TAGS = AudioTags(
    title=None,
    artist=None,
    duration=None,
    album=None,
    album_artist=None,
    genre=None,
    year=None,
    bitrate=None,
    sample_rate=None,
    bit_depth=None,
)


_YEAR = re.compile(r"(\d{4})")


def _parse_year(value: Optional[str]) -> Optional[int]:
    """Date tags come in every shape in the wild - "2007",
    "2007-10-10", "10/10/2007". Take the first four-digit run and
    ignore whatever else is in there."""
    if not value:
        return None
    match = _YEAR.search(str(value))
    return int(match.group(1)) if match else None


# mutagen's easy mode only wraps ID3-in-MP3, MP4 and TrueAudio. WAV and
# AIFF come back with raw ID3 frames and WMA with ASF attributes, each
# keyed differently, so every other format is read through its own map.
_EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "genre": "genre",
    "year": "date",
}
_ID3_KEYS = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "album_artist": "TPE2",
    "genre": "TCON",
    "year": "TDRC",
}
_ASF_KEYS = {
    "title": "Title",
    "artist": "Author",
    "album": "WM/AlbumTitle",
    "album_artist": "WM/AlbumArtist",
    "genre": "WM/Genre",
    "year": "WM/Year",
}


def _first_str(audio, key: str) -> Optional[str]:
    """The first value under `key` as a str, or None. ID3 frames and
    ASF attributes are objects, not strs, so they are converted."""
    values = audio.get(key) if audio.tags is not None else None
    if not values:
        return None
    first = values[0]
    if first is None:
        return None
    text = str(first)
    return text or None


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
        keys = _format_of(audio).keys
        title = _first_str(audio, keys["title"])
        artist = _first_str(audio, keys["artist"])
        album = _first_str(audio, keys["album"])
        album_artist = _first_str(audio, keys["album_artist"])
        genre = _first_str(audio, keys["genre"])
        year = _parse_year(_first_str(audio, keys["year"]))
        info = audio.info
        duration = (
            round(info.length) if info is not None and info.length else None
        )
        # stream details are format-specific attributes on mutagen's
        # StreamInfo - lossy formats carry a bitrate, lossless ones
        # carry bit depth, and none of them carry every field
        bitrate = getattr(info, "bitrate", None) or None
        sample_rate = getattr(info, "sample_rate", None) or None
        bit_depth = getattr(info, "bits_per_sample", None) or None
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
        genre=genre,
        year=year,
        bitrate=bitrate,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
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


def _id3_cover(audio):
    apics = audio.tags.getall("APIC") if audio.tags else []
    picture = _front_cover(apics)
    return (picture.data, picture.mime) if picture is not None else None


def _flac_cover(audio):
    picture = _front_cover(audio.pictures)
    return (picture.data, picture.mime) if picture is not None else None


def _mp4_cover(audio):
    covr = audio.tags.get("covr") if audio.tags else None
    if not covr:
        return None
    cover = covr[0]
    mime = (
        "image/png" if cover.imageformat == cover.FORMAT_PNG else "image/jpeg"
    )
    return bytes(cover), mime


def _no_cover(audio):
    return None


class _Format(NamedTuple):
    types: tuple  # matched with isinstance
    keys: Dict[str, str]  # tag field -> key in the parsed file
    cover: Callable  # audio -> (data, mime) or None


# The one place a format is described, walked in order with isinstance
# by both readers (the order matters where one class subclasses
# another). The last row matches anything and is the fallback.
_FORMATS = (
    _Format((WAVE, AIFF), _ID3_KEYS, _id3_cover),
    _Format((MP3,), _EASY_KEYS, _id3_cover),
    _Format((FLAC,), _EASY_KEYS, _flac_cover),
    _Format((MP4,), _EASY_KEYS, _mp4_cover),
    _Format((ASF,), _ASF_KEYS, _no_cover),
    _Format((object,), _EASY_KEYS, _no_cover),
)


def _format_of(audio) -> _Format:
    return next(row for row in _FORMATS if isinstance(audio, row.types))


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

    try:
        found = _format_of(audio).cover(audio)
    except Exception as e:
        print(
            f"audiotags: failed to extract artwork from {path}: {e}",
            file=sys.stderr,
        )
        return None

    data, mime = found if found is not None else (None, None)
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
