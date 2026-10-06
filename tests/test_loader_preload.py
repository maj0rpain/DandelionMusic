"""loader.preload tells its caller whether it actually extracted.

load_song is monkeypatched, so no worker is started and nothing
reaches the network: these tests drive preload's own decisions - the
early returns, a fresh extraction, a failed one, and a second caller
awaiting an extraction already in flight.
"""

import asyncio

import pytest

from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.song import Song

Preload = loader.Preload

URL = "https://example.invalid/track"


def _song(**kw):
    return Song(SiteTypes.YT_DLP, URL, **kw)


def _never_loads(monkeypatch):
    async def load_song(track):
        raise AssertionError("preload extracted a song that was current")

    monkeypatch.setattr(loader, "load_song", load_song)


def test_a_song_with_no_webpage_url_is_current(monkeypatch):
    _never_loads(monkeypatch)
    song = Song(SiteTypes.LOCAL_LIBRARY, None, url="/music/a.mp3")
    assert asyncio.run(loader.preload(song)) is Preload.CURRENT


def test_a_song_whose_stream_url_has_not_expired_is_current(monkeypatch):
    _never_loads(monkeypatch)
    song = _song(url="https://stream.invalid/a?expire=4102444800")
    assert asyncio.run(loader.preload(song)) is Preload.CURRENT


def test_a_fresh_extraction_updates_the_song_and_reports_it(monkeypatch):
    async def load_song(track):
        return Song(
            SiteTypes.YT_DLP,
            track,
            url="https://stream.invalid/a",
            title="fresh title",
        )

    monkeypatch.setattr(loader, "load_song", load_song)
    song = _song(title="stale title")
    assert asyncio.run(loader.preload(song)) is Preload.EXTRACTED
    assert song.title == "fresh title"


@pytest.mark.parametrize(
    "outcome", [loader.SongError("gone"), RuntimeError("boom"), None]
)
def test_a_failed_extraction_reports_failure(monkeypatch, outcome):
    async def load_song(track):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(loader, "load_song", load_song)
    song = _song(title="stale title")
    assert asyncio.run(loader.preload(song)) is Preload.FAILED
    assert song.title == "stale title"


@pytest.mark.parametrize(
    "extracted, first, second",
    [
        (True, Preload.EXTRACTED, Preload.CURRENT),
        (False, Preload.FAILED, Preload.FAILED),
    ],
)
def test_a_caller_awaiting_an_inflight_extraction_is_told_its_outcome(
    monkeypatch, extracted, first, second
):
    async def run():
        release = asyncio.Event()

        async def load_song(track):
            await release.wait()
            if extracted:
                return Song(SiteTypes.YT_DLP, track, url="https://s.invalid")
            return None

        monkeypatch.setattr(loader, "load_song", load_song)
        song = _song()
        running = asyncio.create_task(loader.preload(song))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(loader.preload(song))
        await asyncio.sleep(0)
        release.set()
        return await running, await waiting

    assert asyncio.run(run()) == (first, second)


def test_the_controller_seams_stub_preload_matches_the_real_one():
    from controller_seam import StubLoader

    assert [m.name for m in StubLoader.Preload] == [m.name for m in Preload]
