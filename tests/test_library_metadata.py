"""Library enrichment from Last.fm and Spotify, driven through the
public entry points over canned responses.

No HTTP: Last.fm's session is a fake that answers from canned response
dicts, and `library_metadata.spotify_api` is a fake client, except in
the Spotify client-limit tests, which build real spotipy `Spotify`
clients (no request is sent) or read the playback client. The sample
file does not exist, so the tag read falls back to the folder names and
there is no embedded cover.
"""

import asyncio
import inspect

import pytest
from helpers import FakeClock, run
from spotipy import Spotify

from config import config
from musicbot import library_metadata, linkutils
from musicbot.library_metadata import ArtInfo, ExternalStats

PLACEHOLDER = (
    "https://lastfm.freetls.fastly.net/i/u/300x300/"
    "2a96cbd8b46e442fc41c2b86b821562f.png"
)


class FakeResponse:
    def __init__(self, data):
        self.status = 200
        self._data = data

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        if isinstance(self._data, BaseException):
            raise self._data
        return self

    async def __aexit__(self, *exc):
        return False


class FakeLastfm:
    """A ClientSession stand-in answering Last.fm's API from canned
    responses keyed by method, recording each query it receives."""

    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def get(self, url, params):
        self.queries.append(params)
        return FakeResponse(self.responses[params["method"]])


class FakeSpotify:
    """A spotipy client stand-in. `album_detail` is what `album()`
    returns, or raises when it is an exception."""

    def __init__(self, artists=(), albums=(), album_detail=None):
        self.artists = list(artists)
        self.albums = list(albums)
        self.album_detail = album_detail or {}
        self.searches = []

    def search(self, q, type, limit):
        self.searches.append((type, q))
        if type == "artist":
            return {"artists": {"items": self.artists}}
        return {"albums": {"items": self.albums}}

    def album(self, album_id):
        if isinstance(self.album_detail, BaseException):
            raise self.album_detail
        return self.album_detail


@pytest.fixture(autouse=True)
def fresh_caches():
    library_metadata.clear_caches()
    yield
    library_metadata.clear_caches()


@pytest.fixture
def lastfm(monkeypatch):
    """Installs a fake Last.fm session; call it with the canned
    responses by method."""
    monkeypatch.setattr(config, "LASTFM_API_KEY", "test-key")

    def install(responses):
        session = FakeLastfm(responses)
        monkeypatch.setattr(linkutils, "get_session", lambda: session)
        return session

    return install


@pytest.fixture
def clock(monkeypatch):
    """A controllable clock for the four lookups' cooldowns."""
    c = FakeClock()
    for name in (
        "_lastfm_artist",
        "_lastfm_album",
        "_spotify_artist",
        "_spotify_album",
    ):
        monkeypatch.setattr(getattr(library_metadata, name), "_clock", c)
    return c


@pytest.fixture
def spotify(monkeypatch):
    def install(client):
        monkeypatch.setattr(library_metadata, "spotify_api", client)
        return client

    return install


@pytest.fixture
def no_spotify(monkeypatch):
    monkeypatch.setattr(library_metadata, "spotify_api", None)


@pytest.fixture
def no_lastfm(monkeypatch):
    monkeypatch.setattr(config, "LASTFM_API_KEY", "")


def artist(tmp_path, name="Radiohead"):
    return run(
        library_metadata.get_artist_enrichment(name, tmp_path / "x.flac")
    )


def album(tmp_path, name="Radiohead", title="OK Computer"):
    return run(
        library_metadata.get_album_enrichment(name, title, tmp_path / "x.flac")
    )


