"""Library enrichment, driven through a test-built `Enricher`'s
`get_enrichment` over canned responses and real tagged files.

No HTTP: Last.fm's session is a fake that answers from canned response
dicts, and the Enricher's Spotify client is a fake enrichment client,
except in the Spotify client-limit tests, which build real spotipy
`Spotify` clients (no request is sent) or read the playback client.
The library is real: MP3s written under `tmp_path` and indexed by
`library.build_index()`. By default they are untagged, so the tag read
falls back to the folder names and there is no embedded cover. Local
reads are counted, held and failed by wrapping the public `read_tags`
and `read_artwork` names `library_metadata` imports.
"""

import asyncio
import inspect
import threading
import time

import pytest
from mutagen.id3 import APIC, ID3, TALB, TPE1, TPE2, PictureType
from spotipy import Spotify

from config import config
from musicbot import audiotags, library, library_metadata, linkutils
from musicbot.library_metadata import ArtInfo, Enrichment, ExternalStats
from helpers import FakeClock, run

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


LAYOUT = {"Radiohead": {"OK Computer": ["01.mp3"]}}
MP3_FRAMES = (b"\xff\xfb\x90\x00" + b"\x00" * 413) * 20


def write_mp3(path, album_artist=None, artist=None, album=None, cover=None):
    """A real MP3 at `path`, tagged with mutagen; `cover` is the bytes
    of a front-cover PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAMES)
    tags = ID3()
    for frame, value in ((TPE2, album_artist), (TPE1, artist), (TALB, album)):
        if value is not None:
            tags.add(frame(encoding=3, text=[value]))
    if cover is not None:
        tags.add(
            APIC(
                encoding=3,
                mime="image/png",
                type=PictureType.COVER_FRONT,
                desc="",
                data=cover,
            )
        )
    tags.save(path)


@pytest.fixture
def make_library(tmp_path, monkeypatch):
    """Builds a real library index under tmp_path from
    {artist: {album: [filename, ...]}}; files are untagged MP3s unless
    written over with `write_mp3` first, and are returned with it."""
    root = tmp_path / "library"
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(root))
    monkeypatch.setattr(config, "LIBRARY_EXTENSIONS", (".mp3",))

    def build(layout):
        for artist_name, albums in layout.items():
            for album_name, filenames in albums.items():
                for filename in filenames:
                    path = root / artist_name / album_name / filename
                    if not path.exists():
                        write_mp3(path)
        return library.build_index()

    yield build
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", "")
    library.build_index()


class Rig:
    """A test-built Enricher over a built library, driven through
    get_enrichment only. It is built on first use, so the fixtures
    below can install its fakes first."""

    def __init__(self, index):
        self.index = index
        self.clock = FakeClock()
        self.spotify_client = None
        self.session = None
        self._enricher = None

    @property
    def enricher(self):
        if self._enricher is None:
            self._enricher = library_metadata.Enricher(
                self.clock, self.spotify_client, lambda: self.session
            )
        return self._enricher

    def get(self, artist_name, album_name=None):
        return run(
            self.enricher.get_enrichment(self.index, artist_name, album_name)
        )

    def artist(self, name="Radiohead"):
        return self.get(name)

    def album(self, name="Radiohead", title="OK Computer"):
        return self.get(name, title)


@pytest.fixture
def rig(make_library):
    return Rig(make_library(LAYOUT))


@pytest.fixture
def tagged_rig(make_library):
    """Builds a Rig over real tagged MP3s. `tags` maps (artist, album,
    filename) to `write_mp3`'s keyword arguments; `spotify` is the fake
    installed, a fresh `FakeSpotify` when None."""

    def build(tags, layout=LAYOUT, spotify=None):
        make_library(layout)
        for (artist_name, album_name, filename), kwargs in tags.items():
            write_mp3(
                library.song_path(artist_name, album_name, filename), **kwargs
            )
        rig = Rig(library.build_index())
        rig.spotify_client = spotify or FakeSpotify()
        return rig

    return build


@pytest.fixture
def lastfm(rig, monkeypatch):
    """Installs a fake Last.fm session; call it with the canned
    responses by method."""
    monkeypatch.setattr(config, "LASTFM_API_KEY", "test-key")

    def install(responses):
        rig.session = FakeLastfm(responses)
        return rig.session

    return install


@pytest.fixture
def clock(rig):
    """The controllable clock of the Enricher's six lookups."""
    return rig.clock


