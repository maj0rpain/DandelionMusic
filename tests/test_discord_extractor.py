"""The Discord attachment extractor gives up within linkutils.HTTP_TIMEOUT.

The extractor builds its own HTTPClient; a fake one stands in for it.
"""

import asyncio
import time

import pytest
from yt_dlp.utils import DownloadError

from config import config
from musicbot import linkutils, loader
from musicbot.loader import SongError
from musicbot.yt_dlp_plugins.extractor import discord as extractor
from musicbot.yt_dlp_plugins.extractor.discord import DiscordAttachmentsIE

URL = "https://discord.com/channels/1/2/3"
LIMIT = 0.05
MAX_ELAPSED = LIMIT * 10

MESSAGE = {"author": {"username": "alice"}, "attachments": []}


class _Http:
    def __init__(self, loop=None):
        self.login_stalls = False
        self.fetch_stalls = False
        self.login_exc = None

    async def close(self):
        pass

    async def static_login(self, token):
        if self.login_stalls:
            await asyncio.Event().wait()
        if self.login_exc:
            raise self.login_exc

    async def get_message(self, channel_id, message_id):
        if self.fetch_stalls:
            await asyncio.Event().wait()
        return MESSAGE


@pytest.fixture
def http(monkeypatch, worker_loop):
    http = _Http()
    monkeypatch.setattr(extractor, "HTTPClient", lambda loop: http)
    monkeypatch.setattr(extractor, "_client", None)
    monkeypatch.setattr(extractor.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(linkutils, "HTTP_TIMEOUT", LIMIT)
    monkeypatch.setattr(loader, "_admitted_link", URL)
    return http


def _extract():
    start = time.monotonic()
    try:
        return DiscordAttachmentsIE()._real_extract(URL)
    finally:
        assert time.monotonic() - start < MAX_ELAPSED


def _fails(message):
    with pytest.raises(DownloadError) as info:
        _extract()
    assert info.value.msg == message


def test_a_stalled_login_is_a_download_error(http):
    http.login_stalls = True
    _fails("TimeoutError")


def test_a_stalled_fetch_is_a_download_error(http):
    http.fetch_stalls = True
    _fails("TimeoutError")


@pytest.mark.parametrize("fault", ["stalled-fetch", "raising-login"])
def test_a_failed_discord_link_is_a_could_not_load_error(
    monkeypatch, http, fault
):
    monkeypatch.setattr(loader, "_downloader", loader._new_downloader())
    if fault == "stalled-fetch":
        http.fetch_stalls = True
    else:
        http.login_exc = RuntimeError("bad token")
    with pytest.raises(SongError) as info:
        loader._load_song(URL, URL)
    assert str(info.value) == config.SONGINFO_ERROR
