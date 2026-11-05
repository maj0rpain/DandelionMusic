"""The loader's no-result failures: the sync search raising SONGINFO_ERROR
or SEARCH_NO_RESULTS, and which message _load_song gives when a search, a
link, a Spotify track or a playlist yields nothing."""

import pytest

from config import config
from musicbot import linkutils, loader
from musicbot.linkutils import SiteTypes
from musicbot.loader import SongError
from helpers import run


def _fake_extraction(monkeypatch, extracted):
    monkeypatch.setattr(loader, "extract_info", lambda *a, **k: extracted)


def _raised(call, *args):
    with pytest.raises(SongError) as info:
        call(*args)
    return info.value.args[0]


def _no_result_load(monkeypatch, track):
    _fake_extraction(monkeypatch, {"entries": []})
    return _raised(loader._load_song, track)


def test_failed_extraction_raises_songinfo_error(monkeypatch):
    _fake_extraction(monkeypatch, None)
    assert _raised(loader._search_youtube, "q") == config.SONGINFO_ERROR


def test_empty_entries_raise_search_no_results(monkeypatch):
    _fake_extraction(monkeypatch, {"entries": []})
    assert _raised(loader._search_youtube, "q") == config.SEARCH_NO_RESULTS


def test_entries_come_back_unchanged(monkeypatch):
    entries = [{"url": "a"}, {"url": "b"}]
    _fake_extraction(monkeypatch, {"entries": entries})
    assert loader._search_youtube("q", 2) == entries


def test_empty_search_is_search_no_results(monkeypatch):
    assert _no_result_load(monkeypatch, "some title") == (
        config.SEARCH_NO_RESULTS
    )


@pytest.mark.parametrize(
    "message", [config.SEARCH_NO_RESULTS, config.SONGINFO_ERROR]
)
def test_spotify_track_whose_search_raises_is_songinfo_error(
    monkeypatch, message
):
    class FakeApi:
        def track(self, code):
            return {"name": "T", "artists": [{"name": "A"}]}

    def search(query, count=1):
        raise SongError(message)

    monkeypatch.setattr(linkutils, "spotify_api", FakeApi())
    monkeypatch.setattr(loader, "_search_youtube", search)
    track = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    assert _raised(loader._load_song, track) == config.SONGINFO_ERROR


def test_unknown_host_is_unsupported(monkeypatch):
    monkeypatch.setattr(loader, "identify_url", lambda t: SiteTypes.UNKNOWN)
    assert _no_result_load(monkeypatch, "https://x.invalid/a") == (
        config.SONGINFO_UNSUPPORTED
    )


def test_playlist_with_no_entries_is_songinfo_error(monkeypatch):
    monkeypatch.setattr(loader, "identify_url", lambda t: SiteTypes.YT_DLP)
    assert _no_result_load(monkeypatch, "https://x.invalid/pl") == (
        config.SONGINFO_ERROR
    )


def test_spotify_track_is_searched_by_the_loader_with_its_query(
    monkeypatch, worker_loop
):
    queries = []

    async def fetch_spotify(url):
        return linkutils.SpotifyTrack('T - A "Topic"')

    def search(query, count=1):
        queries.append(query)
        return [{"url": "https://yt/x", "title": "T"}]

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)
    monkeypatch.setattr(loader, "_search_youtube", search)
    monkeypatch.setattr(loader.YT_IE, "suitable", lambda u: False)
    track = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    song = loader._load_song(track).songs[0]
    assert queries == ['T - A "Topic"']
    assert (song.title, song.host) == ("T", SiteTypes.SPOTIFY)


def test_search_youtube_returns_songs(monkeypatch):
    entries = [
        {"url": "https://yt/a", "title": "A"},
        {"url": "https://yt/b", "title": "B"},
    ]
    _fake_extraction(monkeypatch, {"entries": entries})

    async def run_sync(lane, fn, *args):
        return fn(*args)

    monkeypatch.setattr(loader, "_run_sync", run_sync)
    songs = run(loader.search_youtube("q", 2))
    assert [(s.webpage_url, s.title) for s in songs] == [
        ("https://yt/a", "A"),
        ("https://yt/b", "B"),
    ]
    assert all(s.host is SiteTypes.YT_DLP for s in songs)
