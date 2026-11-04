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
FULL = {
    "title": "Song",
    "artist": "Artist",
    "album": "Album",
    "album_artist": "Various",
    "genre": "Rock",
    "year": "2007-10-10",
}
FIELDS = list(FULL)


def make_wav(tmp_path: Path) -> Path:
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def make_aiff(tmp_path: Path) -> Path:
    path = tmp_path / "a.aiff"
    shutil.copy(FIXTURES / "silent.aiff", path)
    return path


def make_wma(tmp_path: Path) -> Path:
    path = tmp_path / "a.wma"
    shutil.copy(FIXTURES / "silent.wma", path)
    return path


def tag(path: Path, values: dict, pictures=()) -> None:
    if path.suffix == ".wma":
        audio = ASF(path)
        for field, value in values.items():
            audio[ASF_KEYS[field]] = [value]
        audio.save()
        return
    audio = WAVE(path) if path.suffix == ".wav" else AIFF(path)
    audio.add_tags()
    for field, value in values.items():
        frame = ID3_FRAMES[field]
        audio.tags.add(frame(encoding=3, text=[value]))
    for ptype, mime in pictures:
        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=ptype,
                desc="",
                data=b"img" + bytes([int(ptype)]),
            )
        )
    audio.save()


MAKERS = [make_wav, make_aiff, make_wma]


@pytest.fixture(params=MAKERS, ids=["wav", "aiff", "wma"])
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


