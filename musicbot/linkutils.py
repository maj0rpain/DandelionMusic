import re
import sys
import asyncio
from enum import Enum, auto
from dataclasses import dataclass
from traceback import print_exc
from urllib.parse import urlparse
from multiprocessing import current_process
from typing import Dict, Optional, Union, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from spotipy import Spotify
from bs4 import BeautifulSoup
from aiohttp import ClientSession, ClientTimeout
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.lazy_extractors import LazyLoadExtractor
from yt_dlp.utils import DownloadError

from config import config
from musicbot import loader
from musicbot.library import FILE_SCHEME
from musicbot.truncation import Truncation, TruncationReason

# Limits on the network calls the extraction workers make. A call that
# blocks holds its lane's worker (load or preload) for every guild using
# that lane, so none may block long. Deliberately not Config settings:
# nobody has a reason to tune them.
# longest wait honoured for one Spotify 429 Retry-After header (seconds)
RETRY_AFTER_MAX = 5
# total time limit of one aiohttp request, or of each Discord API request
# (a first Discord link logs in, then fetches) (seconds)
HTTP_TIMEOUT = 15
# time limit of one Spotify API or token request (seconds)
SPOTIFY_TIMEOUT = 5


def spotify_session() -> requests.Session:
    """A requests session whose retries match spotipy's own defaults,
    except that a Retry-After wait is capped at RETRY_AFTER_MAX.

    Hand-copied from spotipy 2.26.0. connect, read, allowed_methods and
    backoff_factor are copied from Spotify._build_session; total and
    status are read from Spotify.max_retries and status_forcelist from
    Spotify.default_retry_codes. retry_after_max is our one addition.
    tests/test_worker_network_limits.py fails if spotipy drifts."""
    retry = Retry(
        total=Spotify.max_retries,
        connect=None,
        read=False,
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        status=Spotify.max_retries,
        backoff_factor=0.3,
        status_forcelist=Spotify.default_retry_codes,
        retry_after_max=RETRY_AFTER_MAX,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def build_spotify_api(client_id, client_secret) -> Optional[Spotify]:
    if not (client_id or client_secret):
        return None
    try:
        session = spotify_session()
        return Spotify(
            requests_session=session,
            requests_timeout=SPOTIFY_TIMEOUT,
            auth_manager=SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                requests_session=session,
                requests_timeout=SPOTIFY_TIMEOUT,
                cache_handler=MemoryCacheHandler(),
            ),
        )
    except Exception:
        if (
            # avoid printing this twice
            current_process().name
            == "MainProcess"
        ):
            print_exc(file=sys.stderr)
            print(
                "Failed to connect to Spotify API"
                " because of the above exception.",
                file=sys.stderr,
            )
        return None


spotify_api = build_spotify_api(config.SPOTIFY_ID, config.SPOTIFY_SECRET)

ExtractorT = Union[InfoExtractor, LazyLoadExtractor]
EXTRACTORS = gen_extractor_classes()
YT_IE = next(ie for ie in EXTRACTORS if ie.IE_NAME == "youtube")
# Modified version of
# https://gist.github.com/gruber/249502#gistcomment-1328838
url_regex = re.compile(
    r"(?i)\b((?:[a-z][\w.+-]+:(?:/{1,3}|[?+]?[a-z0-9%]))"
    r"(?P<bare>(?:[^\s()<>]|\((?:[^\s()<>]|(?:\([^\s()<>]+\)))*\))+"
    r"(?:\((?:[^\s()<>]|(?:\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'"
    r'"'
    r".,<>?«»“”‘’])))"
)
# the Spotify types the link and URI patterns accept, case-sensitive
_SPOTIFY_TYPES = "track|playlist|album"
spotify_regex = re.compile(
    r"^(?i:https?://open\.spotify\.com)/([^/]+/)?"
    rf"(?P<type>{_SPOTIFY_TYPES})/(?P<code>\w+)"
)

headers = {}

# aiohttp binds a ClientSession to whatever loop is running when it is
# constructed and refuses to be driven from any other one, raising
# "Timeout context manager should be used inside a task". This module
# runs under three loops - the bot's own, in the bot's process, and
# loader.py's private _loop, which drives the Spotify page lookups in
# each extraction lane's worker - so a session must belong to the loop
# that uses it.
# Keep one session per loop instead of a single global one.
_sessions: Dict[asyncio.AbstractEventLoop, ClientSession] = {}


