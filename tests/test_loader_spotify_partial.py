"""loader._load_song over a faked Spotify fetch: what comes back when
the playlist was only partly fetched."""

import pickle

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SiteTypes, SpotifyTracks
from musicbot.loader import PartialPlaylist, SongError

URL = "https://open.spotify.com/playlist/p"


def _fetch(monkeypatch, result):
    async def fetch_spotify(url):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)
    monkeypatch.setattr(loader, "extract_info", lambda *a, **k: None)
    # tracks are plain, already resolved links
    monkeypatch.setattr(loader.YT_IE, "suitable", lambda u: False)


def _links(n):
    return [f"https://open.spotify.com/track/{i}" for i in range(n)]


def test_known_larger_total_gives_partial_playlist(monkeypatch, worker_loop):
    _fetch(
        monkeypatch, SpotifyTracks(_links(3), 5, "SPOTIFY_TRUNCATED_NO_API")
    )

    result = loader._load_song(URL)

    assert isinstance(result, PartialPlaylist)
    assert len(result) == 3
    assert (result.total, result.reason) == (5, "SPOTIFY_TRUNCATED_NO_API")


@pytest.mark.parametrize("total", [None, 3])
def test_unknown_or_complete_total_gives_plain_list(
    monkeypatch, worker_loop, total
):
    _fetch(monkeypatch, SpotifyTracks(_links(3), total, None))

    result = loader._load_song(URL)

    assert type(result) is list and len(result) == 3


def test_no_links_is_could_not_load_whatever_the_total(
    monkeypatch, worker_loop
):
    _fetch(monkeypatch, SpotifyTracks([], 50, "SPOTIFY_TRUNCATED_NO_API"))

    with pytest.raises(SongError) as info:
        loader._load_song(URL)

    assert info.value.args[0] == config.SONGINFO_ERROR


def test_any_fetch_exception_is_could_not_load(monkeypatch, worker_loop):
    _fetch(monkeypatch, KeyError("odd"))

    with pytest.raises(SongError) as info:
        loader._load_song(URL)

    assert info.value.args[0] == config.SONGINFO_ERROR


def test_partial_playlist_survives_pickling():
    partial = PartialPlaylist(["a", "b"], 9, "SPOTIFY_TRUNCATED_API_ERROR")

    copy = pickle.loads(pickle.dumps(partial))

    assert isinstance(copy, PartialPlaylist)
    assert list(copy) == ["a", "b"]
    assert (copy.total, copy.reason) == (9, "SPOTIFY_TRUNCATED_API_ERROR")


def test_load_song_classifies_and_loads_the_cleaned_text(
    monkeypatch, worker_loop
):
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return {"url": "https://example.com/a", "title": "t"}

    extracted = []

    def fake_extract(url, ie=None):
        extracted.append(url)
        return {"url": url, "title": "t"}

    monkeypatch.setattr(loader, "fetch_spotify", fake_fetch)
    monkeypatch.setattr(loader, "extract_info", fake_extract)
    loader._load_song("<https://open.spotify.com/track/x>")
    loader._load_song("https://youtu.be/dQw4w9WgXcQ.")
    assert fetched == ["https://open.spotify.com/track/x"]
    assert extracted[0] == "https://youtu.be/dQw4w9WgXcQ"


def _no_result_load(monkeypatch, track, search=None):
    monkeypatch.setattr(loader, "_search_youtube", lambda *a, **k: search)
    empty = {"entries": []}
    monkeypatch.setattr(loader, "extract_info", lambda *a, **k: empty)
    with pytest.raises(SongError) as info:
        loader._load_song(track)
    return info.value.args[0]


def test_empty_search_is_search_no_results(monkeypatch):
    assert _no_result_load(monkeypatch, "some title", []) == (
        config.SEARCH_NO_RESULTS
    )


def test_failed_search_is_songinfo_error_not_no_results(monkeypatch):
    message = _no_result_load(monkeypatch, "some title", None)
    assert message == config.SONGINFO_ERROR
    assert message != config.SEARCH_NO_RESULTS


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