@pytest.fixture
def spotify(rig):
    def install(client):
        rig.spotify_client = client
        return client

    return install


@pytest.fixture
def no_spotify(rig):
    rig.spotify_client = None


@pytest.fixture
def no_lastfm(monkeypatch):
    monkeypatch.setattr(config, "LASTFM_API_KEY", "")


def test_lastfm_artist_reads_its_counters_under_stats(rig, lastfm, no_spotify):
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

    result = rig.artist()

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
    rig, lastfm, no_spotify
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

    result = rig.album()

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


def test_lastfm_placeholder_image_is_rejected(rig, lastfm, no_spotify):
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

    result = rig.artist()

    assert result.art is None
    assert result.stats.listeners == 10


def test_lastfm_zero_and_unparseable_counters_are_absent(
    rig, lastfm, no_spotify
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

    stats = rig.artist().stats

    assert (stats.listeners, stats.playcount, stats.tags) == (
        None,
        None,
        ("jazz",),
    )


def test_lastfm_empty_string_tag_container_means_no_tags(
    rig, lastfm, no_spotify
):
    lastfm(
        {
            "artist.getInfo": {
                "artist": {"stats": {"listeners": "7"}, "tags": ""}
            }
        }
    )

    assert rig.artist().stats.tags == ()


def test_lastfm_lone_tag_dict_is_one_tag(rig, lastfm, no_spotify):
    lastfm(
        {
            "album.getInfo": {
                "album": {"toptags": {"tag": {"name": "shoegaze"}}}
            }
        }
    )

    assert rig.album().stats.tags == ("shoegaze",)


def test_lastfm_all_empty_node_is_no_match(rig, lastfm, no_spotify):
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

    result = rig.artist()

    assert (result.stats, result.art) == (None, None)


def test_a_lastfm_parse_error_loses_only_lastfm(rig, lastfm, spotify):
    # #16's deliberate behaviour change: a malformed Last.fm response
    # is a transient Last.fm failure, not the whole enrichment's
    lastfm({"artist.getInfo": {"artist": {"image": "not-a-list"}}})
    spotify(FakeSpotify(artists=[{"popularity": 70, "images": []}]))

    stats = rig.artist().stats

    assert stats.popularity == 70
    assert stats.listeners is None


def test_spotify_artist(rig, spotify, no_lastfm):
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

    result = rig.artist()

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


def test_spotify_album(rig, spotify, no_lastfm):
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

    result = rig.album()

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
    rig, spotify, no_lastfm, clock
):
    client = spotify(
        FakeSpotify(
            albums=[{"id": "abc", "images": [], "release_date": "2000"}],
            album_detail=RuntimeError("rate limited"),
        )
    )

    assert rig.album().stats is None
    rig.album()
    assert len(client.searches) == 1  # in cooldown, nothing cached

    clock.now += 61
    client.album_detail = {"popularity": 77}
    stats = rig.album().stats

    assert len(client.searches) == 2
    assert (stats.popularity, stats.release_date) == (77, "2000")


LASTFM_CALLS = {
    "artist": lambda rig: rig.artist(),
    "album": lambda rig: rig.album(),
}


@pytest.mark.parametrize("kind", ["artist", "album"])
def test_lastfm_error_6_is_cached_as_no_match(
    rig, lastfm, no_spotify, clock, kind
):
    session = lastfm(
        {
            "artist.getInfo": {"error": 6, "message": "not found"},
            "album.getInfo": {"error": 6, "message": "not found"},
        }
    )

    first = LASTFM_CALLS[kind](rig)
    clock.now += 61  # past any failure cooldown: only a cache answers
    LASTFM_CALLS[kind](rig)

    assert (first.stats, first.art) == (None, None)
    assert len(session.queries) == 1


@pytest.mark.parametrize("kind", ["artist", "album"])
def test_other_lastfm_error_codes_are_retried_after_cooldown(
    rig, lastfm, no_spotify, clock, kind
):
    session = lastfm(
        {
            "artist.getInfo": {"error": 10, "message": "bad key"},
            "album.getInfo": {"error": 10, "message": "bad key"},
        }
    )

    LASTFM_CALLS[kind](rig)
    LASTFM_CALLS[kind](rig)
    assert len(session.queries) == 1
    clock.now += 61
    LASTFM_CALLS[kind](rig)

    assert len(session.queries) == 2


