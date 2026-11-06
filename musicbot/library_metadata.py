import asyncio
import functools
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, NamedTuple, Optional, Tuple

from spotipy import Spotify

from config import config
from musicbot import library, linkutils
from musicbot.linkutils import spotify_api
from musicbot.audiotags import read_artwork, read_tags
from musicbot.cached_lookup import ByteBoundedStore, CachedLookup


# prefix of every line this module logs and of its lookup labels
_LOG_PREFIX = "library_metadata: "

# Dedicated pools (not the loop's shared default executor) for the
# blocking calls this module makes. asyncio.wait_for()'s timeout only
# abandons the await, not the underlying thread, so slow or hung calls
# pile up threads. Local mutagen reads are short and Spotify's
# synchronous HTTP client can be slow or rate-limited, so they get a
# pool each: a stalled Spotify can never delay a tag or artwork read.
_file_executor = ThreadPoolExecutor(max_workers=4)
_network_executor = ThreadPoolExecutor(max_workers=4)

# limits of the enrichment-only Spotify client: a slow or rate-limited
# Spotify gives its thread back quickly, and the lookup cooldown takes
# over
_SPOTIFY_TIMEOUT = 3


def _low_limit_client(api: Optional[Spotify]) -> Optional[Spotify]:
    """A new client sharing `api`'s auth manager with the enrichment
    limits; None for None."""
    if api is None:
        return None
    return Spotify(
        auth_manager=api.auth_manager,
        requests_timeout=_SPOTIFY_TIMEOUT,
        retries=0,
        status_retries=0,
    )


class ArtInfo(NamedTuple):
    url: Optional[str]  # a real URL - use with embed.set_thumbnail(url=...)
    data: Optional[bytes]  # raw embedded bytes - needs discord.File
    extension: Optional[str]  # "jpeg"/"png"/etc, set only when data is set


class _AlbumKey(NamedTuple):
    """One artist/album pair, keying the album lookups' caches."""

    artist: str
    album: str

    def __repr__(self) -> str:
        return tuple.__repr__(self)


class _LastfmInfo(NamedTuple):
    image_url: Optional[str]
    listeners: Optional[int]
    playcount: Optional[int]
    tags: Tuple[str, ...]


# since Last.fm's 2019 image-licensing change, artist.getInfo returns
# this one grey-star placeholder as the image for every artist - a real
# URL, so it has to be rejected by hash rather than by being absent
_PLACEHOLDER_IMAGE = "2a96cbd8b46e442fc41c2b86b821562f"


