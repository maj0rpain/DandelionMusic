import shutil
import wave
from pathlib import Path

import pytest
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    PictureType,
)
from mutagen.aiff import AIFF
from mutagen.wave import WAVE

from musicbot.audiotags import AudioTags, read_artwork, read_tags

FIXTURES = Path(__file__).parent / "fixtures"

ID3_FRAMES = {
    "title": TIT2,
    "artist": TPE1,
    "album": TALB,
    "album_artist": TPE2,
    "genre": TCON,
    "year": TDRC,
}
ASF_KEYS = {
    "title": "Title",
    "artist": "Author",
    "album": "WM/AlbumTitle",
    "album_artist": "WM/AlbumArtist",
    "genre": "WM/Genre",
    "year": "WM/Year",
}
EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "genre": "genre",
    "year": "date",
}
MP4_KEYS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_artist": "aART",
    "genre": "\xa9gen",
    "year": "\xa9day",
}
FULL = {
    "title": "Song",
    "artist": "Artist",
    "album": "Album",
    "album_artist": "Various",
    "genre": "Rock",
    "year": "2007-10-10",
}
FIELDS = list(FULL)


def _copy_fixture(tmp_path: Path, ext: str) -> Path:
    path = tmp_path / f"a.{ext}"
    shutil.copy(FIXTURES / f"silent.{ext}", path)
    return path


def make_wav(tmp_path: Path) -> Path:
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def make_aiff(tmp_path: Path) -> Path:
    return _copy_fixture(tmp_path, "aiff")


def make_wma(tmp_path: Path) -> Path:
    return _copy_fixture(tmp_path, "wma")


def make_mp3(tmp_path: Path) -> Path:
    path = tmp_path / "a.mp3"
    # a run of valid MPEG-1 layer 3 frames (128 kbps, 44.1 kHz)
    path.write_bytes((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 20)
    return path


def make_flac(tmp_path: Path) -> Path:
    return _copy_fixture(tmp_path, "flac")


def make_mp4(tmp_path: Path) -> Path:
    return _copy_fixture(tmp_path, "m4a")


def _picture_data(ptype) -> bytes:
    return b"img" + bytes([int(ptype)])


MP4_FORMATS = {
    "image/png": MP4Cover.FORMAT_PNG,
    "image/jpeg": MP4Cover.FORMAT_JPEG,
}


def _add_id3(tags, values, pictures) -> None:
    for field, value in values.items():
        tags.add(ID3_FRAMES[field](encoding=3, text=[value]))
    for ptype, mime in pictures:
        tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=ptype,
                desc="",
                data=_picture_data(ptype),
            )
        )


def _write_asf(path: Path, values: dict, pictures) -> None:
    if pictures:
        raise ValueError("the ASF writer does not write pictures")
    audio = ASF(path)
    for field, value in values.items():
        audio[ASF_KEYS[field]] = [value]
    audio.save()


def _write_flac(path: Path, values: dict, pictures) -> None:
    audio = FLAC(path)
    for field, value in values.items():
        audio[EASY_KEYS[field]] = [value]
    for ptype, mime in pictures:
        picture = Picture()
        picture.type = ptype
        picture.mime = mime
        picture.data = _picture_data(ptype)
        audio.add_picture(picture)
    audio.save()


def _write_mp4(path: Path, values: dict, pictures) -> None:
    audio = MP4(path)
    audio.add_tags()
    for field, value in values.items():
        audio[MP4_KEYS[field]] = [value]
    if pictures:
        audio["covr"] = [
            MP4Cover(_picture_data(ptype), MP4_FORMATS[mime])
            for ptype, mime in pictures
        ]
    audio.save()


def _write_id3(path: Path, values: dict, pictures) -> None:
    tags = ID3()
    _add_id3(tags, values, pictures)
    tags.save(path)


def _write_id3_in_file(path: Path, values: dict, pictures) -> None:
    audio = (WAVE if path.suffix == ".wav" else AIFF)(path)
    audio.add_tags()
    _add_id3(audio.tags, values, pictures)
    audio.save()


WRITERS = {
    ".wma": _write_asf,
    ".flac": _write_flac,
    ".m4a": _write_mp4,
    ".mp3": _write_id3,
    ".wav": _write_id3_in_file,
    ".aiff": _write_id3_in_file,
}


