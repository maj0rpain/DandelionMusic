"""The Discord attachment extractor gives up within linkutils.HTTP_TIMEOUT.

`musicbot.__main__` is replaced by a stub holding a fake bot, since the
real one builds a MusicBot at import time.
"""

import asyncio
import sys
import time
import types

import pytest
from yt_dlp.utils import DownloadError

from config import config
from musicbot import linkutils, loader
from musicbot.loader import SongError
from musicbot.yt_dlp_plugins.extractor.discord import DiscordAttachmentsIE

URL = "https://discord.com/channels/1/2/3"
LIMIT = 0.05
FAST = 1.0

MESSAGE = {
    "author": {"username": "alice"},
    "attachments": [
        {"id": "10", "url": "https://cdn/a.mp3", "filename": "a.mp3"},
        {"id": "11", "url": "https://cdn/b.mp3", "filename": "b.mp3"},
    ],
}


class _Http:
    def __init__(self):
        self.token = None
        self.logins = 0
        self.login_delay = 0
        self.fetch_delay = 0
        self.login_exc = None
        self.fetch_exc = None

    async def static_login(self, token):
        self.logins += 1
        await _pause(self.login_delay)
        if self.login_exc:
            raise self.login_exc
        self.token = token

    async def get_message(self, channel_id, message_id):
        await _pause(self.fetch_delay)
        if self.fetch_exc:
            raise self.fetch_exc
        return MESSAGE


async def _pause(delay):
    if delay is None:
        await asyncio.Event().wait()
    elif delay:
        await asyncio.sleep(delay)


@pytest.fixture
def http(monkeypatch, worker_loop):
    http = _Http()
    stub = types.ModuleType("musicbot.__main__")
    stub.bot = types.SimpleNamespace(http=http)
    monkeypatch.setitem(sys.modules, "musicbot.__main__", stub)
    monkeypatch.setattr(linkutils, "HTTP_TIMEOUT", LIMIT)
    return http


def _extract():
    start = time.monotonic()
    try:
        return DiscordAttachmentsIE()._real_extract(URL)
    finally:
        assert time.monotonic() - start < FAST


def _fails():
    with pytest.raises(DownloadError):
        _extract()


def test_a_stalled_login_is_a_download_error(http):
    http.login_delay = None
    _fails()


def test_a_stalled_fetch_is_a_download_error(http):
    http.token = "t"
    http.fetch_delay = None
    _fails()


def test_login_and_fetch_share_one_limit(http):
    http.login_delay = http.fetch_delay = LIMIT * 0.6
    _fails()


def test_a_raising_login_is_a_download_error(http):
    http.login_exc = RuntimeError("bad token")
    _fails()


def test_a_raising_fetch_is_a_download_error(http):
    http.token = "t"
    http.fetch_exc = RuntimeError("404")
    _fails()


def test_a_set_token_skips_login(http):
    http.token = "t"
    assert _extract()["_type"] == "playlist"
    assert http.logins == 0


def test_a_normal_fetch_is_a_playlist_of_attachments(http):
    result = _extract()
    assert http.logins == 1
    assert result == {
        "_type": "playlist",
        "entries": [
            {
                "id": a["id"],
                "url": a["url"],
                "title": a["filename"],
                "uploader": "alice",
            }
            for a in MESSAGE["attachments"]
        ],
    }


def test_a_failed_discord_link_is_a_could_not_load_error(monkeypatch, http):
    monkeypatch.setattr(loader, "_downloader", loader._new_downloader())
    http.login_exc = RuntimeError("bad token")
    with pytest.raises(SongError) as info:
        loader._load_song(URL)
    assert str(info.value) == config.SONGINFO_ERROR
