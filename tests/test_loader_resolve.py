"""A link that resolves to another link loads as the media it ends at:
yt-dlp follows the chain, while playlist entries stay unresolved. Fully
offline: a real downloader from the loader's factory with fake extractors."""

import pytest
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError

from config import config
from musicbot import loader
from musicbot.loader import SongError

SHORT = "https://short.test/abc"
TARGET = "https://target.test/video"
STREAM = "https://cdn.test/audio.m4a"


class _Target(InfoExtractor):
    _VALID_URL = r"https://target\.test/.*"
    IE_NAME = "faketarget"
    calls = 0

    def _real_extract(self, url):
        type(self).calls += 1
        return {"id": "t", "title": "target title", "url": STREAM}


class _FailingTarget(InfoExtractor):
    IE_NAME = "fakefailing"
    _VALID_URL = r"https://failing\.test/.*"

    def _real_extract(self, url):
        raise ExtractorError("boom", expected=True)


class _Shortener(InfoExtractor):
    _VALID_URL = r"https://short\.test/.*"
    IE_NAME = "fakeshort"
    result = {}

    def _real_extract(self, url):
        return dict(type(self).result, id="s")


class _Playlist(InfoExtractor):
    _VALID_URL = r"https://list\.test/.*"
    IE_NAME = "fakelist"

    def _real_extract(self, url):
        entries = [
            {
                "_type": "url",
                "url": f"https://target.test/{i}",
                "ie_key": _Target.ie_key(),
            }
            for i in range(2)
        ]
        return self.playlist_result(entries, "p", "list")


@pytest.fixture
def downloader(monkeypatch):
    ydl = loader._new_downloader()
    for cls in (_Target, _FailingTarget, _Shortener, _Playlist):
        ydl.add_info_extractor(cls())
    monkeypatch.setattr(loader, "_downloader", ydl)
    _Target.calls = 0
    _Shortener.result = {}
    return ydl


def _route_input_to(monkeypatch, cls):
    monkeypatch.setattr(loader, "identify_url", lambda url: cls)


def _shortener_returns(target, **extra):
    _Shortener.result = {
        "_type": "url",
        "url": target,
        "ie_key": _Target.ie_key(),
        **extra,
    }


def test_short_link_loads_as_the_targets_stream(downloader, monkeypatch):
    _route_input_to(monkeypatch, _Shortener)
    _shortener_returns(TARGET)

    result = loader._load_song(SHORT)

    assert [s.url for s in result.songs] == [STREAM]


def test_chain_of_several_hops_loads_the_final_stream(downloader, monkeypatch):
    class Middle(InfoExtractor):
        _VALID_URL = r"https://middle\.test/.*"
        IE_NAME = "fakemiddle"

        def _real_extract(self, url):
            return {
                "_type": "url",
                "url": TARGET,
                "ie_key": _Target.ie_key(),
                "id": "m",
            }

    downloader.add_info_extractor(Middle())
    _route_input_to(monkeypatch, _Shortener)
    _shortener_returns("https://middle.test/x", ie_key=Middle.ie_key())

    result = loader._load_song(SHORT)

    assert [s.url for s in result.songs] == [STREAM]


def test_url_transparent_keeps_outer_title(downloader, monkeypatch):
    _route_input_to(monkeypatch, _Shortener)
    _shortener_returns(TARGET, _type="url_transparent", title="outer title")

    song = loader._load_song(SHORT).songs[0]

    assert (song.title, song.url) == ("outer title", STREAM)


def test_playlist_url_entries_stay_unresolved(downloader, monkeypatch):
    _route_input_to(monkeypatch, _Playlist)

    result = loader._load_song("https://list.test/p")

    assert [s.webpage_url for s in result.songs] == [
        "https://target.test/0",
        "https://target.test/1",
    ]
    assert _Target.calls == 0


def test_unresolvable_target_raises_songinfo_error(downloader, monkeypatch):
    _route_input_to(monkeypatch, _Shortener)
    _shortener_returns(
        "https://failing.test/x", ie_key=_FailingTarget.ie_key()
    )

    with pytest.raises(SongError) as info:
        loader._load_song(SHORT)

    assert info.value.args[0] == config.SONGINFO_ERROR


def _search_returns(monkeypatch, hit_url):
    hit = {"_type": "url", "url": hit_url, "ie_key": "Youtube"}
    monkeypatch.setattr(loader, "_search_youtube", lambda q, c=1: [hit])
    monkeypatch.setattr(
        loader,
        "get_ie",
        lambda url: _FailingTarget if "failing" in url else _Target,
    )


def test_search_text_loads_the_hits_stream_and_watch_link(
    downloader, monkeypatch
):
    _route_input_to(monkeypatch, loader.SiteTypes.NOT_URL)
    _search_returns(monkeypatch, TARGET)

    result = loader._load_song("some words")

    assert [(s.url, s.webpage_url) for s in result.songs] == [(STREAM, TARGET)]


def test_spotify_track_loads_the_hits_stream_and_watch_link(
    downloader, monkeypatch, worker_loop
):
    _route_input_to(monkeypatch, loader.SiteTypes.SPOTIFY)
    _search_returns(monkeypatch, TARGET)

    async def fetch_spotify(url):
        return loader.SpotifyTrack("artist title")

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)

    result = loader._load_song("https://open.spotify.com/track/x")

    assert [(s.url, s.webpage_url) for s in result.songs] == [(STREAM, TARGET)]


def test_search_hit_that_fails_to_extract_raises_songinfo_error(
    downloader, monkeypatch
):
    _route_input_to(monkeypatch, loader.SiteTypes.NOT_URL)
    _search_returns(monkeypatch, "https://failing.test/x")

    with pytest.raises(SongError) as info:
        loader._load_song("some words")

    assert info.value.args[0] == config.SONGINFO_ERROR


def test_search_listing_keeps_flat_hits_without_extracting(
    downloader, monkeypatch
):
    _search_returns(monkeypatch, TARGET)

    songs = loader._search_songs("some words")

    assert [s.webpage_url for s in songs] == [TARGET]
    assert _Target.calls == 0
