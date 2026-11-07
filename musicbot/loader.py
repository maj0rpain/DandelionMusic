import sys
import atexit
import asyncio
from enum import Enum, StrEnum
from traceback import print_exc
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context as mp_context
from typing import Dict, List, Optional, Union

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from config import config
from musicbot import library
from musicbot.message_link import CheckedTrack, is_message_link
from musicbot.audiotags import nice_title, read_tags
from musicbot.song import LoadResult, Song
from musicbot.utils import wrap_stdio
from musicbot.linkutils import (
    ExtractorT,
    SiteTypes,
    get_ie,
    SpotifyTrack,
    SpotifyTracks,
    fetch_spotify,
    identify_url,
    normalize_track,
    resolve_short_link,
    init as init_session,
    stop as stop_session,
)

_context = mp_context("spawn")


class LoaderProcess(_context.Process):
    def run(self):
        try:
            super().run()
        # suppress noisy errors that happen on Ctrl+C
        except (KeyboardInterrupt, InterruptedError):
            pass


# The two extraction lanes: each is its own single-worker process pool.
# The preload lane serves only preload()'s extraction, so playback never
# waits behind a user-requested load; the load lane serves every other
# loader call. A fixed property of the code, not a Config setting.
class Lane(StrEnum):
    LOAD = "load"
    PRELOAD = "preload"


# Importing this module changes no process-global state: everything
# below is None, or empty, until init() (in the bot's process) or
# _init_worker() (in the spawned extraction worker) sets it up.
_pools: Dict[Lane, ProcessPoolExecutor] = {}
# the worker's own event loop and downloader - read by extraction here
# and by the Discord-attachment and Suno yt-dlp plugins
# (`from musicbot.loader import _loop`)
_loop: Optional[asyncio.AbstractEventLoop] = None
_downloader: Optional[YoutubeDL] = None
# empty at import, filled at run time: _preloading by preload() in the
# bot's process, _site_locks by extract_info() in the worker
_preloading = {}
_site_locks = {}


def _new_downloader() -> YoutubeDL:
    return YoutubeDL(
        {
            "format": "bestaudio/best",
            "extract_flat": "in_playlist",
            "noplaylist": True,
            # default_search shouldn't be needed as long as
            # we don't pass plain text to the downloader.
            # still leaving it just in case
            "default_search": "auto",
            "cookiefile": config.COOKIE_PATH,
            "quiet": True,
            "extractor_args": {
                "youtube": {"player_client": ["default", "tv"]},
                "youtubepot-bgutilhttp": {
                    "base_url": ["http://bgutil-provider:4416"]
                },
            },
            "ignoreerrors": "only_download",
            # "verbose": True
            # "remote_components": "ejs:npm"
        }
    )


def _init_worker():
    """Runs once inside the spawned worker, before any extraction."""
    global _loop, _downloader
    wrap_stdio()
    _loop = asyncio.new_event_loop()
    _loop.run_until_complete(init_session())
    _downloader = _new_downloader()
    atexit.register(_teardown_worker)


def _teardown_worker():
    _loop.run_until_complete(stop_session())


def _noop():
    pass


def init():
    """Sets up the loader for the bot's process:
    installs LoaderProcess and starts both extraction lanes' workers,
    warming them up concurrently. A second call does nothing. If a
    warm-up raises, init() raises it with both pools left in _pools."""
    if _pools:
        return
    _context.Process = LoaderProcess
    for lane in Lane:
        _pools[lane] = _new_pool()
    # wake both up to spawn the processes immediately, together
    futures = [_pools[lane].submit(_noop) for lane in Lane]
    for future in futures:
        future.result()


def shutdown():
    """Stops both extraction workers. Safe without init(), and twice."""
    pools = [_pools[lane] for lane in Lane if lane in _pools]
    _pools.clear()
    for pool in pools:
        pool.shutdown(wait=True, cancel_futures=True)


