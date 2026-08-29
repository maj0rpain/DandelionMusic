import asyncio
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple

from bs4 import BeautifulSoup

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
    summary: Optional[str]
    image_url: Optional[str]


_lastfm_cache: Dict[object, Optional[_LastfmInfo]] = {}
_lastfm_futures: Dict[object, asyncio.Future] = {}
_lastfm_cooldown: Dict[object, float] = {}


# Last.fm serves this boilerplate link as the *entire* bio/wiki summary
# for an entity that has no write-up, and tacks it onto genuine ones
_READ_MORE = re.compile(r"\s*Read more on Last\.fm\s*\.?\s*$")

# since Last.fm's 2019 image-licensing change, artist.getInfo returns
# this one grey-star placeholder as the image for every artist - a real
# URL, so it has to be rejected by hash rather than by being absent
_PLACEHOLDER_IMAGE = "2a96cbd8b46e442fc41c2b86b821562f"


def _clean_summary(raw: str, max_length: int = 400) -> Optional[str]:
    text = BeautifulSoup(raw, "html.parser").get_text().strip()
    text = _READ_MORE.sub("", text).strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[:max_length].rsplit(" ", 1)[0] + "…"


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
            bio = (node.get("bio") or {}).get("summary") or (
                node.get("wiki") or {}
            ).get("summary")
            summary = _clean_summary(bio) if bio else None

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
            if summary or image_url:
                result = _LastfmInfo(summary=summary, image_url=image_url)

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


_spotify_cache: Dict[object, Optional[str]] = {}
_spotify_futures: Dict[object, asyncio.Future] = {}
_spotify_cooldown: Dict[object, float] = {}


def _spotify_artist_image_sync(name: str) -> Optional[str]:
    results = spotify_api.search(q=name, type="artist", limit=1)
    items = results.get("artists", {}).get("items", [])
    if not items:
        return None
    images = items[0].get("images", [])
    return images[0]["url"] if images else None


def _spotify_album_image_sync(
    artist_name: str, album_name: str
) -> Optional[str]:
    query = f"artist:{artist_name} album:{album_name}"
    results = spotify_api.search(q=query, type="album", limit=1)
    items = results.get("albums", {}).get("items", [])
    if not items:
        return None
    images = items[0].get("images", [])
    return images[0]["url"] if images else None


async def _spotify_image(fn, key: object, label: str) -> Optional[str]:
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

    result: Optional[str] = None
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


class Enrichment(NamedTuple):
    summary: Optional[str]
    art: Optional[ArtInfo]


async def _artist_photo(artist_name: str) -> Optional[ArtInfo]:
    url = await _spotify_image(
        lambda: _spotify_artist_image_sync(artist_name),
        artist_name,
        "artist photo",
    )
    if url:
        return ArtInfo(url=url, data=None, extension=None)

    info = await _lastfm_info(
        "artist.getInfo", {"artist": artist_name}, artist_name
    )
    if info and info.image_url:
        return ArtInfo(url=info.image_url, data=None, extension=None)

    return None


async def _artist_summary(artist_name: str) -> Optional[str]:
    info = await _lastfm_info(
        "artist.getInfo", {"artist": artist_name}, artist_name
    )
    return info.summary if info else None


async def _album_art(
    artist_name: str, album_name: str, sample_file: Path
) -> Optional[ArtInfo]:
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
        embedded = None
    except Exception as e:
        print(
            f"library_metadata: reading artwork from {sample_file}"
            f" failed: {e}",
            file=sys.stderr,
        )
        embedded = None
    if embedded is not None:
        data, extension = embedded
        return ArtInfo(url=None, data=data, extension=extension)

    key = (artist_name, album_name)
    url = await _spotify_image(
        lambda: _spotify_album_image_sync(artist_name, album_name),
        key,
        "album art",
    )
    if url:
        return ArtInfo(url=url, data=None, extension=None)

    info = await _lastfm_info(
        "album.getInfo", {"artist": artist_name, "album": album_name}, key
    )
    if info and info.image_url:
        return ArtInfo(url=info.image_url, data=None, extension=None)

    return None


async def _album_summary(artist_name: str, album_name: str) -> Optional[str]:
    info = await _lastfm_info(
        "album.getInfo",
        {"artist": artist_name, "album": album_name},
        (artist_name, album_name),
    )
    return info.summary if info else None


async def get_artist_enrichment(
    artist_folder: str, sample_file: Path
) -> Enrichment:
    """Blurb and photo for one artist browse level. The two lookups
    are one call because they share a resolved name: exposing them
    separately made every navigation resolve names twice, doing the
    same blocking tag read of the same file twice. Running them
    concurrently also overlaps their waits instead of summing them -
    the Last.fm lookup they have in common dedupes through
    _lastfm_futures rather than firing twice."""
    artist_name, _ = await _resolve_names(artist_folder, None, sample_file)
    summary, art = await asyncio.gather(
        _artist_summary(artist_name),
        _artist_photo(artist_name),
    )
    return Enrichment(summary=summary, art=art)


async def get_album_enrichment(
    artist_folder: str, album_folder: str, sample_file: Path
) -> Enrichment:
    """Blurb and cover art for one album browse level. See
    get_artist_enrichment for why this is a single call."""
    artist_name, album_name = await _resolve_names(
        artist_folder, album_folder, sample_file
    )
    summary, art = await asyncio.gather(
        _album_summary(artist_name, album_name),
        _album_art(artist_name, album_name, sample_file),
    )
    return Enrichment(summary=summary, art=art)
