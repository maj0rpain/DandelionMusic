import shutil
import wave
from pathlib import Path

import pytest
from mutagen.asf import ASF
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

from musicbot.audiotags import read_artwork, read_tags

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