# How long before a stream url's `expire=` preload re-extracts it, and
# how long it trusts a stream url that has no `expire=`. They reflect how
# hosts sign urls, so they are not Config settings.
EXPIRY_MARGIN = timedelta(minutes=10)
UNKNOWN_EXPIRY_MAX_AGE = timedelta(minutes=10)


def _now() -> datetime:
    """The loader's clock seam: tests monkeypatch it."""
    return datetime.now(timezone.utc)


class LoaderNotRunning(RuntimeError):
    """A loader call was made with no extraction worker running."""


class SongError(Exception):
    pass


class Preload(Enum):
    """What preload() did. It defines no __bool__: compare with `is`.

    FAILED: an extraction was needed and failed.
    CURRENT: nothing needed extracting - the song has no webpage_url,
    is a local-library or custom-link song, has a stream url not within
    EXPIRY_MARGIN of its expire= (or the link is its own stream), has a
    stream url with no expire= extracted less than
    UNKNOWN_EXPIRY_MAX_AGE ago, or another caller's in-flight
    extraction of it succeeded.
    EXTRACTED: this call ran an extraction and updated the song.
    """

    FAILED = "failed"
    CURRENT = "current"
    EXTRACTED = "extracted"


def extract_info(url: str, ie: Optional[ExtractorT] = None) -> Optional[dict]:
    if ie is None:
        ie = get_ie(url)
    try:
        return _downloader.extract_info(url, False, ie.ie_key())
    except DownloadError:
        return None


async def search_youtube(title: str, count: int = 1) -> list[Song]:
    """See _search_youtube: a non-empty list of songs, or SongError."""
    return await _run_sync(Lane.LOAD, _search_songs, title, count)


def _search_songs(title: str, count: int = 1) -> list[Song]:
    songs = []
    for data in _search_youtube(title, count):
        song = Song(SiteTypes.YT_DLP, webpage_url=data["url"])
        song.update(data)
        songs.append(song)
    return songs


def _search_youtube(title: str, count: int = 1) -> list[dict]:
    """Searches youtube for the video title.
    Returns the search's entries, never empty. Raises SongError with
    SONGINFO_ERROR when extraction failed, or SEARCH_NO_RESULTS when
    nothing was found."""

    r = extract_info(f"ytsearch{count}:{title}")

    if r is None:
        raise SongError(config.SONGINFO_ERROR)
    if not r["entries"]:
        raise SongError(config.SEARCH_NO_RESULTS)

    return r["entries"]


async def load_song(track: Union[str, CheckedTrack]) -> LoadResult:
    """Loads a plain track or one the message-link gate checked."""
    if isinstance(track, CheckedTrack):
        track = track.track
    return await _load(Lane.LOAD, track)


async def _load(lane: Lane, track: str) -> LoadResult:
    """Runs _load_song in the given lane and stamps the result."""
    result = await _run_sync(lane, _load_song, track)
    now = _now()
    for song in result.songs:
        song.extracted_at = now
    return result


async def load_local_songs(tracks: List[str]) -> List[Optional[Song]]:
    """Loads a batch of local-library tracks in one executor call.
    Returns a list parallel to `tracks`, None wherever a file could not
    be loaded.

    What this saves is the round trip, not the subprocess. Every call
    into a lane's single-worker pool is an IPC round trip that queues
    behind whatever extraction is already running in that lane, so
    queueing an album used to mean a dozen of them and queueing a
    discography hundreds - one
    call covers the batch instead. It still goes through the pool,
    because a local track's work is a mutagen tag read: mostly pure
    Python, and therefore holding the GIL. On a thread it would be
    contending with the event loop and discord.py's audio-player
    thread for as long as the batch takes, which is the hazard
    _search_lock exists for in musicbot.commands.library. Measured over
    300 tracks the pool is no slower than a thread anyway (247ms
    against 254ms), so there is nothing to trade away.

    Refuses anything that is not a local-library URI. The other
    branches of _load_song can return several songs for one track
    (a playlist), which would silently break the one-entry-per-input
    contract the caller relies on to report which files it skipped.
    """
    for track in tracks:
        if identify_url(track) is not SiteTypes.LOCAL_LIBRARY:
            raise ValueError(f"not a local library track: {track!r}")
    return await _run_sync(Lane.LOAD, _load_local_songs, tracks)


