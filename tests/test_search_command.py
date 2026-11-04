"""d!search tells a failed search apart from one that found nothing."""

from config import config
from musicbot.commands import music
from musicbot.commands.music import Music
from helpers import run


class RecordingCtx:
    def __init__(self):
        self.sent = []

    async def defer(self):
        pass

    async def send(self, message=None, **kwargs):
        self.sent.append(message)


def run_search(results, monkeypatch):
    async def search_youtube(query, count):
        return results

    monkeypatch.setattr(music, "search_youtube", search_youtube)
    ctx = RecordingCtx()
    run(Music._search.callback(Music(None), ctx, query="q"))
    return ctx.sent


def test_a_failed_search_replies_songinfo_error(monkeypatch):
    assert run_search(None, monkeypatch) == [config.SONGINFO_ERROR]


def test_an_empty_search_replies_no_results(monkeypatch):
    assert run_search([], monkeypatch) == [config.SEARCH_NO_RESULTS]
