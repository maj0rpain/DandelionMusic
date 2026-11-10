from bs4 import BeautifulSoup
import pytest

from musicbot import linkutils
from musicbot.linkutils import SpotifyPlaylistTypes, fetch_playlist_with_api
from musicbot.truncation import Truncation, TruncationReason
from helpers import assert_logged_with_traceback, run


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
    bad = {"track": {"external_urls": {}}}
    spotify([{"track": track(1)}, bad])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p").links

    assert links == [url(1)]
    assert f"Cannot extract URL from {bad}:" in capsys.readouterr().err


def test_album_tracks_resolve_in_order(spotify):
    spotify([track(1), track(2)])

    links = fetch_playlist_with_api(SpotifyPlaylistTypes.ALBUM, "a").links

    assert links == [url(1), url(2)]


class PagedSpotify(FakeSpotify):
    """Two pages of two tracks; `next` raises, or ends the paging."""

    def __init__(self, exc=None, first_raises=False, total=4):
        super().__init__([track(1), track(2)])
        self.total = total
        self.exc = exc
        self.first_raises = first_raises

    def _page(self):
        if self.first_raises:
            raise self.exc
        page = {"items": self._items}
        if self.total is not None:
            page["total"] = self.total
        return page

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
    assert result.truncation == Truncation(4, TruncationReason.API_ERROR)


def test_paging_failure_after_page_without_total_reports_none(monkeypatch):
    monkeypatch.setattr(
        linkutils,
        "spotify_api",
        PagedSpotify(RuntimeError("boom"), total=None),
    )

    result = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")

    assert result.links == [url(1), url(2)]
    assert result.truncation is None


def test_paging_failure_with_every_reported_track_fetched_reports_none(
    monkeypatch,
):
    monkeypatch.setattr(
        linkutils, "spotify_api", PagedSpotify(RuntimeError("boom"), total=2)
    )

    result = fetch_playlist_with_api(SpotifyPlaylistTypes.PLAYLIST, "p")

    assert result.links == [url(1), url(2)]
    assert result.truncation is None


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

    assert result.truncation is None


def _page(count, song_count):
    tags = "".join(
        f'<meta name="music:song" content="{url(n)}">' for n in range(count)
    )
    if song_count is not None:
        tags += f'<meta name="music:song_count" content="{song_count}">'
    return BeautifulSoup(f"<html><head>{tags}</head></html>", "html.parser")


@pytest.mark.parametrize(
    "count, expected",
    [
        (50, Truncation(50, TruncationReason.NO_API)),
        (30, None),
        (None, None),
    ],
)
def test_scrape_reports_song_count_as_total(monkeypatch, count, expected):
    monkeypatch.setattr(linkutils, "spotify_api", None)
    _public_page(monkeypatch, 30, count)

    result = _fetch()

    assert len(result.links) == 30
    assert result.truncation == expected


def test_fetch_spotify_with_credentials_looks_the_track_up_by_id(
    monkeypatch,
):
    calls = []

    class FakeApi:
        def track(self, arg):
            calls.append(arg)
            return {"name": "T", "artists": [{"name": "A"}]}

    monkeypatch.setattr(linkutils, "spotify_api", FakeApi())
    track_id = "4cOdK2wGLETKBW3PvgPWqT"
    result = run(
        linkutils.fetch_spotify(f"HTTPS://OPEN.SPOTIFY.COM/track/{track_id}")
    )
    assert calls == [track_id]
    assert result == linkutils.SpotifyTrack('T - A "Topic"')


class NothingSpotify(FakeSpotify):
    """The API answers every request with nothing."""

    def _page(self):
        return None


API_UNAVAILABLE_CASES = pytest.mark.parametrize(
    "api",
    [
        PagedSpotify(RuntimeError("boom"), first_raises=True),
        NothingSpotify([]),
    ],
    ids=["first-page-failure", "nothing"],
)


def _public_page(monkeypatch, listed, reported):
    """Installs a public page listing `listed` song links and reporting
    `reported` as its count (None for no count tag)."""

    async def get_soup(u):
        return _page(listed, reported)

    monkeypatch.setattr(linkutils, "get_soup", get_soup)


def _failing_page(monkeypatch, error):
    """Installs a public page whose read raises `error`."""

    async def get_soup(u):
        raise error

    monkeypatch.setattr(linkutils, "get_soup", get_soup)