def _as_int(value) -> Optional[int]:
    """Last.fm returns its counters as strings, and as "0" for an
    entity nobody has ever played - neither is worth showing."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _lastfm_tags(node: dict) -> Tuple[str, ...]:
    """Community tags for an entity. Last.fm serves these as
    {"tag": [...]}, but collapses the container to an empty string
    when there are none and unwraps a lone tag into a bare object."""
    container = node.get("tags") or node.get("toptags") or {}
    if not isinstance(container, dict):
        return ()
    tags = container.get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    names = [
        tag["name"]
        for tag in tags
        if isinstance(tag, dict) and tag.get("name")
    ]
    return tuple(names[:3])


async def _fetch_lastfm(session, method: str, params: Dict[str, str]) -> dict:
    query = {
        "method": method,
        "api_key": config.LASTFM_API_KEY,
        "format": "json",
        **params,
    }
    async with session.get(
        "https://ws.audioscrobbler.com/2.0/", params=query
    ) as response:
        # a non-200 (503 outage, 429 rate limit, ...) is transient -
        # raise rather than returning "nothing found", so the caller
        # marks it transient instead of caching a permanent no-match
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        data = await response.json(content_type=None)
    # Last.fm returns HTTP 200 even for a bad/expired API key or
    # rate limiting - the failure only shows up as an "error" field
    # in the body, which would otherwise silently look like "no match"
    if "error" in data:
        # error 6 is "not found": a definite answer, returned as an
        # empty body so the parser maps it to a (cacheable) no-match
        if data.get("error") == 6:
            return {}
        raise RuntimeError(f"{data.get('message')} (code {data.get('error')})")
    return data


def _parse_lastfm(data: dict) -> Optional[_LastfmInfo]:
    """The facts worth keeping from an artist.getInfo or album.getInfo
    response, or None when it carries none of them. A malformed
    response raises, which the lookup treats as a transient failure."""
    node = data.get("artist") or data.get("album")
    if not node:
        return None
    # artist.getInfo nests its counters under "stats";
    # album.getInfo puts the same keys at the top level
    counters = node.get("stats") or node
    listeners = _as_int(counters.get("listeners"))
    playcount = _as_int(counters.get("playcount"))
    tags = _lastfm_tags(node)

    images = node.get("image") or []
    image_url = next(
        (
            img["#text"]
            for img in reversed(images)
            if img.get("#text") and _PLACEHOLDER_IMAGE not in img["#text"]
        ),
        None,
    )
    if not (image_url or listeners or playcount or tags):
        return None
    return _LastfmInfo(
        image_url=image_url,
        listeners=listeners,
        playcount=playcount,
        tags=tags,
    )


class _SpotifyInfo(NamedTuple):
    image_url: Optional[str]
    popularity: Optional[int]  # 0-100
    followers: Optional[int]  # artist level only
    release_date: Optional[str]  # album level only


class ExternalStats(NamedTuple):
    """The facts worth showing from the online backends. Every field
    is independently optional - a library with no API keys configured
    gets None for the whole struct, and a partial match fills in only
    what the responding backend knew."""

    listeners: Optional[int]  # Last.fm
    playcount: Optional[int]  # Last.fm scrobbles
    tags: Tuple[str, ...]  # Last.fm community tags
    popularity: Optional[int]  # Spotify, 0-100
    followers: Optional[int]  # Spotify, artist level
    release_date: Optional[str]  # Spotify, album level


class Enrichment(NamedTuple):
    stats: Optional[ExternalStats]
    art: Optional[ArtInfo]


def _external_stats(
    spotify: Optional[_SpotifyInfo], lastfm: Optional[_LastfmInfo]
) -> Optional[ExternalStats]:
    if spotify is None and lastfm is None:
        return None
    return ExternalStats(
        listeners=lastfm.listeners if lastfm else None,
        playcount=lastfm.playcount if lastfm else None,
        tags=lastfm.tags if lastfm else (),
        popularity=spotify.popularity if spotify else None,
        followers=spotify.followers if spotify else None,
        release_date=spotify.release_date if spotify else None,
    )


def _remote_art(
    spotify: Optional[_SpotifyInfo], lastfm: Optional[_LastfmInfo]
) -> Optional[ArtInfo]:
    """Spotify's image wins over Last.fm's - the same preference the
    two separate per-backend lookups encoded before."""
    if spotify and spotify.image_url:
        return ArtInfo(url=spotify.image_url, data=None, extension=None)
    if lastfm and lastfm.image_url:
        return ArtInfo(url=lastfm.image_url, data=None, extension=None)
    return None


def _spotify_artist_sync(client: Spotify, name: str) -> Optional[_SpotifyInfo]:
    results = client.search(q=name, type="artist", limit=1)
    items = results.get("artists", {}).get("items", [])
    if not items:
        return None
    item = items[0]
    images = item.get("images", [])
    return _SpotifyInfo(
        image_url=images[0]["url"] if images else None,
        popularity=item.get("popularity"),
        followers=(item.get("followers") or {}).get("total"),
        release_date=None,
    )


def _spotify_album_sync(
    client: Spotify, key: _AlbumKey
) -> Optional[_SpotifyInfo]:
    query = f"artist:{key.artist} album:{key.album}"
    results = client.search(q=query, type="album", limit=1)
    items = results.get("albums", {}).get("items", [])
    if not items:
        return None
    item = items[0]
    images = item.get("images", [])
    # a search only yields simplified album objects, which carry no
    # popularity - that lives on the full object and needs its own
    # fetch. A failed fetch propagates: the whole album lookup is then
    # a transient failure, retried after the cooldown, rather than a
    # popularity-less success cached until restart.
    popularity = None
    album_id = item.get("id")
    if album_id:
        popularity = client.album(album_id).get("popularity")
    return _SpotifyInfo(
        image_url=images[0]["url"] if images else None,
        popularity=popularity,
        followers=None,
        release_date=item.get("release_date"),
    )


class _Tags(NamedTuple):
    """The tag fields the query names are derived from."""

    album_artist: Optional[str]
    artist: Optional[str]
    album: Optional[str]


# Embedded covers are the one thing worth caching that is also big
# enough to be worth bounding: read_artwork() admits up to 5 MB per
# album, and a browsing session can walk hundreds of them.
_ART_CACHE_BUDGET = 32 * 1024 * 1024
# A second bound, on entry count: "this album has no embedded cover"
# weighs nothing, so the byte budget alone does not bound how many of
# them accumulate.
_ART_CACHE_MAX_ENTRIES = 512


def _art_size(art: Optional[ArtInfo]) -> int:
    return len(art.data) if art is not None and art.data else 0


async def _off_loop(executor, fn, *args):
    """Runs `fn(*args)` in `executor`, off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(
        executor, fn, *args
    )