def test_lastfm_artist_reads_its_counters_under_stats(
    tmp_path, lastfm, no_spotify
):
    lastfm(
        {
            "artist.getInfo": {
                "artist": {
                    "image": [
                        {"#text": "https://img/small.png"},
                        {"#text": "https://img/large.png"},
                    ],
                    "stats": {"listeners": "1200", "playcount": "34000"},
                    "tags": {"tag": [{"name": "rock"}, {"name": "alt"}]},
                }
            }
        }
    )

    result = artist(tmp_path)

    assert result.stats == ExternalStats(
        listeners=1200,
        playcount=34000,
        tags=("rock", "alt"),
        popularity=None,
        followers=None,
        release_date=None,
    )
    assert result.art == ArtInfo(
        url="https://img/large.png", data=None, extension=None
    )


def test_lastfm_album_reads_its_counters_at_the_top_level(
    tmp_path, lastfm, no_spotify
):
    session = lastfm(
        {
            "album.getInfo": {
                "album": {
                    "listeners": "500",
                    "playcount": "9000",
                    "image": [{"#text": "https://img/cover.png"}],
                    "tags": {"tag": [{"name": "art rock"}]},
                }
            }
        }
    )

    result = album(tmp_path)

    assert result.stats == ExternalStats(
        listeners=500,
        playcount=9000,
        tags=("art rock",),
        popularity=None,
        followers=None,
        release_date=None,
    )
    assert result.art == ArtInfo(
        url="https://img/cover.png", data=None, extension=None
    )
    assert session.queries[0]["artist"] == "Radiohead"
    assert session.queries[0]["album"] == "OK Computer"


def test_lastfm_placeholder_image_is_rejected(tmp_path, lastfm, no_spotify):
    lastfm(
        {
            "artist.getInfo": {
                "artist": {
                    "image": [{"#text": PLACEHOLDER}],
                    "stats": {"listeners": "10"},
                }
            }
        }
    )

    result = artist(tmp_path)

    assert result.art is None
    assert result.stats.listeners == 10


def test_lastfm_zero_and_unparseable_counters_are_absent(
    tmp_path, lastfm, no_spotify
):
    lastfm(
        {
            "artist.getInfo": {
                "artist": {
                    "stats": {"listeners": "0", "playcount": "lots"},
                    "tags": {"tag": [{"name": "jazz"}]},
                }
            }
        }
    )

    stats = artist(tmp_path).stats

    assert (stats.listeners, stats.playcount, stats.tags) == (
        None,
        None,
        ("jazz",),
    )


def test_lastfm_empty_string_tag_container_means_no_tags(
    tmp_path, lastfm, no_spotify
):
    lastfm(
        {
            "artist.getInfo": {
                "artist": {"stats": {"listeners": "7"}, "tags": ""}
            }
        }
    )

    assert artist(tmp_path).stats.tags == ()


def test_lastfm_lone_tag_dict_is_one_tag(tmp_path, lastfm, no_spotify):
    lastfm(
        {
            "album.getInfo": {
                "album": {"toptags": {"tag": {"name": "shoegaze"}}}
            }
        }
    )

    assert album(tmp_path).stats.tags == ("shoegaze",)


def test_lastfm_all_empty_node_is_no_match(tmp_path, lastfm, no_spotify):
    lastfm(
        {
            "artist.getInfo": {
                "artist": {
                    "image": [{"#text": ""}],
                    "stats": {"listeners": "0", "playcount": "0"},
                    "tags": "",
                }
            }
        }
    )

    result = artist(tmp_path)

    assert (result.stats, result.art) == (None, None)


def test_a_lastfm_parse_error_loses_only_lastfm(tmp_path, lastfm, spotify):
    # #16's deliberate behaviour change: a malformed Last.fm response
    # is a transient Last.fm failure, not the whole enrichment's
    lastfm({"artist.getInfo": {"artist": {"image": "not-a-list"}}})
    spotify(FakeSpotify(artists=[{"popularity": 70, "images": []}]))

    stats = artist(tmp_path).stats

    assert stats.popularity == 70
    assert stats.listeners is None


