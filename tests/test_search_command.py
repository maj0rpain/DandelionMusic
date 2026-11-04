"""d!search lets the search's SongError propagate, message intact, and
sends nothing itself (on_command_error does the replying)."""

import asyncio

import pytest

from config import config
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.loader import SongError


class RecordingCtx:
    def __init__(self):
        self.sent = []

    async def defer(self):
        pass

    async def send(self, message=None, **kwargs):
        self.sent.append(message)


@pytest.mark.parametrize(
    "message", [config.SONGINFO_ERROR, config.SEARCH_NO_RESULTS]
)
def test_a_search_error_propagates_and_nothing_is_sent(monkeypatch, message):
    async def search_youtube(query, count):
        raise SongError(message)

    monkeypatch.setattr(music, "search_youtube", search_youtube)
    ctx = RecordingCtx()
    with pytest.raises(SongError) as info:
        asyncio.run(Music._search.callback(Music(None), ctx, query="q"))
    assert info.value.args[0] == message
    assert ctx.sent == []
