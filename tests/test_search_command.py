"""d!search lets the search's SongError propagate, message intact, and
sends nothing itself (on_command_error does the replying)."""

import pytest

from config import config
from musicbot.commands import music
from musicbot.commands.music import Music
from discord_fakes import RecordingCtx
from musicbot.loader import SongError
from helpers import plain_song, run


def test_a_search_error_propagates_and_nothing_is_sent(monkeypatch):
    message = config.SEARCH_NO_RESULTS

    async def search_youtube(query, count):
        raise SongError(message)

    monkeypatch.setattr(music, "search_youtube", search_youtube)
    ctx = RecordingCtx()
    with pytest.raises(SongError) as info:
        run(Music._search.callback(Music(None), ctx, query="q"))
    assert info.value.args[0] == message
    assert ctx.sent == []


def test_search_offers_the_loaders_songs_as_its_picks(monkeypatch):
    songs = [plain_song("T1"), plain_song("T2")]

    async def search_youtube(query, count):
        return songs

    monkeypatch.setattr(music, "search_youtube", search_youtube)

    class KwargCtx(RecordingCtx):
        async def send(self, message=None, **kwargs):
            self.sent.append(kwargs)
            return "message"

    ctx = KwargCtx()
    run(Music._search.callback(Music(None), ctx, query="q"))
    sent = ctx.sent[-1]
    assert len(sent["view"].children) == 2
    values = " ".join(f.value for f in sent["embed"].fields)
    assert "T1" in values and "T2" in values