def tag(path: Path, values: dict, pictures=()) -> None:
    WRITERS[path.suffix](path, values, pictures)


def _format_id(maker) -> str:
    return maker.__name__.removeprefix("make_")


MAKERS = [make_wav, make_aiff, make_wma, make_mp3, make_flac, make_mp4]
TYPED_PICTURE_MAKERS = [make_wav, make_aiff, make_mp3, make_flac]


@pytest.fixture(params=MAKERS, ids=_format_id)
def audio_file(request, tmp_path):
    return request.param(tmp_path)


def test_reads_all_six_fields(audio_file):
    tag(audio_file, FULL)
    tags = read_tags(audio_file)
    assert (
        tags.title,
        tags.artist,
        tags.album,
        tags.album_artist,
        tags.genre,
        tags.year,
    ) == ("Song", "Artist", "Album", "Various", "Rock", 2007)


def test_partial_tags_leave_the_rest_none(audio_file):
    tag(audio_file, {"title": "Song", "genre": "Rock"})
    tags = read_tags(audio_file)
    assert (
        tags.title,
        tags.artist,
        tags.album,
        tags.album_artist,
        tags.genre,
        tags.year,
    ) == ("Song", None, None, None, "Rock", None)


def test_unparsable_year_is_none_and_keeps_other_fields(audio_file):
    tag(audio_file, {"title": "Song", "year": "soon"})
    tags = read_tags(audio_file)
    assert (tags.title, tags.year) == ("Song", None)


def test_values_are_str_or_none(audio_file):
    tag(audio_file, FULL)
    tags = read_tags(audio_file)
    for field in ("title", "artist", "album", "album_artist", "genre"):
        assert type(getattr(tags, field)) is str


@pytest.mark.parametrize("maker", TYPED_PICTURE_MAKERS, ids=_format_id)
def test_front_cover_picked_over_earlier_non_front(maker, tmp_path):
    path = maker(tmp_path)
    tag(
        path,
        {},
        [
            (PictureType.OTHER, "image/png"),
            (PictureType.COVER_FRONT, "image/jpeg"),
        ],
    )
    assert read_artwork(path) == (
        _picture_data(PictureType.COVER_FRONT),
        "jpg",
    )


@pytest.mark.parametrize("maker", TYPED_PICTURE_MAKERS, ids=_format_id)
def test_unsupported_picture_mime_gives_no_cover(maker, tmp_path):
    path = maker(tmp_path)
    tag(path, {}, [(PictureType.COVER_FRONT, "-->")])
    assert read_artwork(path) is None


def test_flac_without_front_marked_picture_gives_the_first(tmp_path):
    path = make_flac(tmp_path)
    tag(
        path,
        {},
        [
            (PictureType.OTHER, "image/png"),
            (PictureType.COVER_BACK, "image/jpeg"),
        ],
    )
    assert read_artwork(path) == (_picture_data(PictureType.OTHER), "png")


def test_no_cover_gives_none(audio_file):
    tag(audio_file, {"title": "Song"})
    assert read_artwork(audio_file) is None


@pytest.mark.parametrize(
    "mime, ext",
    [("image/png", "png"), ("image/jpeg", "jpg")],
    ids=["png", "jpeg"],
)
def test_mp4_cover_format_maps_to_extension(mime, ext, tmp_path):
    path = make_mp4(tmp_path)
    tag(path, {}, [(PictureType.COVER_FRONT, mime)])
    assert read_artwork(path) == (_picture_data(PictureType.COVER_FRONT), ext)


def test_cover_larger_than_max_bytes_gives_none(tmp_path):
    path = make_flac(tmp_path)
    tag(path, {}, [(PictureType.COVER_FRONT, "image/jpeg")])
    assert read_artwork(path, max_bytes=3) is None
    assert read_artwork(path, max_bytes=4) is not None


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".m4a", ".wav", ".wma"])
def test_corrupt_file_gives_empty_tags_and_no_cover(suffix, tmp_path):
    path = tmp_path / f"bad{suffix}"
    path.write_bytes(b"not audio at all" * 8)
    assert read_tags(path) == AudioTags(*[None] * 10)
    assert read_artwork(path) is None