def _load_local_songs(tracks: List[str]) -> List[Optional[Song]]:
    songs: List[Optional[Song]] = []
    for track in tracks:
        # One bad file must not cost the caller the rest of the batch -
        # the browser reports the missing ones by name. SongError is
        # the expected way that happens (a file the index has and the
        # disk no longer does); anything else - an unparseable URI, a
        # stale mount refusing resolve() - is unexpected but just as
        # local to the one track, and the per-track path this replaced
        # kept everything queued up to the failure rather than losing
        # the batch.
        try:
            songs.append(_load_song(track).songs[0])
        except SongError:
            songs.append(None)
        except Exception as e:
            print(f"loader: loading {track!r} failed: {e}", file=sys.stderr)
            songs.append(None)
    return songs


def _search_spotify_track(track: SpotifyTrack) -> dict:
    """The first YouTube result for a Spotify track. Any failure is a
    SONGINFO_ERROR, as a failed Spotify fetch is."""
    try:
        return _search_youtube(track.query)[0]
    except Exception as e:
        raise SongError(config.SONGINFO_ERROR) from e


def _load_song(track: str) -> LoadResult:
    track = normalize_track(track)
    truncation = None
    host = identify_url(track)

    if host == SiteTypes.SHORT_LINK:
        # resolved once; the result is never resolved again
        try:
            track = _loop.run_until_complete(resolve_short_link(track))
        except Exception as e:
            raise SongError(config.SONGINFO_ERROR) from e
        track = normalize_track(track)
        host = identify_url(track)
        # the message-link gate ran on the short link as typed; a
        # resolved Discord message link would skip it
        if host == SiteTypes.NOT_URL or is_message_link(track):
            raise SongError(config.SONGINFO_UNSUPPORTED)

    if host == SiteTypes.NOT_URL:
        data = _search_youtube(track)[0]
        host = SiteTypes.YT_DLP

    elif host in (SiteTypes.UNKNOWN, SiteTypes.SHORT_LINK):
        raise SongError(config.SONGINFO_UNSUPPORTED)

    elif host == SiteTypes.SPOTIFY:
        try:
            data = _loop.run_until_complete(fetch_spotify(track))
        except Exception as e:
            raise SongError(config.SONGINFO_ERROR) from e
        if isinstance(data, SpotifyTrack):
            data = _search_spotify_track(data)
        elif isinstance(data, SpotifyTracks):
            truncation = data.truncation
            data = [{"url": url} for url in data.links]

    elif host == SiteTypes.CUSTOM:
        data = {
            "url": track,
            "webpage_url": track,
            "title": urlparse(track).path.rpartition("/")[2],
        }

    elif host == SiteTypes.LOCAL_LIBRARY:
        path = (
            library.track_path_from_uri(track)
            if config.MUSIC_LIBRARY_PATH
            else None
        )
        if path is None:
            raise SongError(config.LIBRARY_FILE_MISSING)
        tags = read_tags(path)
        data = {
            "url": str(path),
            "webpage_url": track,
            "title": nice_title(tags, fallback=path.stem),
            "uploader": tags.artist,
            "duration": tags.duration,
        }

    else:  # host is info extractor
        data = extract_info(track, host)
        host = SiteTypes.YT_DLP

    if not data:
        raise SongError(config.SONGINFO_ERROR)

    if isinstance(data, dict):
        if "entries" in data:
            # assuming a playlist
            data = data["entries"]

    if isinstance(data, list):
        if not data:
            raise SongError(config.SONGINFO_ERROR)
        results = []
        for entry in data:
            entry.pop("webpage_url", None)
            song = Song(
                host,
                webpage_url=entry.pop("url"),
            )
            song.update(entry)
            results.append(song)
        single = truncation is None and len(results) == 1
        return LoadResult(results, single, truncation)

    song = Song(host, webpage_url=track)
    song.update(data)

    return LoadResult.of_song(song)


