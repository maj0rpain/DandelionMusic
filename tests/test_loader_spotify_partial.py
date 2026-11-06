"""loader._load_song over a faked Spotify fetch: what comes back when
the playlist was only partly fetched."""

import pickle

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SpotifyTracks
from musicbot.loader import SongError
from musicbot.song import LoadResult
from musicbot.truncation import Truncation, TruncationReason

URL = "https://open.spotify.com/playlist/p"


def _fetch(monkeypatch, result):
    async def fetch_spotify(url):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)
    monkeypatch.setattr(loader, "extract_info", lambda *a, **k: None)


def _links(n):
    return [f"https://open.spotify.com/track/{i}" for i in range(n)]


def test_known_larger_total_gives_truncated_result(monkeypatch, worker_loop):
    cut = Truncation(5, TruncationReason.NO_API)
    _fetch(monkeypatch, SpotifyTracks(_links(3), cut))

    result = loader._load_song(URL)

    assert len(result.songs) == 3
    assert result.single is False
    assert result.truncation == cut


def test_no_truncation_gives_untruncated_playlist(monkeypatch, worker_loop):
    _fetch(monkeypatch, SpotifyTracks(_links(3), None))

    result = loader._load_song(URL)

    assert len(result.songs) == 3
    assert (result.single, result.truncation) == (False, None)


def test_truncated_one_track_playlist_is_never_single(
    monkeypatch, worker_loop
):
    cut = Truncation(5, TruncationReason.NO_API)
    _fetch(monkeypatch, SpotifyTracks(_links(1), cut))

    result = loader._load_song(URL)

    assert (len(result.songs), result.single) == (1, False)


def test_untruncated_one_track_playlist_is_single(monkeypatch, worker_loop):
    _fetch(monkeypatch, SpotifyTracks(_links(1), None))

    result = loader._load_song(URL)

    assert (len(result.songs), result.single) == (1, True)


def test_no_links_is_could_not_load_whatever_the_total(
    monkeypatch, worker_loop
):
    _fetch(
        monkeypatch, SpotifyTracks([], Truncation(50, TruncationReason.NO_API))
    )

    with pytest.raises(SongError) as info:
        loader._load_song(URL)

    assert info.value.args[0] == config.SONGINFO_ERROR


def test_any_fetch_exception_is_could_not_load(monkeypatch, worker_loop):
    _fetch(monkeypatch, KeyError("odd"))

    with pytest.raises(SongError) as info:
        loader._load_song(URL)

    assert info.value.args[0] == config.SONGINFO_ERROR


def test_load_result_survives_pickling():
    cut = Truncation(9, TruncationReason.API_ERROR)
    result = LoadResult(["a", "b"], False, cut)

    copy = pickle.loads(pickle.dumps(result))

    assert copy == LoadResult(["a", "b"], False, cut)


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
