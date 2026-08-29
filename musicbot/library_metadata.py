import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple

from config import config
from musicbot import linkutils
from musicbot.linkutils import spotify_api
from musicbot.audiotags import read_artwork, read_tags

# Dedicated pool (not the loop's shared default executor) for the
# blocking calls this module makes - mutagen reads and spotipy's
# synchronous HTTP client. asyncio.wait_for()'s timeout only abandons
# the await, not the underlying thread, so a string of slow/hung
# external calls could otherwise pile up threads in whatever pool the
# rest of the bot shares.
_executor = ThreadPoolExecutor(max_workers=4)


# A transient failure is deliberately not cached as a result, but
# retrying it on every navigation makes a persistently broken backend
# (bad credentials, an outage, an unreachable host) pay its full
# timeout again for each new key. Suppress its retries for a while.
_TRANSIENT_COOLDOWN = 60


def _in_cooldown(deadlines: Dict[object, float], key: object) -> bool:
    deadline = deadlines.get(key)
    if deadline is None:
        return False
    if time.monotonic() < deadline:
        return True
    del deadlines[key]
    return False


def _start_cooldown(deadlines: Dict[object, float], key: object) -> None:
    deadlines[key] = time.monotonic() + _TRANSIENT_COOLDOWN


class ArtInfo(NamedTuple):
    url: Optional[str]  # a real URL - use with embed.set_thumbnail(url=...)
    data: Optional[bytes]  # raw embedded bytes - needs discord.File
    extension: Optional[str]  # "jpeg"/"png"/etc, set only when data is set


async def _resolve_names(
    artist_folder: str, album_folder: Optional[str], sample_file: Path
) -> Tuple[str, Optional[str]]:
    """Resolves the query name(s) to use for external lookups,
    preferring tag data over the (possibly organizational-only,
    non-canonical) folder name: albumartist tag > artist tag > folder
    name for the artist, album tag > folder name for the album. A
    hung/slow read (e.g. MUSIC_LIBRARY_PATH on a NAS mount) times out
    and falls back to the folder names rather than blocking forever."""
    loop = asyncio.get_running_loop()
    artist_name = artist_folder
    album_name = album_folder
    try:
        tags = await asyncio.wait_for(
            loop.run_in_executor(_executor, read_tags, sample_file),
            timeout=3,
        )
    except asyncio.TimeoutError:
        print(
            f"library_metadata: reading tags from {sample_file} timed out",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"library_metadata: reading tags from {sample_file} failed: {e}",
            file=sys.stderr,
        )
    else:
        artist_name = tags.album_artist or tags.artist or artist_folder
        if album_folder is not None:
            album_name = tags.album or album_folder
    return artist_name, album_name


class _LastfmInfo(NamedTuple):
    image_url: Optional[str]
    listeners: Optional[int]
    playcount: Optional[int]
    tags: Tuple[str, ...]


_lastfm_cache: Dict[object, Optional[_LastfmInfo]] = {}
_lastfm_futures: Dict[object, asyncio.Future] = {}
_lastfm_cooldown: Dict[object, float] = {}


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


async def _fetch_lastfm(method: str, params: Dict[str, str]) -> dict:
    session = linkutils.get_session()
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
        raise RuntimeError(f"{data.get('message')} (code {data.get('error')})")
    return data


async def _lastfm_info(
    method: str, params: Dict[str, str], key: object
) -> Optional[_LastfmInfo]:
    if not config.LASTFM_API_KEY:
        return None
    if key in _lastfm_cache:
        return _lastfm_cache[key]
    if _in_cooldown(_lastfm_cooldown, key):
        return None
    future = _lastfm_futures.get(key)
    if future:
        # shielded: this waiter being cancelled (view teardown, bot
        # shutdown) must not cancel the shared future out from under
        # the call that owns it, nor the other waiters on it
        return await asyncio.shield(future)
    _lastfm_futures[key] = asyncio.get_running_loop().create_future()

    result: Optional[_LastfmInfo] = None
    # a timeout/exception is transient and must not be cached as a
    # permanent "no match" - only a real fetched-successfully outcome
    # (match or genuine no-match) is worth remembering for the rest
    # of the process's lifetime
    transient_failure = False
    try:
        try:
            data = await asyncio.wait_for(
                _fetch_lastfm(method, params), timeout=3
            )
        except asyncio.TimeoutError:
            print(
                f"library_metadata: Last.fm {method} timed out"
                f" for {params}",
                file=sys.stderr,
            )
            transient_failure = True
            data = None
        except Exception as e:
            print(
                f"library_metadata: Last.fm {method} failed"
                f" for {params}: {e}",
                file=sys.stderr,
            )
            transient_failure = True
            data = None

        node = (data.get("artist") or data.get("album")) if data else None
        if node:
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
                    if img.get("#text")
                    and _PLACEHOLDER_IMAGE not in img["#text"]
                ),
                None,
            )
            if image_url or listeners or playcount or tags:
                result = _LastfmInfo(
                    image_url=image_url,
                    listeners=listeners,
                    playcount=playcount,
                    tags=tags,
                )

        if result is None and not transient_failure:
            print(
                f"library_metadata: Last.fm {method} found no match"
                f" for {params}",
                file=sys.stderr,
            )

        if transient_failure:
            _start_cooldown(_lastfm_cooldown, key)
        else:
            _lastfm_cache[key] = result
    finally:
        pending = _lastfm_futures.pop(key)
        if not pending.done():
            pending.set_result(result)

    return result