def test_spotify_artist(tmp_path, spotify, no_lastfm):
    spotify(
        FakeSpotify(
            artists=[
                {
                    "images": [{"url": "https://sp/artist.jpg"}],
                    "popularity": 81,
                    "followers": {"total": 5000000},
                }
            ]
        )
    )

    result = artist(tmp_path)

    assert result.stats == ExternalStats(
        listeners=None,
        playcount=None,
        tags=(),
        popularity=81,
        followers=5000000,
        release_date=None,
    )
    assert result.art == ArtInfo(
        url="https://sp/artist.jpg", data=None, extension=None
    )


def test_spotify_album(tmp_path, spotify, no_lastfm):
    client = spotify(
        FakeSpotify(
            albums=[
                {
                    "id": "abc",
                    "images": [{"url": "https://sp/album.jpg"}],
                    "release_date": "1997-05-21",
                }
            ],
            album_detail={"popularity": 77},
        )
    )

    result = album(tmp_path)

    assert result.stats == ExternalStats(
        listeners=None,
        playcount=None,
        tags=(),
        popularity=77,
        followers=None,
        release_date="1997-05-21",
    )
    assert result.art == ArtInfo(
        url="https://sp/album.jpg", data=None, extension=None
    )
    assert client.searches == [("album", "artist:Radiohead album:OK Computer")]


def test_spotify_album_detail_failure_is_retried_in_full(
    tmp_path, spotify, no_lastfm, clock
):
    client = spotify(
        FakeSpotify(
            albums=[{"id": "abc", "images": [], "release_date": "2000"}],
            album_detail=RuntimeError("rate limited"),
        )
    )

    assert album(tmp_path).stats is None
    album(tmp_path)
    assert len(client.searches) == 1  # in cooldown, nothing cached

    clock.now += 61
    client.album_detail = {"popularity": 77}
    stats = album(tmp_path).stats

    assert len(client.searches) == 2
    assert (stats.popularity, stats.release_date) == (77, "2000")


LASTFM_CALLS = {"artist": artist, "album": album}


@pytest.mark.parametrize("kind", ["artist", "album"])
def test_lastfm_error_6_is_cached_as_no_match(
    tmp_path, lastfm, no_spotify, clock, kind
):
    session = lastfm(
        {
            "artist.getInfo": {"error": 6, "message": "not found"},
            "album.getInfo": {"error": 6, "message": "not found"},
        }
    )

    first = LASTFM_CALLS[kind](tmp_path)
    clock.now += 61  # past any failure cooldown: only a cache answers
    LASTFM_CALLS[kind](tmp_path)

    assert (first.stats, first.art) == (None, None)
    assert len(session.queries) == 1


@pytest.mark.parametrize("kind", ["artist", "album"])
def test_other_lastfm_error_codes_are_retried_after_cooldown(
    tmp_path, lastfm, no_spotify, clock, kind
):
    session = lastfm(
        {
            "artist.getInfo": {"error": 10, "message": "bad key"},
            "album.getInfo": {"error": 10, "message": "bad key"},
        }
    )

    LASTFM_CALLS[kind](tmp_path)
    LASTFM_CALLS[kind](tmp_path)
    assert len(session.queries) == 1
    clock.now += 61
    LASTFM_CALLS[kind](tmp_path)

    assert len(session.queries) == 2


@pytest.mark.parametrize("kind", ["artist", "album"])
def test_lastfm_non_200_status_is_retried_after_cooldown(
    tmp_path, lastfm, no_spotify, clock, kind
):
    session = lastfm({"artist.getInfo": {}, "album.getInfo": {}})
    real_get = session.get

    def get(url, params):
        response = real_get(url, params)
        response.status = 503
        return response

    session.get = get

    LASTFM_CALLS[kind](tmp_path)
    clock.now += 61
    LASTFM_CALLS[kind](tmp_path)

    assert len(session.queries) == 2


