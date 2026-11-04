"""No single guild's Spotify or aiohttp request can hold its extraction
lane's worker for long.

Song loading is called directly with Spotify failures faked; the
Spotify session is driven against a local server that always answers
429 with a huge Retry-After, with urllib3's sleep recorded; and
get_session()'s timeout is read off the session it returns.
"""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import pytest
import requests
from spotipy import Spotify
from spotipy.exceptions import SpotifyException, SpotifyOauthError

from config import config
from musicbot import linkutils, loader
from musicbot.loader import SongError

TRACK = "https://open.spotify.com/track/abc123"


class _FailingApi:
    def __init__(self, exc):
        self.exc = exc

    def track(self, url):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        SpotifyException(429, -1, "rate limited"),
        SpotifyOauthError("token request failed"),
        requests.exceptions.RetryError("too many 429 responses"),
        TimeoutError(),
        aiohttp.ClientConnectionError("connection dropped"),
    ],
    ids=["api", "oauth", "token-retry", "timeout", "aiohttp"],
)
def test_a_failed_spotify_lookup_is_a_could_not_load_error(
    monkeypatch, worker_loop, exc
):
    monkeypatch.setattr(linkutils, "spotify_api", _FailingApi(exc))
    with pytest.raises(SongError) as info:
        loader._load_song(TRACK)
    assert str(info.value) == config.SONGINFO_ERROR
    assert info.value.__cause__ is exc


class _Always429(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(429)
        self.send_header("Retry-After", "3600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def rate_limiting_server():
    _Always429.hits = 0
    server = HTTPServer(("127.0.0.1", 0), _Always429)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()


def test_a_rate_limited_request_retries_briefly_then_gives_up(
    monkeypatch, rate_limiting_server
):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with pytest.raises(requests.exceptions.RetryError):
        linkutils.spotify_session().get(rate_limiting_server)
    assert _Always429.hits == Spotify.max_retries + 1  # and its retries
    assert sleeps
    assert max(sleeps) <= linkutils.RETRY_AFTER_MAX


@pytest.fixture
def api():
    return linkutils.build_spotify_api("id", "secret")


def test_build_spotify_api_gives_both_clients_the_capped_session(api):
    for session in (api._session, api.auth_manager._session):
        retry = session.get_adapter("https://api.spotify.com").max_retries
        assert retry.retry_after_max == linkutils.RETRY_AFTER_MAX


def test_build_spotify_api_bounds_the_token_request(api):
    assert api.requests_timeout == linkutils.SPOTIFY_TIMEOUT
    assert api.auth_manager.requests_timeout == linkutils.SPOTIFY_TIMEOUT


def test_our_spotify_retry_matches_spotipys_own():
    ours = vars(
        linkutils.spotify_session().get_adapter("https://x").max_retries
    )
    theirs = vars(
        Spotify(auth="x")._session.get_adapter("https://x").max_retries
    )
    # urllib3 gives its own Retry a default retry_after_max; ours is capped
    ours.pop("retry_after_max")
    theirs.pop("retry_after_max", None)
    assert ours == theirs


def test_build_spotify_api_writes_no_token_cache_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = linkutils.build_spotify_api("id", "secret")
    token = {"access_token": "t", "expires_at": 9999999999}
    api.auth_manager.cache_handler.save_token_to_cache(token)
    assert api.auth_manager.cache_handler.get_cached_token() == token
    assert list(tmp_path.iterdir()) == []


def test_build_spotify_api_without_credentials_is_none():
    assert linkutils.build_spotify_api(None, None) is None


def test_build_spotify_api_returns_none_when_construction_fails(
    monkeypatch,
):
    def boom(*args, **kwargs):
        raise RuntimeError("no spotify")

    monkeypatch.setattr(linkutils, "Spotify", boom)
    assert linkutils.build_spotify_api("id", "secret") is None


def test_every_http_session_has_the_total_timeout():
    async def run():
        try:
            return linkutils.get_session().timeout.total
        finally:
            await linkutils.stop()

    assert asyncio.run(run()) == linkutils.HTTP_TIMEOUT
