"""A piped song keeps the minimal info dict `yt-dlp --load-info-json`
needs, built by the loader from its extraction and re-filled by preload.
Offline: a real downloader from the loader's factory, a fake extractor."""

import json
import pickle

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor

from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.song import Song
from helpers import run

PAGE = "https://dlinfo.test/video"
STREAM = "https://cdn.test/audio.m4a"


class _Full(InfoExtractor):
    _VALID_URL = r"https://dlinfo\.test/video"
    IE_NAME = "fakedlinfo"
    result = {}

    def _real_extract(self, url):
        return dict(type(self).result)


FULL = {
    "id": "abc",
    "title": "a title",
    "url": STREAM,
    "ext": "m4a",
    "protocol": "https",
    "http_headers": {"User-Agent": "x"},
    "downloader_options": {"http_chunk_size": 1024},
    "uploader": "someone",
}


@pytest.fixture
def downloader(monkeypatch):
    ydl = loader._new_downloader()
    ydl.add_info_extractor(_Full())
    monkeypatch.setattr(loader, "_downloader", ydl)
    _Full.result = dict(FULL)
    monkeypatch.setattr(loader, "identify_url", lambda url: _Full)
    return ydl


def test_an_extracted_song_keeps_the_listed_keys(downloader):
    info = loader._load_song(PAGE).songs[0].download_info
    # yt-dlp merges its default headers into the format's own
    assert info.pop("http_headers")["User-Agent"] == "x"
    expected = {
        "id": "abc",
        "extractor_key": _Full.ie_key(),
        "webpage_url": PAGE,
        "title": "a title",
        "url": STREAM,
        "ext": "m4a",
        "protocol": "https",
        "downloader_options": {"http_chunk_size": 1024},
    }
    assert {k: info[k] for k in expected} == expected


def test_a_formats_available_at_is_kept(downloader):
    _Full.result = dict(FULL, available_at=4102444800)
    info = loader._load_song(PAGE).songs[0].download_info
    assert info["available_at"] == 4102444800


def test_every_yt_dlp_format_field_is_kept():
    names = [
        *YoutubeDL._format_fields,
        "id",
        "extractor_key",
        "webpage_url",
        "title",
        "cookies",
    ]
    info = loader._download_info({n: 1 for n in names})
    assert [n for n in names if n not in info] == []


def test_values_json_cannot_encode_still_serialize():
    info = loader._download_info(
        {"url": STREAM, "fragments": lambda ctx: [], "request_data": b"\x00"}
    )
    json.dumps(info)
    pickle.dumps(info)


def test_keys_the_extraction_lacks_are_left_out(downloader):
    _Full.result = {"id": "abc", "title": "t", "url": STREAM, "ext": None}
    info = loader._load_song(PAGE).songs[0].download_info
    assert "downloader_options" not in info
    assert None not in info.values()


def test_a_song_unpickled_without_it_reads_none():
    restored = pickle.loads(pickle.dumps(Song(SiteTypes.YT_DLP, PAGE)))
    assert restored.download_info is None


def test_preload_re_extracts_a_current_looking_song_without_it(
    downloader, monkeypatch
):
    song = Song(
        SiteTypes.YT_DLP, PAGE, url="https://cdn.test/a?expire=4102444800"
    )

    async def load(lane, track):
        return loader._load_song(track)

    monkeypatch.setattr(loader, "_load", load)
    assert run(loader.preload(song)) is loader.Preload.EXTRACTED
    assert song.download_info["url"] == STREAM