def test_clear_caches_clears_all_four_lookups(tmp_path, lastfm, spotify):
    session = lastfm(
        {
            "artist.getInfo": {"artist": {"stats": {"listeners": "1"}}},
            "album.getInfo": {"album": {"listeners": "1"}},
        }
    )
    client = spotify(
        FakeSpotify(
            artists=[{"popularity": 1, "images": []}],
            albums=[{"images": [], "release_date": "2000"}],
        )
    )

    def lookups():
        artist(tmp_path)
        album(tmp_path)

    lookups()
    lookups()
    assert (len(session.queries), len(client.searches)) == (2, 2)

    library_metadata.clear_caches()
    lookups()

    assert sorted(q["method"] for q in session.queries) == [
        "album.getInfo",
        "album.getInfo",
        "artist.getInfo",
        "artist.getInfo",
    ]
    assert sorted(kind for kind, _ in client.searches) == [
        "album",
        "album",
        "artist",
        "artist",
    ]


def test_a_timeout_is_logged_with_label_and_key(
    tmp_path, lastfm, no_spotify, capsys
):
    # aiohttp reports a read timeout as an asyncio.TimeoutError
    lastfm({"artist.getInfo": asyncio.TimeoutError()})

    artist(tmp_path)

    assert (
        "library_metadata: Last.fm artist.getInfo timed out for Radiohead"
        in capsys.readouterr().err
    )


def test_stalled_spotify_does_not_starve_file_reads(
    tmp_path, spotify, no_lastfm
):
    import threading
    import time

    release = threading.Event()
    entered = threading.Semaphore(0)

    class BlockingSpotify(FakeSpotify):
        def search(self, q, type, limit):
            entered.release()
            release.wait(30)
            return super().search(q, type, limit)

    spotify(BlockingSpotify())
    sample = tmp_path / "Folder Artist" / "Folder Album" / "01.flac"

    async def scenario():
        stalled = [
            asyncio.ensure_future(
                library_metadata.get_artist_enrichment(
                    f"Stalled {i}", tmp_path / "x.flac"
                )
            )
            for i in range(4)
        ]
        loop = asyncio.get_running_loop()
        for _ in range(4):
            assert await loop.run_in_executor(None, entered.acquire, True, 5)
        start = time.monotonic()
        names = await asyncio.wait_for(
            library_metadata._resolve_names(
                "Folder Artist", "Folder Album", sample
            ),
            timeout=1,
        )
        art = await asyncio.wait_for(
            library_metadata._embedded_art(sample), timeout=1
        )
        elapsed = time.monotonic() - start
        release.set()
        await asyncio.gather(*stalled, return_exceptions=True)
        return names, art, elapsed

    try:
        names, art, elapsed = run(scenario())
    finally:
        release.set()

    assert elapsed < 1
    assert names == ("Folder Artist", "Folder Album")
    assert art is None


def assert_default_limits(client):
    params = inspect.signature(Spotify.__init__).parameters
    for name in ("requests_timeout", "retries", "status_retries"):
        assert getattr(client, name) == params[name].default, name


def test_enrichment_spotify_client_has_low_limits(monkeypatch):
    auth_one = object()
    auth_two = object()
    first = Spotify(auth_manager=auth_one)
    second = Spotify(auth_manager=auth_two)
    fake = FakeSpotify()

    monkeypatch.setattr(library_metadata, "spotify_api", first)
    client = library_metadata._spotify_client()
    assert client is not first
    assert client.auth_manager is auth_one
    assert client.requests_timeout == 3
    assert client.retries == 0
    assert client.status_retries == 0
    assert library_metadata._spotify_client() is client

    monkeypatch.setattr(library_metadata, "spotify_api", second)
    assert library_metadata._spotify_client().auth_manager is auth_two

    monkeypatch.setattr(library_metadata, "spotify_api", fake)
    assert library_metadata._spotify_client() is fake

    assert_default_limits(first)

    monkeypatch.setattr(library_metadata, "spotify_api", None)
    assert library_metadata._spotify_enabled() is False


def test_playback_spotify_client_keeps_default_limits():
    api = linkutils.spotify_api
    if api is None:
        pytest.skip("no Spotify credentials configured")
    assert_default_limits(api)
