"""loader._load_song over a faked short-link resolver."""

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SpotifyTrack, SpotifyTracks
from musicbot.loader import SongError

SHORT = "https://on.soundcloud.com/AbC123"
SC_TRACK = "https://soundcloud.com/artist/track?si=abc"
SC_SET = "https://soundcloud.com/artist/sets/album"


class Resolver:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def extracted(monkeypatch):
    calls = []
    out = {"data": None}

    def extract_info(url, ie=None):
        calls.append(url)
        return out["data"]

    monkeypatch.setattr(loader, "extract_info", extract_info)
    monkeypatch.setattr(loader.YT_IE, "suitable", lambda u: False)
    return calls, out


def _resolver(monkeypatch, result):
    resolver = Resolver(result)
    monkeypatch.setattr(loader, "resolve_short_link", resolver)
    return resolver


def test_short_link_to_a_track_loads_the_resolved_url(
    monkeypatch, worker_loop, extracted
):
    calls, out = extracted
    out["data"] = {"url": "stream", "title": "T"}
    _resolver(monkeypatch, SC_TRACK)

    result = loader._load_song(SHORT)

    assert calls == [SC_TRACK]
    assert result.songs[0].webpage_url == SC_TRACK


def test_extractor_webpage_url_replaces_the_resolved_url(
    monkeypatch, worker_loop, extracted
):
    calls, out = extracted
    out["data"] = {"url": "s", "webpage_url": "https://soundcloud.com/a/t"}
    _resolver(monkeypatch, SC_TRACK)

    result = loader._load_song(SHORT)

    assert result.songs[0].webpage_url == "https://soundcloud.com/a/t"


def test_short_link_to_a_set_queues_every_entry(
    monkeypatch, worker_loop, extracted
):
    calls, out = extracted
    out["data"] = {"entries": [{"url": f"https://x/{i}"} for i in range(3)]}
    _resolver(monkeypatch, SC_SET)

    result = loader._load_song(SHORT)

    assert calls == [SC_SET]
    assert len(result.songs) == 3
    assert SHORT not in [s.webpage_url for s in result.songs]


def test_short_link_to_a_spotify_track(monkeypatch, worker_loop, extracted):
    resolved = "https://open.spotify.com/track/t"
    fetched = []

    async def fetch_spotify(url):
        fetched.append(url)
        return SpotifyTrack("Song Artist")

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)
    monkeypatch.setattr(
        loader, "_search_youtube", lambda q: [{"url": "https://yt/v"}]
    )
    _resolver(monkeypatch, resolved)

    result = loader._load_song("https://spotify.link/xyz")

    assert fetched == [resolved]
    assert [s.webpage_url for s in result.songs] == ["https://yt/v"]


def test_short_link_to_a_spotify_playlist(
    monkeypatch, worker_loop, extracted
):
    resolved = "https://open.spotify.com/playlist/p"
    fetched = []
    links = [f"https://open.spotify.com/track/{i}" for i in (1, 2)]

    async def fetch_spotify(url):
        fetched.append(url)
        return SpotifyTracks(links, None)

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)
    _resolver(monkeypatch, resolved)

    result = loader._load_song("https://spotify.link/xyz")

    assert fetched == [resolved]
    assert [s.webpage_url for s in result.songs] == links


def test_raising_resolver_is_a_songinfo_error(
    monkeypatch, worker_loop, extracted
):
    _resolver(monkeypatch, OSError("boom"))

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_ERROR


def test_resolved_unknown_url_is_unsupported(
    monkeypatch, worker_loop, extracted
):
    _resolver(monkeypatch, "https://example.com/page")

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_UNSUPPORTED


def test_resolved_short_link_is_unsupported_after_one_resolution(
    monkeypatch, worker_loop, extracted
):
    resolver = _resolver(monkeypatch, "https://spotify.link/other")

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_UNSUPPORTED
    assert len(resolver.calls) == 1


def test_unknown_url_off_the_list_never_calls_the_resolver(
    monkeypatch, worker_loop, extracted
):
    resolver = _resolver(monkeypatch, SC_TRACK)

    with pytest.raises(SongError) as e:
        loader._load_song("https://example.com/")

    assert str(e.value) == config.SONGINFO_UNSUPPORTED
    assert resolver.calls == []