@pytest.mark.parametrize("kind", ["artist", "album"])
def test_lastfm_non_200_status_is_retried_after_cooldown(
    rig, lastfm, no_spotify, clock, kind
):
    session = lastfm({"artist.getInfo": {}, "album.getInfo": {}})
    real_get = session.get

    def get(url, params):
        response = real_get(url, params)
        response.status = 503
        return response

    session.get = get

    LASTFM_CALLS[kind](rig)
    clock.now += 61
    LASTFM_CALLS[kind](rig)

    assert len(session.queries) == 2


def test_a_timeout_is_logged_with_label_and_key(
    rig, lastfm, no_spotify, capsys
):
    # aiohttp reports a read timeout as an asyncio.TimeoutError
    lastfm({"artist.getInfo": asyncio.TimeoutError()})

    rig.artist()

    assert (
        "library_metadata: Last.fm artist.getInfo timed out for Radiohead"
        in capsys.readouterr().err
    )


def test_an_album_timeout_is_logged_with_label_and_key(
    rig, lastfm, no_spotify, capsys
):
    lastfm({"album.getInfo": asyncio.TimeoutError()})

    rig.album()

    assert (
        "library_metadata: Last.fm album.getInfo timed out for "
        "('Radiohead', 'OK Computer')" in capsys.readouterr().err
    )


class CountingReads:
    """Wrappers around the real `read_tags` and `read_artwork` that
    `library_metadata` imports, counting reads per path and optionally
    failing them; installed on the module."""

    def __init__(self, monkeypatch, artwork=None):
        self.tag_reads = []
        self.artwork_reads = []
        self.fail = set()  # "read_tags" and/or "read_artwork"
        self._artwork = artwork or audiotags.read_artwork
        monkeypatch.setattr(library_metadata, "read_tags", self.read_tags)
        monkeypatch.setattr(
            library_metadata, "read_artwork", self.read_artwork
        )

    def read_tags(self, path):
        self.tag_reads.append(path)
        if "read_tags" in self.fail:
            raise OSError("unreadable")
        return audiotags.read_tags(path)

    def read_artwork(self, path):
        self.artwork_reads.append(path)
        if "read_artwork" in self.fail:
            raise OSError("unreadable")
        return self._artwork(path)


@pytest.fixture
def reads(monkeypatch):
    return CountingReads(monkeypatch)


def test_stalled_spotify_does_not_starve_file_reads(
    make_library, spotify, no_lastfm, monkeypatch
):
    release = threading.Event()
    entered = threading.Semaphore(0)

    class BlockingSpotify(FakeSpotify):
        def search(self, q, type, limit):
            entered.release()
            release.wait(30)
            return super().search(q, type, limit)

    layout = {f"Stalled {i}": {"A": ["01.mp3"]} for i in range(4)}
    layout["Folder Artist"] = {"Folder Album": ["01.mp3"]}
    rig = Rig(make_library(layout))
    spotify_client = BlockingSpotify()
    rig.spotify_client = spotify_client
    finished = {}
    wrapped = CountingReads(monkeypatch)

    async def scenario():
        loop = asyncio.get_running_loop()
        both = asyncio.Event()

        def stamp(name, real):
            def read(path):
                result = real(path)
                finished[name] = time.monotonic()
                if len(finished) == 2:
                    loop.call_soon_threadsafe(both.set)
                return result

            return read

        monkeypatch.setattr(
            library_metadata, "read_tags", stamp("tags", wrapped.read_tags)
        )
        monkeypatch.setattr(
            library_metadata,
            "read_artwork",
            stamp("artwork", wrapped.read_artwork),
        )
        stalled = [
            asyncio.ensure_future(
                rig.enricher.get_enrichment(rig.index, f"Stalled {i}", None)
            )
            for i in range(4)
        ]
        # these four tag reads run on the file pool; the Spotify
        # searches then fill the network pool
        for _ in range(4):
            assert await loop.run_in_executor(None, entered.acquire, True, 5)
        started = time.monotonic()
        album_level = asyncio.ensure_future(
            rig.enricher.get_enrichment(
                rig.index, "Folder Artist", "Folder Album"
            )
        )
        await asyncio.wait_for(both.wait(), timeout=2)
        release.set()
        await asyncio.gather(*stalled, album_level, return_exceptions=True)
        return started

    try:
        started = run(scenario())
    finally:
        release.set()

    assert max(finished.values()) - started < 1
    assert set(finished) == {"tags", "artwork"}