class _SpotifyInfo(NamedTuple):
    image_url: Optional[str]
    popularity: Optional[int]  # 0-100
    followers: Optional[int]  # artist level only
    release_date: Optional[str]  # album level only


_spotify_cache: Dict[object, Optional[_SpotifyInfo]] = {}
_spotify_futures: Dict[object, asyncio.Future] = {}
_spotify_cooldown: Dict[object, float] = {}


def _spotify_artist_sync(name: str) -> Optional[_SpotifyInfo]:
    results = spotify_api.search(q=name, type="artist", limit=1)
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
    artist_name: str, album_name: str
) -> Optional[_SpotifyInfo]:
    query = f"artist:{artist_name} album:{album_name}"
    results = spotify_api.search(q=query, type="album", limit=1)
    items = results.get("albums", {}).get("items", [])
    if not items:
        return None
    item = items[0]
    images = item.get("images", [])
    # a search only yields simplified album objects, which carry no
    # popularity - that lives on the full object and needs its own
    # fetch. Losing that extra call must not cost us the rest of the
    # match, so it degrades to no popularity rather than failing.
    popularity = None
    album_id = item.get("id")
    if album_id:
        try:
            popularity = spotify_api.album(album_id).get("popularity")
        except Exception as e:
            print(
                f"library_metadata: Spotify album detail failed"
                f" for {album_name}: {e}",
                file=sys.stderr,
            )
    return _SpotifyInfo(
        image_url=images[0]["url"] if images else None,
        popularity=popularity,
        followers=None,
        release_date=item.get("release_date"),
    )


async def _spotify_lookup(
    fn, key: object, label: str
) -> Optional[_SpotifyInfo]:
    if spotify_api is None:
        return None
    if key in _spotify_cache:
        return _spotify_cache[key]
    if _in_cooldown(_spotify_cooldown, key):
        return None
    future = _spotify_futures.get(key)
    if future:
        # see _lastfm_info - shielded against waiter cancellation
        return await asyncio.shield(future)
    _spotify_futures[key] = asyncio.get_running_loop().create_future()

    result: Optional[_SpotifyInfo] = None
    # see _lastfm_info's identical reasoning - a timeout/exception is
    # transient and must not be cached as a permanent "no match"
    transient_failure = False
    try:
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_executor, fn), timeout=3
            )
        except asyncio.TimeoutError:
            print(
                f"library_metadata: Spotify {label} timed out for {key}",
                file=sys.stderr,
            )
            transient_failure = True
        except Exception as e:
            print(
                f"library_metadata: Spotify {label} failed for {key}: {e}",
                file=sys.stderr,
            )
            transient_failure = True
        else:
            if result is None:
                print(
                    f"library_metadata: Spotify found no {label}"
                    f" for {key}",
                    file=sys.stderr,
                )
        if transient_failure:
            _start_cooldown(_spotify_cooldown, key)
        else:
            _spotify_cache[key] = result
    finally:
        pending = _spotify_futures.pop(key)
        if not pending.done():
            pending.set_result(result)

    return result


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


async def _embedded_art(sample_file: Path) -> Optional[ArtInfo]:
    loop = asyncio.get_running_loop()
    try:
        embedded = await asyncio.wait_for(
            loop.run_in_executor(_executor, read_artwork, sample_file),
            timeout=3,
        )
    except asyncio.TimeoutError:
        print(
            f"library_metadata: reading artwork from {sample_file}"
            " timed out",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(
            f"library_metadata: reading artwork from {sample_file}"
            f" failed: {e}",
            file=sys.stderr,
        )
        return None
    if embedded is None:
        return None
    data, extension = embedded
    return ArtInfo(url=None, data=data, extension=extension)


async def get_artist_enrichment(
    artist_folder: str, sample_file: Path
) -> Enrichment:
    """Stats and photo for one artist browse level. Each backend is
    queried once and feeds both halves of the result - the same
    response carries the image and the listener/popularity figures, so
    there is nothing left to gain from splitting them. Running the two
    concurrently overlaps their waits instead of summing them."""
    artist_name, _ = await _resolve_names(artist_folder, None, sample_file)
    spotify, lastfm = await asyncio.gather(
        _spotify_lookup(
            lambda: _spotify_artist_sync(artist_name), artist_name, "artist"
        ),
        _lastfm_info("artist.getInfo", {"artist": artist_name}, artist_name),
    )
    return Enrichment(
        stats=_external_stats(spotify, lastfm),
        art=_remote_art(spotify, lastfm),
    )


async def get_album_enrichment(
    artist_folder: str, album_folder: str, sample_file: Path
) -> Enrichment:
    """Stats and cover art for one album browse level. Embedded
    artwork still beats the remote images, but unlike before it no
    longer short-circuits the remote lookups - the stats come from
    them, so they run regardless, concurrently with the tag read."""
    artist_name, album_name = await _resolve_names(
        artist_folder, album_folder, sample_file
    )
    key = (artist_name, album_name)
    embedded, spotify, lastfm = await asyncio.gather(
        _embedded_art(sample_file),
        _spotify_lookup(
            lambda: _spotify_album_sync(artist_name, album_name), key, "album"
        ),
        _lastfm_info(
            "album.getInfo",
            {"artist": artist_name, "album": album_name},
            key,
        ),
    )
    return Enrichment(
        stats=_external_stats(spotify, lastfm),
        art=embedded or _remote_art(spotify, lastfm),
    )
