import atexit
import sys

import pytest
from yt_dlp.utils import DownloadError

from config import config
from musicbot.yt_dlp_plugins.extractor import discord as extractor
from message_link_fakes import FakeHTTPClient

URL = "https://discord.com/channels/1/22/333"


@pytest.fixture
def exit_handlers(monkeypatch, worker_loop):
    FakeHTTPClient.instances = []
    FakeHTTPClient.login_error = None
    FakeHTTPClient.fetch_error = None
    handlers = []
    monkeypatch.setattr(extractor, "HTTPClient", FakeHTTPClient)
    monkeypatch.setattr(extractor, "_client", None)
    monkeypatch.setattr(config, "BOT_TOKEN", "tok")
    monkeypatch.setattr(
        atexit, "register", lambda f, *a, **k: handlers.append(f)
    )
    monkeypatch.delitem(sys.modules, "musicbot.__main__", raising=False)
    yield handlers


def extract():
    ie = extractor.DiscordAttachmentsIE()
    return ie._real_extract(URL)


def test_two_extractions_log_in_once_and_fetch_by_ids(exit_handlers):
    extract()
    extract()
    (client,) = FakeHTTPClient.instances
    assert client.logins == ["tok"]
    assert client.fetches == [(22, 333), (22, 333)]


def test_result_is_playlist_of_attachments(exit_handlers):
    assert extract() == {
        "_type": "playlist",
        "entries": [
            {
                "id": "9",
                "url": "https://cdn/a.mp3",
                "title": "a.mp3",
                "uploader": "alice",
            },
            {
                "id": "10",
                "url": "https://cdn/b.mp3",
                "title": "b.mp3",
                "uploader": "alice",
            },
        ],
    }


def test_one_exit_handler_closes_the_client(exit_handlers):
    extract()
    extract()
    assert len(exit_handlers) == 1
    exit_handlers[0]()
    assert FakeHTTPClient.instances[0].closed


def test_fetch_failure_is_download_error(exit_handlers):
    FakeHTTPClient.fetch_error = RuntimeError("boom")
    with pytest.raises(DownloadError):
        extract()


def test_extraction_does_not_import_the_bot_entrypoint(exit_handlers):
    extract()
    assert "musicbot.__main__" not in sys.modules


def test_failed_login_is_download_error_and_retried(exit_handlers):
    FakeHTTPClient.login_error = RuntimeError("bad token")
    with pytest.raises(DownloadError):
        extract()
    assert FakeHTTPClient.instances[0].closed
    assert exit_handlers == []
    FakeHTTPClient.login_error = None
    extract()
    assert len(FakeHTTPClient.instances) == 2
    assert FakeHTTPClient.instances[1].logins == ["tok"]
    assert len(exit_handlers) == 1