def assert_default_limits(client):
    params = inspect.signature(Spotify.__init__).parameters
    for name in ("requests_timeout", "retries", "status_retries"):
        assert getattr(client, name) == params[name].default, name


def test_low_limit_client_shares_the_auth_manager_with_low_limits():
    auth = object()
    source = Spotify(auth_manager=auth)

    client = library_metadata._low_limit_client(source)

    assert client is not source
    assert client.auth_manager is auth
    assert client.requests_timeout == 3
    assert client.retries == 0
    assert client.status_retries == 0
    assert_default_limits(source)


def test_low_limit_client_of_none_is_none():
    assert library_metadata._low_limit_client(None) is None


def test_spotify_enrichment_is_disabled_without_a_client(
    rig, no_spotify, no_lastfm
):
    result = rig.artist()

    assert result == Enrichment(stats=None, art=None)


def test_the_default_enricher_adds_no_spotify_enrichment_without_spotify_api(
    make_library, no_lastfm
):
    if library_metadata.spotify_api is not None:
        pytest.skip("Spotify credentials configured")
    index = make_library(LAYOUT)

    result = run(library_metadata.get_enrichment(index, "Radiohead", None))

    assert result == Enrichment(stats=None, art=None)


def test_a_failed_spotify_artist_search_is_logged(
    rig, spotify, no_lastfm, capsys
):
    spotify(FakeSpotify(search_error=RuntimeError("boom")))

    rig.artist()

    assert (
        "library_metadata: Spotify artist failed for Radiohead: boom"
        in capsys.readouterr().err.splitlines()
    )


def test_a_failed_spotify_album_detail_is_logged(
    rig, spotify, no_lastfm, capsys
):
    spotify(
        FakeSpotify(
            albums=[{"id": "abc", "images": []}],
            album_detail=RuntimeError("rate limited"),
        )
    )

    rig.album()

    assert (
        "library_metadata: Spotify album failed for "
        "('Radiohead', 'OK Computer'): rate limited"
        in capsys.readouterr().err.splitlines()
    )


def test_playback_spotify_client_keeps_default_limits():
    api = linkutils.spotify_api
    if api is None:
        pytest.skip("no Spotify credentials configured")
    assert_default_limits(api)


# --- scopes, names and art, through get_enrichment on real files -------


def test_the_root_has_no_enrichment(rig, lastfm):
    session = lastfm({})

    assert rig.get(None) is None
    assert session.queries == []


def test_a_missing_or_empty_scope_has_no_enrichment(rig):
    assert rig.get("Nobody") is None
    assert rig.get("Radiohead", "Nothing") is None
    assert rig.get("Nobody", "OK Computer") is None


def test_artist_and_album_scopes_ask_their_own_spotify_search(
    rig, spotify, no_lastfm
):
    client = spotify(FakeSpotify())

    rig.artist()
    rig.album()

    assert client.searches == [
        ("artist", "Radiohead"),
        ("album", "artist:Radiohead album:OK Computer"),
    ]


def test_the_artist_level_samples_the_first_album_in_casefold_order(
    tagged_rig, no_lastfm
):
    rig = tagged_rig(
        {
            ("Band", "Zeta", "01.mp3"): {"album_artist": "Tagged Zeta"},
            ("Band", "alpha", "01.mp3"): {"album_artist": "Tagged alpha"},
        },
        layout={"Band": {"Zeta": ["01.mp3"], "alpha": ["01.mp3"]}},
    )

    rig.artist("Band")

    assert rig.spotify_client.searches == [("artist", "Tagged alpha")]


