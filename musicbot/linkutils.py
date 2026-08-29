import re
import sys
import asyncio
from enum import Enum, auto
from traceback import print_exc
from urllib.parse import urlparse
from multiprocessing import current_process
from typing import Dict, Optional, Union, List

from spotipy import Spotify
from bs4 import BeautifulSoup
from aiohttp import ClientSession
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.lazy_extractors import LazyLoadExtractor

from config import config
from musicbot import loader


spotify_api = None
if config.SPOTIFY_ID or config.SPOTIFY_SECRET:
    try:
        spotify_api = Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=config.SPOTIFY_ID,
                client_secret=config.SPOTIFY_SECRET,
            )
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
    r"^https?://open\.spotify\.com/([^/]+/)?"
    r"(?P<type>track|playlist|album)/(?P<code>\w+)"
)

headers = {}

# aiohttp binds a ClientSession to whatever loop is running when it is
# constructed and refuses to be driven from any other one, raising
# "Timeout context manager should be used inside a task". This process
# runs two loops - the bot's own, and loader.py's private _loop that
# drives the Spotify page lookups - so a single global session would be
# usable from only one of them. Keep one session per loop instead.
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
        session = _sessions[loop] = ClientSession(headers=headers)
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


async def get_soup(url: str) -> BeautifulSoup:
    async with get_session().get(url) as response:
        response.raise_for_status()
        page = await response.text()

    return BeautifulSoup(page, "html.parser")


async def fetch_spotify(url: str) -> Optional[Union[dict, List[str]]]:
    """Searches YouTube for Spotify song or loads Spotify playlist"""
    match = spotify_regex.match(url)
    # strip any extra parts
    url = match.group()
    url_type = match.group("type")
    if url_type != "track":
        return await fetch_spotify_playlist(url, url_type, match.group("code"))

    if spotify_api:
        track_data = spotify_api.track(url)
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
) -> List[str]:
    """Returns list of Spotify links"""

    if spotify_api:
        return fetch_playlist_with_api(SpotifyPlaylistTypes(list_type), code)

    soup = await get_soup(url)
    results = soup.find_all(attrs={"name": "music:song", "content": True})

    return [item["content"] for item in results]


def fetch_playlist_with_api(
    list_type: SpotifyPlaylistTypes, code: str
) -> List[str]:
    tracks = []
    try:
        if list_type == SpotifyPlaylistTypes.ALBUM:
            results = spotify_api.album_tracks(code)
        elif list_type == SpotifyPlaylistTypes.PLAYLIST:
            results = spotify_api.playlist_items(code)

        if results:
            while results:
                tracks.extend(results["items"])
                results = spotify_api.next(results)
        else:
            print(
                f"Warning: Spotify API returned nothing"
                f" for {list_type} {code}",
                file=sys.stderr,
            )
    except Exception:
        print(
            f"ERROR: Spotify API returned error for {list_type} {code}:",
            file=sys.stderr,
        )
        print_exc(file=sys.stderr)

    links = []
    for track in tracks:
        try:
            links.append(track.get("track", track)["external_urls"]["spotify"])
        except KeyError as e:
            print(
                f"Warning: Cannot extract URL from {track}:"
                f" field {e.args[0]!r} is missing",
                file=sys.stderr,
            )
    return links


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
