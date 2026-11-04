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
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.lazy_extractors import LazyLoadExtractor

from config import config
from musicbot import loader

# Limits on the network calls the extraction worker makes. One guild's
# call that blocks holds the worker for every guild, so none may block
# long. Deliberately not Config settings: nobody has a reason to tune them.
# longest wait honoured for one Spotify 429 Retry-After header (seconds)
RETRY_AFTER_MAX = 5
# total time limit of one aiohttp request (seconds)
HTTP_TIMEOUT = 15


def spotify_session() -> requests.Session:
    """A requests session whose retries match spotipy's own defaults,
    except that a Retry-After wait is capped at RETRY_AFTER_MAX."""
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
            auth_manager=SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                requests_session=session,
                requests_timeout=5,
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
spotify_regex = re.compile(
    r"^(?i:https?://open\.spotify\.com)/([^/]+/)?"
    r"(?P<type>track|playlist|album)/(?P<code>\w+)"
)

headers = {}

# aiohttp binds a ClientSession to whatever loop is running when it is
# constructed and refuses to be driven from any other one, raising
# "Timeout context manager should be used inside a task". This module
# runs under two loops - the bot's own, in the bot's process, and
# loader.py's private _loop that drives the Spotify page lookups, in the
# extraction worker - so a session must belong to the loop that uses it.
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
    """Links of a Spotify playlist or album. `total` is the number of
    tracks Spotify reports when the fetch is known to be partial, else
    None; `reason` is the config key of why it is partial."""

    links: List[str]
    total: Optional[int] = None
    reason: Optional[str] = None


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
    results = loader._search_youtube(query)
    return results[0] if results else None


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

    return SpotifyTracks(
        [item["content"] for item in results],
        total,
        "SPOTIFY_TRUNCATED_NO_API",
    )


def fetch_playlist_with_api(
    list_type: SpotifyPlaylistTypes, code: str
) -> SpotifyTracks:
    tracks = []
    total = None
    reason = None
    pages = 0
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
        total = first_total
        reason = "SPOTIFY_TRUNCATED_API_ERROR"

    links = []
    for track in tracks:
        item = track.get("track", track)
        if item is None:
            # Spotify sends "track": null for removed/unavailable songs
            continue
        try:
            links.append(item["external_urls"]["spotify"])
        except KeyError as e:
            print(
                f"Warning: Cannot extract URL from {track}:"
                f" field {e.args[0]!r} is missing",
                file=sys.stderr,
            )
    return SpotifyTracks(links, total, reason)


_spotify_uri_regex = re.compile(r"spotify:(track|album|playlist):(\w+)")
_TRAILING_PUNCTUATION = ".,!?;:"


def normalize_track(text: str) -> str:
    """Cleans up user-typed track text before it is classified: strips
    whitespace, unwraps one <...> around a single URL, drops trailing
    sentence punctuation after a single URL and rewrites spotify: URIs.
    Anything else is returned unchanged apart from the strip."""
    text = text.strip()
    if (
        len(text) > 2
        and text[0] == "<"
        and text[-1] == ">"
        and url_regex.fullmatch(text[1:-1])
    ):
        text = text[1:-1]
    if m := _spotify_uri_regex.fullmatch(text):
        return f"https://open.spotify.com/{m.group(1)}/{m.group(2)}"
    bare = text.rstrip(_TRAILING_PUNCTUATION)
    if bare != text and url_regex.fullmatch(bare):
        return bare
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

    if url.startswith("file://"):
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
