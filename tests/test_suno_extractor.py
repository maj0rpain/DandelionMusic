"""The Suno extractor turns a failed page fetch into a named DownloadError.

`linkutils.get_soup` is patched to time out; the extractor runs on the
worker loop the loader would give it.
"""

import asyncio

import pytest
from yt_dlp.utils import DownloadError

from musicbot import linkutils
from musicbot.yt_dlp_plugins.extractor.suno import SunoAIIE

URL = "https://suno.com/song/abc123"


def test_a_timed_out_suno_link_is_a_named_download_error(
    monkeypatch, worker_loop
):
    async def get_soup(url):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(linkutils, "get_soup", get_soup)
    with pytest.raises(DownloadError) as info:
        SunoAIIE()._real_extract(URL)
    assert info.value.msg == "TimeoutError"


def test_download_error_keeps_an_exceptions_message():
    assert linkutils.download_error(RuntimeError("404")).msg == "404"
