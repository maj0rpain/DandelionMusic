import asyncio

from bs4 import BeautifulSoup
import pytest

from musicbot import linkutils, loader
from musicbot.linkutils import SpotifyPlaylistTypes, fetch_playlist_with_api


def url(n):
    return f"https://open.spotify.com/t/{n}"


def track(n):
    return {"external_urls": {"spotify": url(n)}}


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

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p").links

    assert links == [url(1), url(2)]
    assert capsys.readouterr().err == ""


def test_local_file_item_is_skipped_with_warning(spotify, capsys):
    spotify([{"track": track(1)}, {"track": {"external_urls": {}}}])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p").links

    assert links == [url(1)]
    assert "Cannot extract URL" in capsys.readouterr().err


def test_playlist_of_only_removed_tracks_is_empty(spotify):
    spotify([{"track": None}, {"track": None}])

    assert (
        fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p").links == []
    )


def test_album_tracks_resolve_in_order(spotify):
    spotify([track(1), track(2)])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.ALBUM, "a").links

    assert links == [url(1), url(2)]


class PagedSpotify(FakeSpotify):
    """Two pages of two tracks; `next` raises, or ends the paging."""

    def __init__(self, exc=None, first_raises=False):
        super().__init__([track(1), track(2)])
        self.exc = exc
        self.first_raises = first_raises

    def _page(self):
        if self.first_raises:
            raise self.exc
        return {"items": self._items, "total": 4}

    def next(self, results):
        if self.exc:
            raise self.exc
        return None


def test_paging_failure_after_page_one_returns_partial_with_reason(
    monkeypatch,
):
    monkeypatch.setattr(
        linkutils, "spotify_api", PagedSpotify(RuntimeError("boom"))
    )

    result = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")

    assert result.links == [url(1), url(2)]
    assert result.total == 4
    assert result.reason == "SPOTIFY_TRUNCATED_API_ERROR"


def test_failure_before_any_page_propagates(monkeypatch):
    monkeypatch.setattr(
        linkutils,
        "spotify_api",
        PagedSpotify(RuntimeError("boom"), first_raises=True),
    )

    with pytest.raises(RuntimeError):
        fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")


def test_completed_fetch_with_removed_track_has_no_total(spotify):
    spotify([{"track": track(1)}, {"track": None}])

    result = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")

    assert result.total is None and result.reason is None


def _page(count, song_count):
    tags = "".join(
        f'<meta name="music:song" content="{url(n)}">' for n in range(count)
    )
    if song_count is not None:
        tags += f'<meta name="music:song_count" content="{song_count}">'
    return BeautifulSoup(f"<html><head>{tags}</head></html>", "html.parser")


@pytest.mark.parametrize("count, expected", [(50, 50), (None, None)])
def test_scrape_reports_song_count_as_total(monkeypatch, count, expected):
    monkeypatch.setattr(linkutils, "spotify_api", None)

    async def get_soup(u):
        return _page(30, count)

    monkeypatch.setattr(linkutils, "get_soup", get_soup)

    result = asyncio.run(
        linkutils.fetch_spotify_playlist("u", "playlist", "p")
    )

    assert len(result.links) == 30
    assert result.total == expected
    assert result.reason == "SPOTIFY_TRUNCATED_NO_API"


def test_fetch_spotify_with_credentials_looks_the_track_up_by_id(
    monkeypatch,
):
    calls = []

    class FakeApi:
        def track(self, arg):
            calls.append(arg)
            return {"name": "T", "artists": [{"name": "A"}]}

    monkeypatch.setattr(linkutils, "spotify_api", FakeApi())
    monkeypatch.setattr(
        loader, "_search_youtube", lambda query: [{"url": "u"}]
    )
    track_id = "4cOdK2wGLETKBW3PvgPWqT"
    asyncio.run(
        linkutils.fetch_spotify(f"HTTPS://OPEN.SPOTIFY.COM/track/{track_id}")
    )
    assert calls == [track_id]