class FailingTrackApi:
    """A Spotify API whose track lookup raises `error`."""

    def __init__(self, error):
        self.error = error

    def track(self, code):
        raise self.error


def _fetch(list_type="playlist"):
    return run(
        linkutils.fetch_spotify_playlist(
            "u", SpotifyPlaylistTypes(list_type), "p"
        )
    )


@pytest.mark.parametrize("list_type", ["playlist", "album"])
@API_UNAVAILABLE_CASES
def test_failed_api_falls_back_to_the_public_page(
    monkeypatch, capsys, api, list_type
):
    monkeypatch.setattr(linkutils, "spotify_api", api)
    _public_page(monkeypatch, 30, 50)

    result = _fetch(list_type)

    assert len(result.links) == 30
    assert result.truncation == Truncation(
        50, TruncationReason.API_UNAVAILABLE
    )
    err = capsys.readouterr().err
    assert_logged_with_traceback(
        err, f"ERROR: Spotify API returned error for {list_type} p:"
    )


@pytest.mark.parametrize("list_type", ["playlist", "album"])
def test_api_returning_nothing_raises_in_the_pure_reader(
    monkeypatch, list_type
):
    monkeypatch.setattr(linkutils, "spotify_api", NothingSpotify([]))

    with pytest.raises(linkutils.SpotifyApiReturnedNothing) as info:
        fetch_playlist_with_api(SpotifyPlaylistTypes(list_type), "p")

    assert f"for {list_type} p" in str(info.value)


def test_api_and_public_page_failing_raises_the_page_error(monkeypatch):
    api_error = RuntimeError("api")
    page_error = RuntimeError("page")
    monkeypatch.setattr(
        linkutils, "spotify_api", PagedSpotify(api_error, first_raises=True)
    )
    _failing_page(monkeypatch, page_error)

    with pytest.raises(RuntimeError) as info:
        _fetch()

    assert info.value is page_error
    assert info.value.__context__ is api_error


@pytest.mark.parametrize("list_type", ["playlist", "album"])
def test_paging_failure_after_page_one_does_not_scrape(
    monkeypatch, capsys, list_type
):
    monkeypatch.setattr(
        linkutils, "spotify_api", PagedSpotify(RuntimeError("boom"))
    )
    _failing_page(monkeypatch, AssertionError("scraped"))

    result = _fetch(list_type)

    assert result.truncation == Truncation(4, TruncationReason.API_ERROR)
    err = capsys.readouterr().err
    assert f"ERROR: Spotify API returned error for {list_type} p:" in err


def test_track_whose_api_lookup_raises_falls_back_to_the_page_title(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        linkutils, "spotify_api", FailingTrackApi(RuntimeError("boom"))
    )

    async def get_soup(u):
        return BeautifulSoup(
            "<title>T - song and lyrics by A | Spotify</title>",
            "html.parser",
        )

    monkeypatch.setattr(linkutils, "get_soup", get_soup)

    result = run(linkutils.fetch_spotify("https://open.spotify.com/track/abc"))

    assert result == linkutils.SpotifyTrack('T - A "Topic"')
    err = capsys.readouterr().err
    assert_logged_with_traceback(
        err, "ERROR: Spotify API returned error for track abc:"
    )


def test_track_without_api_reads_the_page_title_and_logs_no_error(
    monkeypatch, capsys
):
    monkeypatch.setattr(linkutils, "spotify_api", None)

    async def get_soup(u):
        return BeautifulSoup(
            "<title>T - song and lyrics by A | Spotify</title>",
            "html.parser",
        )

    monkeypatch.setattr(linkutils, "get_soup", get_soup)

    result = run(linkutils.fetch_spotify("https://open.spotify.com/track/abc"))

    assert result == linkutils.SpotifyTrack('T - A "Topic"')
    assert capsys.readouterr().err == ""


def test_track_api_and_public_page_failing_raises_the_page_error(
    monkeypatch,
):
    api_error = RuntimeError("api")
    page_error = RuntimeError("page")

    monkeypatch.setattr(linkutils, "spotify_api", FailingTrackApi(api_error))
    _failing_page(monkeypatch, page_error)

    with pytest.raises(RuntimeError) as info:
        run(linkutils.fetch_spotify("https://open.spotify.com/track/abc"))

    assert info.value is page_error
    assert info.value.__context__ is api_error