async def init():
    get_session()


async def stop():
    session = _sessions.pop(asyncio.get_running_loop(), None)
    if session is None:
        return
    await session.close()
    # according to aiohttp docs, we need to wait a little after closing session
    await asyncio.sleep(0.5)


def get_session() -> ClientSession:
    """The ClientSession belonging to the calling loop, opened on
    first use. Never cache what this returns across loops (and never
    `from linkutils import _sessions` at another module's top level) -
    always call it from inside the coroutine that does the request."""
    loop = asyncio.get_running_loop()
    session = _sessions.get(loop)
    if session is None:
        session = _sessions[loop] = ClientSession(
            headers=headers, timeout=ClientTimeout(total=HTTP_TIMEOUT)
        )
    return session


class SiteTypes(Enum):
    SPOTIFY = auto()
    YT_DLP = auto()
    CUSTOM = auto()
    LOCAL_LIBRARY = auto()
    UNKNOWN = auto()
    NOT_URL = auto()


class SpotifyPlaylistTypes(Enum):
    PLAYLIST = "playlist"
    ALBUM = "album"


@dataclass
class SpotifyTracks:
    """Links of a Spotify playlist or album. `truncation` is set when
    the playlist was cut short; it is `None` when every reported track
    was fetched."""

    links: List[str]
    truncation: Optional[Truncation] = None


def download_error(e: Exception) -> DownloadError:
    """A DownloadError for e, named by its type when its message is empty
    (a TimeoutError stringifies empty)."""
    return DownloadError(str(e) or type(e).__name__)


async def get_soup(url: str) -> BeautifulSoup:
    async with get_session().get(url) as response:
        response.raise_for_status()
        page = await response.text()

    return BeautifulSoup(page, "html.parser")


async def fetch_spotify(
    url: str,
) -> Optional[Union[dict, SpotifyTracks]]:
    """Searches YouTube for Spotify song or loads Spotify playlist"""
    match = spotify_regex.match(url)
    # strip any extra parts
    url = match.group()
    url_type = match.group("type")
    if url_type != "track":
        return await fetch_spotify_playlist(url, url_type, match.group("code"))

    if spotify_api:
        track_data = spotify_api.track(match.group("code"))
        title = track_data["name"]
        artist = track_data["artists"][0]["name"]
    else:
        soup = await get_soup(url)

        title_str = soup.find("title").string
        # Match "Title - song and lyrics by Artist | Spotify"
        match = re.match(
            r"(.*) - song(?: and lyrics)? by (.*) \| Spotify", title_str
        )
        if match:
            title, artist = match.group(1), match.group(2)
        else:
            # Fallback if regex doesn't match
            title, artist = title_str, ""

    if artist:
        query = f'{title} - {artist} "Topic"'
    else:
        query = f'{title} "Topic"'

    # use sync function because we're already in executor
    return loader._search_youtube(query)[0]


async def fetch_spotify_playlist(
    url: str, list_type: str, code: str
) -> SpotifyTracks:
    """Returns the Spotify links, and how many tracks are missing"""

    if spotify_api:
        return fetch_playlist_with_api(SpotifyPlaylistTypes(list_type), code)

    soup = await get_soup(url)
    results = soup.find_all(attrs={"name": "music:song", "content": True})
    count_tag = soup.find(attrs={"name": "music:song_count", "content": True})
    try:
        total = int(count_tag["content"]) if count_tag else None
    except ValueError:
        total = None

    links = [item["content"] for item in results]
    truncation = Truncation.of(total, len(links), TruncationReason.NO_API)
    return SpotifyTracks(links, truncation)