def test_mp3_is_still_read_through_easy_mode(tmp_path):
    path = tmp_path / "a.mp3"
    # a run of valid MPEG-1 layer 3 frames (128 kbps, 44.1 kHz)
    path.write_bytes((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 20)
    id3 = ID3()
    id3.add(TIT2(encoding=3, text=["Song"]))
    id3.add(TPE1(encoding=3, text=["Artist"]))
    id3.add(TDRC(encoding=3, text=["2001"]))
    id3.save(path)
    tags = read_tags(path)
    assert (tags.title, tags.artist, tags.year) == ("Song", "Artist", 2001)


@pytest.mark.parametrize("maker", [make_wav, make_aiff])
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
    assert read_artwork(path) == (b"img" + bytes([3]), "jpg")


@pytest.mark.parametrize("maker", [make_wav, make_aiff])
def test_unsupported_apic_mime_gives_no_cover(maker, tmp_path):
    path = maker(tmp_path)
    tag(path, {}, [(PictureType.COVER_FRONT, "-->")])
    assert read_artwork(path) is None


@pytest.mark.parametrize("maker", [make_wav, make_aiff])
def test_no_apic_gives_no_cover(maker, tmp_path):
    path = maker(tmp_path)
    tag(path, {"title": "Song"})
    assert read_artwork(path) is None


def make_mp3(tmp_path: Path) -> Path:
    path = tmp_path / "a.mp3"
    # a run of valid MPEG-1 layer 3 frames (128 kbps, 44.1 kHz)
    path.write_bytes((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 20)
    return path


def make_flac(tmp_path: Path) -> Path:
    path = tmp_path / "a.flac"
    shutil.copy(FIXTURES / "silent.flac", path)
    return path


def make_mp4(tmp_path: Path) -> Path:
    path = tmp_path / "a.m4a"
    shutil.copy(FIXTURES / "silent.m4a", path)
    return path


def tag_mp3(path: Path, pictures=()) -> None:
    id3 = ID3()
    for ptype, mime in pictures:
        id3.add(
            APIC(
                encoding=3,
                mime=mime,
                type=ptype,
                desc="",
                data=b"img" + bytes([int(ptype)]),
            )
        )
    id3.save(path)


def tag_flac(path: Path, values: dict = None, pictures=()) -> None:
    audio = FLAC(path)
    for field, value in (values or {}).items():
        audio[EASY_KEYS[field]] = [value]
    for ptype, mime in pictures:
        picture = Picture()
        picture.type = ptype
        picture.mime = mime
        picture.data = b"img" + bytes([int(ptype)])
        audio.add_picture(picture)
    audio.save()


def tag_mp4(path: Path, values: dict = None, covers=()) -> None:
    audio = MP4(path)
    audio.add_tags()
    for field, value in (values or {}).items():
        audio[MP4_KEYS[field]] = [value]
    if covers:
        audio["covr"] = [MP4Cover(data, fmt) for data, fmt in covers]
    audio.save()


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
SIX = ("title", "artist", "album", "album_artist", "genre", "year")


@pytest.mark.parametrize(
    "maker, tagger",
    [(make_flac, tag_flac), (make_mp4, tag_mp4)],
    ids=["flac", "mp4"],
)
def test_flac_and_mp4_read_all_six_fields(maker, tagger, tmp_path):
    path = maker(tmp_path)
    tagger(path, FULL)
    tags = read_tags(path)
    assert tuple(getattr(tags, f) for f in SIX) == (
        "Song",
        "Artist",
        "Album",
        "Various",
        "Rock",
        2007,
    )


def test_mp3_front_cover_picked_over_earlier_non_front(tmp_path):
    path = make_mp3(tmp_path)
    tag_mp3(
        path,
        [
            (PictureType.OTHER, "image/png"),
            (PictureType.COVER_FRONT, "image/jpeg"),
        ],
    )
    assert read_artwork(path) == (b"img" + bytes([3]), "jpg")


def test_mp3_without_cover_gives_none(tmp_path):
    path = make_mp3(tmp_path)
    tag_mp3(path)
    assert read_artwork(path) is None


def test_flac_front_cover_picked_over_earlier_non_front(tmp_path):
    path = make_flac(tmp_path)
    tag_flac(
        path,
        pictures=[
            (PictureType.OTHER, "image/png"),
            (PictureType.COVER_FRONT, "image/jpeg"),
        ],
    )
    assert read_artwork(path) == (b"img" + bytes([3]), "jpg")


def test_flac_without_front_marked_picture_gives_the_first(tmp_path):
    path = make_flac(tmp_path)
    tag_flac(
        path,
        pictures=[
            (PictureType.OTHER, "image/png"),
            (PictureType.COVER_BACK, "image/jpeg"),
        ],
    )
    assert read_artwork(path) == (b"img" + bytes([0]), "png")


def test_flac_without_cover_gives_none(tmp_path):
    path = make_flac(tmp_path)
    tag_flac(path, {"title": "Song"})
    assert read_artwork(path) is None


def test_mp4_png_cover_yields_png(tmp_path):
    path = make_mp4(tmp_path)
    tag_mp4(path, covers=[(b"pngdata", MP4Cover.FORMAT_PNG)])
    assert read_artwork(path) == (b"pngdata", "png")


def test_mp4_jpeg_cover_yields_jpg(tmp_path):
    path = make_mp4(tmp_path)
    tag_mp4(path, covers=[(b"jpgdata", MP4Cover.FORMAT_JPEG)])
    assert read_artwork(path) == (b"jpgdata", "jpg")


def test_mp4_without_cover_gives_none(tmp_path):
    path = make_mp4(tmp_path)
    tag_mp4(path, {"title": "Song"})
    assert read_artwork(path) is None


def test_wma_has_no_artwork(tmp_path):
    path = make_wma(tmp_path)
    tag(path, FULL)
    assert read_artwork(path) is None


def test_cover_larger_than_max_bytes_gives_none(tmp_path):
    path = make_flac(tmp_path)
    tag_flac(path, pictures=[(PictureType.COVER_FRONT, "image/jpeg")])
    assert read_artwork(path, max_bytes=3) is None
    assert read_artwork(path, max_bytes=4) is not None


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".m4a", ".wav", ".wma"])
def test_corrupt_file_gives_empty_tags_and_no_cover(suffix, tmp_path):
    path = tmp_path / f"bad{suffix}"
    path.write_bytes(b"not audio at all" * 8)
    assert read_tags(path) == AudioTags(*[None] * 10)
    assert read_artwork(path) is None
