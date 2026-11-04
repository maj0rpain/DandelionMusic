"""loader._load_song over a faked Spotify fetch: what comes back when
the playlist was only partly fetched."""

import pickle

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SpotifyTracks
from musicbot.loader import PartialPlaylist, SongError
from musicbot.truncation import Truncation, TruncationReason

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
    cut = Truncation(5, TruncationReason.NO_API)
    _fetch(monkeypatch, SpotifyTracks(_links(3), cut))

    result = loader._load_song(URL)

    assert isinstance(result, PartialPlaylist)
    assert len(result) == 3
    assert result.truncation == cut


@pytest.mark.parametrize(
    "truncation", [None, Truncation(3, TruncationReason.NO_API)]
)
def test_unknown_or_complete_total_gives_plain_list(
    monkeypatch, worker_loop, truncation
):
    _fetch(monkeypatch, SpotifyTracks(_links(3), truncation))

    result = loader._load_song(URL)

    assert type(result) is list and len(result) == 3


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


def test_partial_playlist_survives_pickling():
    cut = Truncation(9, TruncationReason.API_ERROR)
    partial = PartialPlaylist(["a", "b"], cut)

    copy = pickle.loads(pickle.dumps(partial))

    assert isinstance(copy, PartialPlaylist)
    assert list(copy) == ["a", "b"]
    assert copy.truncation == cut


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