def test_tags_name_the_album_and_beat_the_folder_names(tagged_rig, no_lastfm):
    rig = tagged_rig(
        {
            ("Radiohead", "OK Computer", "01.mp3"): {
                "album_artist": "Radiohead (tagged)",
                "artist": "Thom",
                "album": "OK Computer (tagged)",
            }
        }
    )

    rig.album()

    assert rig.spotify_client.searches == [
        (
            "album",
            "artist:Radiohead (tagged) album:OK Computer (tagged)",
        )
    ]


def test_the_artist_tag_stands_in_for_a_missing_album_artist(
    tagged_rig, no_lastfm
):
    rig = tagged_rig(
        {("Radiohead", "OK Computer", "01.mp3"): {"artist": "Thom"}}
    )

    rig.album()

    assert rig.spotify_client.searches == [
        ("album", "artist:Thom album:OK Computer")
    ]


def test_embedded_art_beats_remote_art(tagged_rig, no_lastfm):
    rig = tagged_rig(
        {("Radiohead", "OK Computer", "01.mp3"): {"cover": b"png-bytes"}},
        spotify=FakeSpotify(
            albums=[{"images": [{"url": "https://sp/a.jpg"}], "id": None}]
        ),
    )

    result = rig.album()

    assert result.art.data == b"png-bytes"
    assert result.art.url is None


def test_a_file_without_a_cover_is_read_once_across_two_calls(
    rig, reads, no_lastfm
):
    rig.album()
    rig.album()

    assert len(reads.artwork_reads) == 1
    assert len(reads.tag_reads) == 1


@pytest.mark.parametrize("failing", ["read_tags", "read_artwork"])
def test_a_failed_local_read_is_refused_until_the_cooldown_passes(
    rig, reads, clock, no_lastfm, failing
):
    reads.fail.add(failing)
    attempts = lambda: len(  # noqa: E731
        reads.tag_reads if failing == "read_tags" else reads.artwork_reads
    )

    first = rig.album()
    rig.album()
    assert attempts() == 1  # refused, not retried
    assert first == Enrichment(stats=None, art=None)

    clock.now += 61
    reads.fail.clear()
    rig.album()

    assert attempts() == 2


def test_a_failed_local_read_is_logged_with_its_label(
    rig, reads, no_lastfm, capsys
):
    reads.fail.add("read_artwork")

    rig.album()

    assert "library_metadata: reading artwork failed for " in (
        capsys.readouterr().err
    )


def build_many_albums(make_library, count):
    return make_library(
        {"Artist": {f"Album {i:03}": ["01.mp3"] for i in range(count)}}
    )


def visit_all(rig, count):
    for i in range(count):
        rig.get("Artist", f"Album {i:03}")


def test_no_cover_albums_are_remembered_up_to_512_and_the_513th_evicts(
    make_library, monkeypatch, no_lastfm
):
    rig = Rig(build_many_albums(make_library, 513))
    reads = CountingReads(monkeypatch)
    first = library.song_path("Artist", "Album 000", "01.mp3")

    visit_all(rig, 512)
    visit_all(rig, 512)
    assert len(reads.artwork_reads) == 512  # all remembered

    rig.get("Artist", "Album 512")
    rig.get("Artist", "Album 000")

    assert reads.artwork_reads.count(first) == 2  # the 513th evicted it


@pytest.mark.parametrize(
    "cover_size, reread", [(7 * 1024 * 1024, True), (1024, False)]
)
def test_covers_are_held_within_32_mb(
    make_library, monkeypatch, no_lastfm, cover_size, reread
):
    rig = Rig(build_many_albums(make_library, 5))
    reads = CountingReads(
        monkeypatch, artwork=lambda path: (b"x" * cover_size, "png")
    )
    first = library.song_path("Artist", "Album 000", "01.mp3")

    visit_all(rig, 5)
    rig.get("Artist", "Album 000")

    assert (reads.artwork_reads.count(first) == 2) is reread


def test_an_index_rebuild_clears_all_six_lookups(rig, reads, lastfm, spotify):
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

    def visit():
        rig.artist()
        rig.album()

    def hits():
        return (
            len(session.queries),
            len(client.searches),
            len(reads.tag_reads),
            len(reads.artwork_reads),
        )

    visit()
    first = hits()
    visit()
    assert hits() == first  # same generation: nothing cleared

    rig.index = library.build_index()
    visit()

    assert all(after > before for before, after in zip(first, hits()))
    assert len(session.queries) == 4
    assert len(client.searches) == 4
