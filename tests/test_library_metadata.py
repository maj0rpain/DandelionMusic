"""Library enrichment from Last.fm and Spotify, driven through the
public entry points over canned responses.

No HTTP and no real spotipy: Last.fm's session is a fake that answers
from canned response dicts, and `library_metadata._enrichment_client`
is a fake enrichment Spotify client. The sample file does not exist,
so the tag read falls back to the folder names and there is no
embedded cover.

Synchronous test functions running their coroutine through
asyncio.run(): the dev group has no pytest-asyncio.
"""

import asyncio

import pytest

from config import config
from musicbot import library_metadata, linkutils
from musicbot.library_metadata import ArtInfo, ExternalStats

PLACEHOLDER = (
    "https://lastfm.freetls.fastly.net/i/u/300x300/"
    "2a96cbd8b46e442fc41c2b86b821562f.png"
)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


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

    def __init__(
        self, artists=(), albums=(), album_detail=None, search_error=None
    ):
        self.search_error = search_error
        self.artists = list(artists)
        self.albums = list(albums)
        self.album_detail = album_detail or {}
        self.searches = []

    def search(self, q, type, limit):
        self.searches.append((type, q))
        if self.search_error is not None:
            raise self.search_error
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


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """A controllable clock for the four lookups' cooldowns."""
    c = Clock()
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
        monkeypatch.setattr(library_metadata, "_enrichment_client", client)
        return client

    return install


@pytest.fixture
def no_spotify(monkeypatch):
    monkeypatch.setattr(library_metadata, "_enrichment_client", None)


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


def test_an_album_timeout_is_logged_with_label_and_key(
    tmp_path, lastfm, no_spotify, capsys
):
    lastfm({"album.getInfo": asyncio.TimeoutError()})

    album(tmp_path)

    assert (
        "library_metadata: Last.fm album.getInfo timed out for "
        "('Radiohead', 'OK Computer')" in capsys.readouterr().err
    )


def test_the_same_album_twice_calls_each_backend_once(
    tmp_path, lastfm, spotify
):
    session = lastfm({"album.getInfo": {"album": {"listeners": "5"}}})
    client = spotify(FakeSpotify(albums=[{"images": []}]))

    album(tmp_path)
    album(tmp_path)

    assert len(session.queries) == 1
    assert len(client.searches) == 1


# a 1x1 PNG-ish payload; mutagen stores the bytes verbatim
COVER = b"\x89PNG\r\n\x1a\nembedded-cover-bytes"


def make_tagged_flac(path):
    """A minimal FLAC (marker + STREAMINFO) tagged through mutagen."""
    from mutagen.flac import FLAC, Picture

    streaminfo = (
        (4096).to_bytes(2, "big")
        + (4096).to_bytes(2, "big")
        + b"\x00" * 6
        + ((44100 << 44) | (1 << 41) | (15 << 36) | 44100).to_bytes(8, "big")
        + b"\x00" * 16
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + streaminfo)
    flac = FLAC(path)
    flac["artist"] = "Tag Artist"
    flac["albumartist"] = "Tag Album Artist"
    flac["album"] = "Tag Album"
    pic = Picture()
    pic.type = 3
    pic.mime = "image/png"
    pic.data = COVER
    flac.add_picture(pic)
    flac.save()


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
    sample.parent.mkdir(parents=True)
    make_tagged_flac(sample)

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
        names, art, elapsed = asyncio.run(scenario())
    finally:
        release.set()

    assert elapsed < 1
    assert names == ("Tag Album Artist", "Tag Album")
    assert art is not None and art.data == COVER


def test_low_limit_client_shares_the_auth_manager_with_low_limits():
    import inspect

    from spotipy import Spotify

    auth = object()
    source = Spotify(auth_manager=auth)

    client = library_metadata._low_limit_client(source)

    assert client is not source
    assert client.auth_manager is auth
    assert client.requests_timeout == 3
    assert client.retries == 0
    assert client.status_retries == 0
    params = inspect.signature(Spotify.__init__).parameters
    for name in ("requests_timeout", "retries", "status_retries"):
        assert getattr(source, name) == params[name].default, name


def test_low_limit_client_of_none_is_none():
    assert library_metadata._low_limit_client(None) is None


def test_spotify_enrichment_is_disabled_without_a_client(no_spotify):
    assert library_metadata._spotify_enabled() is False


def test_the_import_built_enrichment_client_is_none_without_spotify_api():
    if library_metadata.spotify_api is not None:
        pytest.skip("Spotify credentials configured")
    assert library_metadata._enrichment_client is None


def test_the_import_built_enrichment_client_follows_spotify_api():
    api = library_metadata.spotify_api
    if api is None:
        pytest.skip("no Spotify credentials configured")
    client = library_metadata._enrichment_client
    assert client is not api
    assert client.auth_manager is api.auth_manager
    assert client.requests_timeout == 3
    assert client.retries == 0
    assert client.status_retries == 0


def test_a_failed_spotify_artist_search_is_logged(
    tmp_path, spotify, no_lastfm, capsys
):
    spotify(FakeSpotify(search_error=RuntimeError("boom")))

    artist(tmp_path)

    assert (
        "library_metadata: Spotify artist failed for Radiohead: boom"
        in capsys.readouterr().err.splitlines()
    )


def test_a_failed_spotify_album_detail_is_logged(
    tmp_path, spotify, no_lastfm, capsys
):
    spotify(
        FakeSpotify(
            albums=[{"id": "abc", "images": []}],
            album_detail=RuntimeError("rate limited"),
        )
    )

    album(tmp_path)

    assert (
        "library_metadata: Spotify album failed for "
        "('Radiohead', 'OK Computer'): rate limited"
        in capsys.readouterr().err.splitlines()
    )


def test_playback_spotify_client_keeps_default_limits():
    api = linkutils.spotify_api
    if api is None:
        pytest.skip("no Spotify credentials configured")
    import inspect

    from spotipy import Spotify

    params = inspect.signature(Spotify.__init__).parameters
    for name in ("requests_timeout", "retries", "status_retries"):
        assert getattr(api, name) == params[name].default, name