def fetch_playlist_with_api(
    list_type: SpotifyPlaylistTypes, code: str
) -> SpotifyTracks:
    tracks = []
    truncation = None
    pages = 0
    api_failed = False
    first_total = None
    try:
        if list_type == SpotifyPlaylistTypes.ALBUM:
            results = spotify_api.album_tracks(code)
        elif list_type == SpotifyPlaylistTypes.PLAYLIST:
            results = spotify_api.playlist_items(code)

        if results:
            first_total = results.get("total")
            while results:
                tracks.extend(results["items"])
                pages += 1
                results = spotify_api.next(results)
        else:
            print(
                f"Warning: Spotify API returned nothing"
                f" for {list_type} {code}",
                file=sys.stderr,
            )
    except Exception:
        if not pages:
            raise
        print(
            f"ERROR: Spotify API returned error for {list_type} {code}:",
            file=sys.stderr,
        )
        print_exc(file=sys.stderr)
        api_failed = True

    links = []
    for item in tracks:
        track = item.get("track", item)
        if track is None:
            # Spotify sends "track": null for removed/unavailable songs
            continue
        try:
            links.append(track["external_urls"]["spotify"])
        except KeyError as e:
            print(
                f"Warning: Cannot extract URL from {item}:"
                f" field {e.args[0]!r} is missing",
                file=sys.stderr,
            )
    if api_failed:
        truncation = Truncation.of(
            first_total, len(links), TruncationReason.API_ERROR
        )
    return SpotifyTracks(links, truncation)


_spotify_uri_regex = re.compile(rf"(?i:spotify):({_SPOTIFY_TYPES}):(\w+)")
_TRAILING_PUNCTUATION = ".,!?;:"


def _is_single_url(s: str) -> bool:
    """True for one URL (with a scheme://) or a spotify: URI, not for
    plain text that merely looks like word:word."""
    return bool(url_regex.fullmatch(s)) and (
        "://" in s or bool(_spotify_uri_regex.fullmatch(s))
    )


def _unwrapped_url(s: str) -> Optional[str]:
    """The single URL inside a <...> wrapper, or None."""
    if len(s) > 2 and s[0] == "<" and s[-1] == ">":
        inner = s[1:-1]
        if _is_single_url(inner.rstrip(_TRAILING_PUNCTUATION)):
            return inner
    return None


def normalize_track(text: str) -> str:
    """Cleans up user-typed track text before it is classified: strips
    whitespace, unwraps one <...> around a single URL (trailing
    punctuation may follow the wrapper or sit inside it), drops trailing
    sentence punctuation after a single URL, rewrites spotify: URIs and
    lower-cases a file: scheme written in any case.
    Anything else is returned unchanged apart from the strip."""
    text = text.strip()
    for candidate in (text, text.rstrip(_TRAILING_PUNCTUATION)):
        if (inner := _unwrapped_url(candidate)) is not None:
            text = inner
            break
    bare = text.rstrip(_TRAILING_PUNCTUATION)
    if bare != text and _is_single_url(bare):
        text = bare
    if m := _spotify_uri_regex.fullmatch(text):
        return f"https://open.spotify.com/{m.group(1)}/{m.group(2)}"
    scheme_len = len(FILE_SCHEME)
    if text[:scheme_len].lower() == FILE_SCHEME and _is_single_url(text):
        return FILE_SCHEME + text[scheme_len:]
    return text


def get_urls(content: str) -> List[str]:
    return [m[0] for m in url_regex.findall(content)]


def get_ie(url: str) -> Optional[ExtractorT]:
    for ie in EXTRACTORS:
        if ie.suitable(url) and ie.IE_NAME != "generic":
            return ie
    return None


def identify_url(url: str) -> Union[SiteTypes, ExtractorT]:
    if not url_regex.fullmatch(url):
        return SiteTypes.NOT_URL

    if spotify_regex.match(url):
        return SiteTypes.SPOTIFY

    if url.startswith(FILE_SCHEME):
        if not config.ENABLE_LOCAL_LIBRARY:
            return SiteTypes.UNKNOWN
        return SiteTypes.LOCAL_LIBRARY

    if ie := get_ie(url):
        return ie

    if urlparse(url).path.lower().endswith(config.SUPPORTED_EXTENSIONS):
        return SiteTypes.CUSTOM

    # If no match
    return SiteTypes.UNKNOWN


def get_site_type(url: str) -> SiteTypes:
    type_ = identify_url(url)
    if not isinstance(type_, SiteTypes):
        return SiteTypes.YT_DLP
    return type_