def _parse_expire(url: str) -> Optional[int]:
    expire = (
        ("&" + urlparse(url).query).partition("&expire=")[2].partition("&")[0]
    )
    try:
        return int(expire)
    except ValueError:
        return None


def _stream_url_is_current(song: Song) -> bool:
    expire = _parse_expire(song.url)
    if expire is None:
        return (
            song.extracted_at is not None
            and _now() - song.extracted_at < UNKNOWN_EXPIRY_MAX_AGE
        )
    if expire == _parse_expire(song.webpage_url):
        # the link is its own stream: re-extracting cannot refresh it
        return True
    return _now() < datetime.fromtimestamp(expire, timezone.utc) - (
        EXPIRY_MARGIN
    )


async def preload(song: Song) -> Preload:
    if song.webpage_url is None:
        return Preload.CURRENT

    if song.host in (SiteTypes.LOCAL_LIBRARY, SiteTypes.CUSTOM):
        return Preload.CURRENT

    if song.url is not None and _stream_url_is_current(song):
        return Preload.CURRENT

    future = _preloading.get(song)
    if future:
        # only the caller that ran the extraction reports EXTRACTED,
        # so one extraction is acted on at most once
        if await asyncio.shield(future):
            return Preload.CURRENT
        return Preload.FAILED
    _preloading[song] = asyncio.Future()

    # any exception here (not just SongError) must still resolve the
    # future below, or a future preload() call on the same song hangs
    # forever awaiting it
    success = False
    try:
        try:
            preloaded = await _load(Lane.PRELOAD, song.webpage_url)
        except SongError:
            pass
        except Exception:
            print_exc(file=sys.stderr)
        else:
            success = preloaded.single

        if success:
            song.update(preloaded.songs[0])
    finally:
        _preloading.pop(song).set_result(success)

    return Preload.EXTRACTED if success else Preload.FAILED


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(1, _context, initializer=_init_worker)


def _replace_broken(lane: Lane, broken: ProcessPoolExecutor) -> None:
    """Swaps in a fresh pool for `broken` in `lane`, unless another call
    already did (one crash fails every call queued in the lane's
    single-worker pool, and they all share one rebuild) or shutdown()
    ran. Only that lane's pool is touched. Loop-only, which is what
    makes the identity check race-free. The new pool starts lazily:
    warming it up here would block the event loop on a spawn."""
    if _pools.get(lane) is not broken:
        return
    print(
        f"loader: the {lane} lane's extraction worker died,"
        " starting a new one",
        file=sys.stderr,
    )
    _pools[lane] = _new_pool()
    broken.shutdown(wait=False)


def _require_running(lane: Lane) -> ProcessPoolExecutor:
    pool = _pools.get(lane)
    if pool is None:
        raise LoaderNotRunning(
            "the loader is not running: loader.init() has not been called"
            " or loader.shutdown() already ran"
        )
    return pool


async def _run_sync(lane: Lane, f, *args):
    # run_in_executor(None, ...) would pick asyncio's default thread pool
    # and run extraction in this process, with no worker state set up
    loop = asyncio.get_running_loop()
    for _ in (1, 2):
        executor = _require_running(lane)
        try:
            return await loop.run_in_executor(executor, f, *args)
        except BrokenProcessPool:
            _replace_broken(lane, executor)
    # the retry broke too; if shutdown() ran meanwhile, this raises
    # LoaderNotRunning instead
    _require_running(lane)
    raise SongError(config.LOADER_CRASHED) from None
