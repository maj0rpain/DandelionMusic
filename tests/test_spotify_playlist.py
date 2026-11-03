import pytest

from musicbot import linkutils
from musicbot.linkutils import SpotifyPlaylistTypes, fetch_playlist_with_api


def track(n):
    return {"external_urls": {"spotify": f"https://open.spotify.com/t/{n}"}}


def url(n):
    return f"https://open.spotify.com/t/{n}"


class FakeSpotify:
    def __init__(self, items):
        self._items = items

    def _page(self):
        return {"items": self._items}

    def playlist_items(self, code):
        return self._page()

    def album_tracks(self, code):
        return self._page()

    def next(self, results):
        return None


@pytest.fixture
def spotify(monkeypatch):
    def install(items):
        monkeypatch.setattr(linkutils, "spotify_api", FakeSpotify(items))

    return install


def test_removed_track_is_skipped_quietly(spotify, capsys):
    spotify([{"track": track(1)}, {"track": None}, {"track": track(2)}])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")

    assert links == [url(1), url(2)]
    assert capsys.readouterr().err == ""


def test_local_file_item_is_skipped_with_warning(spotify, capsys):
    spotify([{"track": track(1)}, {"track": {"external_urls": {}}}])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")

    assert links == [url(1)]
    assert "Cannot extract URL" in capsys.readouterr().err


def test_playlist_of_only_removed_tracks_is_empty(spotify):
    spotify([{"track": None}, {"track": None}])

    assert fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p") == []


def test_album_tracks_resolve_in_order(spotify):
    spotify([track(1), track(2)])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.ALBUM, "a")

    assert links == [url(1), url(2)]