class Enricher:
    """The enrichment for a scope of a library snapshot, and the six
    lookups behind it: Last.fm and Spotify for an artist and for an
    album, plus the tag read and the artwork read of the scope's
    sample file.

    - `clock`: monotonic seconds, shared by all six lookups.
    - `spotify`: the enrichment Spotify client, or None to turn
      Spotify off; exposed as the read-only `spotify` attribute.
    - `lastfm_session`: zero-argument getter of the HTTP session used
      for Last.fm. Whether Last.fm is on is a live read of
      `config.LASTFM_API_KEY`.

    An index rebuild (a change of `library.index_generation()`)
    clears every lookup at the start of the next `get_enrichment`."""

    def __init__(
        self,
        clock: Callable[[], float],
        spotify: Optional[Spotify],
        lastfm_session: Callable[[], object],
    ) -> None:
        self._spotify = spotify
        self._lastfm_session = lastfm_session
        self._generation = library.index_generation()
        self._lastfm_artist: CachedLookup[str, _LastfmInfo] = CachedLookup(
            self._lastfm_enabled,
            self._lastfm_artist_backend,
            _LOG_PREFIX + "Last.fm artist.getInfo",
            clock=clock,
        )
        self._lastfm_album: CachedLookup[_AlbumKey, _LastfmInfo] = (
            CachedLookup(
                self._lastfm_enabled,
                self._lastfm_album_backend,
                _LOG_PREFIX + "Last.fm album.getInfo",
                clock=clock,
            )
        )
        self._spotify_artist: CachedLookup[str, _SpotifyInfo] = CachedLookup(
            self._spotify_enabled,
            self._spotify_artist_backend,
            _LOG_PREFIX + "Spotify artist",
            clock=clock,
        )
        self._spotify_album: CachedLookup[_AlbumKey, _SpotifyInfo] = (
            CachedLookup(
                self._spotify_enabled,
                self._spotify_album_backend,
                _LOG_PREFIX + "Spotify album",
                clock=clock,
            )
        )
        self._tags: CachedLookup[Path, _Tags] = CachedLookup(
            lambda: True,
            self._tags_backend,
            _LOG_PREFIX + "reading tags",
            clock=clock,
        )
        self._artwork: CachedLookup[Path, ArtInfo] = CachedLookup(
            lambda: True,
            self._artwork_backend,
            _LOG_PREFIX + "reading artwork",
            clock=clock,
            store=ByteBoundedStore(
                _ART_CACHE_BUDGET, _ART_CACHE_MAX_ENTRIES, _art_size
            ),
        )

    def clear(self) -> None:
        """Drops every memoized lookup. In-flight lookups are left
        alone: their futures are what concurrent waiters are parked
        on, and they simply repopulate the cleared caches."""
        for lookup in (
            self._lastfm_artist,
            self._lastfm_album,
            self._spotify_artist,
            self._spotify_album,
            self._tags,
            self._artwork,
        ):
            lookup.clear()

    # --- backends -----------------------------------------------------

    @staticmethod
    def _lastfm_enabled() -> bool:
        return bool(config.LASTFM_API_KEY)

    def _spotify_enabled(self) -> bool:
        return self._spotify is not None

    async def _lastfm_artist_backend(self, name: str) -> Optional[_LastfmInfo]:
        data = await _fetch_lastfm(
            self._lastfm_session(), "artist.getInfo", {"artist": name}
        )
        return _parse_lastfm(data)

    async def _lastfm_album_backend(
        self, key: _AlbumKey
    ) -> Optional[_LastfmInfo]:
        data = await _fetch_lastfm(
            self._lastfm_session(),
            "album.getInfo",
            {"artist": key.artist, "album": key.album},
        )
        return _parse_lastfm(data)

    async def _spotify_artist_backend(
        self, name: str
    ) -> Optional[_SpotifyInfo]:
        return await _off_loop(
            _network_executor, _spotify_artist_sync, self._spotify, name
        )

    async def _spotify_album_backend(
        self, key: _AlbumKey
    ) -> Optional[_SpotifyInfo]:
        return await _off_loop(
            _network_executor, _spotify_album_sync, self._spotify, key
        )

    async def _tags_backend(self, path: Path) -> _Tags:
        tags = await _off_loop(
            _file_executor, functools.partial(read_tags, strict=True), path
        )
        return _Tags(tags.album_artist, tags.artist, tags.album)

    async def _artwork_backend(self, path: Path) -> Optional[ArtInfo]:
        embedded = await _off_loop(
            _file_executor,
            functools.partial(read_artwork, strict=True),
            path,
        )
        if embedded is None:
            return None
        data, extension = embedded
        return ArtInfo(url=None, data=data, extension=extension)

    # --- the entry point ----------------------------------------------

    async def _names(
        self,
        artist_folder: str,
        album_folder: Optional[str],
        sample_file: Path,
    ) -> Tuple[str, Optional[str]]:
        """The query name(s) for external lookups, preferring tag data
        over the (possibly organizational-only) folder name: albumartist
        tag > artist tag > folder name for the artist, album tag >
        folder name for the album. A failed, timed-out or refused tag
        read falls back to the folder names."""
        tags = await self._tags.get(sample_file)
        if tags is None:
            return artist_folder, album_folder
        artist_name = tags.album_artist or tags.artist or artist_folder
        album_name = (
            (tags.album or album_folder) if album_folder is not None else None
        )
        return artist_name, album_name

    async def get_enrichment(
        self,
        index: library.LibraryIndex,
        artist: Optional[str],
        album: Optional[str],
    ) -> Optional[Enrichment]:
        """What is known about a scope of `index`: None for the root
        (`artist` None) and for a scope with no sample file; otherwise
        the artist level (`album` None) or the album level."""
        generation = library.index_generation()
        if generation != self._generation:
            self.clear()
            self._generation = generation
        if artist is None:
            return None
        sample = library.sample_track(index, artist, album)
        if sample is None:
            return None
        path = library.song_path(*sample)
        if album is None:
            return await self._artist_enrichment(artist, path)
        return await self._album_enrichment(artist, album, path)

    async def _artist_enrichment(
        self, artist_folder: str, sample_file: Path
    ) -> Enrichment:
        """Each backend is queried once and feeds both halves of the
        result - the same response carries the image and the
        listener/popularity figures. Running the two concurrently
        overlaps their waits instead of summing them."""
        artist_name, _ = await self._names(artist_folder, None, sample_file)
        spotify, lastfm = await asyncio.gather(
            self._spotify_artist.get(artist_name),
            self._lastfm_artist.get(artist_name),
        )
        return Enrichment(
            stats=_external_stats(spotify, lastfm),
            art=_remote_art(spotify, lastfm),
        )

    async def _album_enrichment(
        self, artist_folder: str, album_folder: str, sample_file: Path
    ) -> Enrichment:
        """Embedded artwork beats the remote images, but does not
        short-circuit the remote lookups - the stats come from them, so
        they run regardless, concurrently with the artwork read."""
        artist_name, album_name = await self._names(
            artist_folder, album_folder, sample_file
        )
        key = _AlbumKey(artist_name, album_name)
        embedded, spotify, lastfm = await asyncio.gather(
            self._artwork.get(sample_file),
            self._spotify_album.get(key),
            self._lastfm_album.get(key),
        )
        return Enrichment(
            stats=_external_stats(spotify, lastfm),
            art=embedded or _remote_art(spotify, lastfm),
        )


default_enricher = Enricher(
    time.monotonic, _low_limit_client(spotify_api), linkutils.get_session
)


async def get_enrichment(
    index: library.LibraryIndex, artist: Optional[str], album: Optional[str]
) -> Optional[Enrichment]:
    """The enrichment for a scope of a library snapshot, from the
    default `Enricher`."""
    return await default_enricher.get_enrichment(index, artist, album)
