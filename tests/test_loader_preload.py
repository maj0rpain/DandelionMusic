"""loader.preload tells its caller whether it actually extracted.

_load is monkeypatched, so no worker is started and nothing
reaches the network: these tests drive preload's own decisions - the
early returns, a fresh extraction, a failed one, and a second caller
awaiting an extraction already in flight.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.song import LoadResult, Song
from musicbot.truncation import Truncation, TruncationReason
from helpers import fake_redirect_follower, run

Preload = loader.Preload

URL = "https://example.invalid/track"


def _song(**kw):
    return Song(SiteTypes.YT_DLP, URL, **kw)


def _never_loads(monkeypatch):
    async def load(lane, track):
        raise AssertionError("preload extracted a song that was current")

    monkeypatch.setattr(loader, "_load", load)


def test_a_song_with_no_webpage_url_is_current(monkeypatch):
    _never_loads(monkeypatch)
    song = Song(SiteTypes.LOCAL_LIBRARY, None, url="/music/a.mp3")
    assert run(loader.preload(song)) is Preload.CURRENT


def test_a_song_whose_stream_url_has_not_expired_is_current(monkeypatch):
    _never_loads(monkeypatch)
    song = _song(url="https://stream.invalid/a?expire=4102444800")
    assert run(loader.preload(song)) is Preload.CURRENT


def test_a_fresh_extraction_updates_the_song_and_reports_it(monkeypatch):
    async def load(lane, track):
        return LoadResult.of_song(
            Song(
                SiteTypes.YT_DLP,
                track,
                url="https://stream.invalid/a",
                title="fresh title",
            )
        )

    monkeypatch.setattr(loader, "_load", load)
    song = _song(title="stale title")
    assert run(loader.preload(song)) is Preload.EXTRACTED
    assert song.title == "fresh title"


@pytest.mark.parametrize(
    "outcome", [loader.SongError("gone"), RuntimeError("boom")]
)
def test_a_failed_extraction_reports_failure(monkeypatch, outcome):
    async def load(lane, track):
        raise outcome

    monkeypatch.setattr(loader, "_load", load)
    song = _song(title="stale title")
    assert run(loader.preload(song)) is Preload.FAILED
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
    async def scenario():
        release = asyncio.Event()

        async def load(lane, track):
            await release.wait()
            if extracted:
                return LoadResult.of_song(
                    Song(SiteTypes.YT_DLP, track, url="https://s.invalid")
                )
            raise loader.SongError("gone")

        monkeypatch.setattr(loader, "_load", load)
        song = _song()
        running = asyncio.create_task(loader.preload(song))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(loader.preload(song))
        await asyncio.sleep(0)
        release.set()
        return await running, await waiting

    assert run(scenario()) == (first, second)


def test_cancelling_one_waiter_leaves_the_owner_and_other_waiters_alone(
    monkeypatch,
):
    async def scenario():
        release = asyncio.Event()

        async def load(lane, track):
            await release.wait()
            return LoadResult.of_song(
                Song(SiteTypes.YT_DLP, track, url="https://s.invalid")
            )

        monkeypatch.setattr(loader, "_load", load)
        song = _song()
        owner = asyncio.create_task(loader.preload(song))
        await asyncio.sleep(0)
        cancelled = asyncio.create_task(loader.preload(song))
        other = asyncio.create_task(loader.preload(song))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(
            owner, cancelled, other, return_exceptions=True
        )

    owner, cancelled, other = run(scenario())
    assert isinstance(cancelled, asyncio.CancelledError)
    assert (owner, other) == (Preload.EXTRACTED, Preload.CURRENT)


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)


def _clock(monkeypatch, now=NOW):
    monkeypatch.setattr(loader, "_now", lambda: now)


def _expiring_in(delta):
    return f"https://stream.invalid/a?expire={int((NOW + delta).timestamp())}"


def _fresh_load(monkeypatch):
    calls = []

    async def load(lane, track):
        calls.append(track)
        return LoadResult.of_song(
            Song(SiteTypes.YT_DLP, track, url="https://s.invalid/new")
        )

    monkeypatch.setattr(loader, "_load", load)
    return calls


@pytest.mark.parametrize("count", [None, 2])
def test_load_song_stamps_every_song_with_the_clock(monkeypatch, count):
    _clock(monkeypatch)

    async def run_sync(lane, f, *args):
        n = count or 1
        return LoadResult(
            [Song(SiteTypes.YT_DLP, URL) for _ in range(n)], n == 1
        )

    monkeypatch.setattr(loader, "_run_sync", run_sync)
    result = run(loader.load_song(URL))
    assert [s.extracted_at for s in result.songs] == [NOW] * (count or 1)


def test_no_expire_with_a_fresh_stamp_is_current(monkeypatch):
    _never_loads(monkeypatch)
    _clock(monkeypatch)
    song = _song(url="https://stream.invalid/a")
    song.extracted_at = NOW - 9 * MINUTE
    assert run(loader.preload(song)) is Preload.CURRENT


@pytest.mark.parametrize("age", [11 * MINUTE, None])
def test_no_expire_with_a_stale_or_missing_stamp_is_reextracted(
    monkeypatch, age
):
    calls = _fresh_load(monkeypatch)
    _clock(monkeypatch)
    song = _song(url="https://stream.invalid/a")
    if age is not None:
        song.extracted_at = NOW - age
    assert run(loader.preload(song)) is Preload.EXTRACTED
    assert calls == [URL]


def test_an_expire_within_the_margin_is_reextracted(monkeypatch):
    calls = _fresh_load(monkeypatch)
    _clock(monkeypatch)
    song = _song(url=_expiring_in(9 * MINUTE))
    assert run(loader.preload(song)) is Preload.EXTRACTED
    assert calls == [URL]


def test_an_expire_beyond_the_margin_is_current(monkeypatch):
    _never_loads(monkeypatch)
    _clock(monkeypatch)
    song = _song(url=_expiring_in(11 * MINUTE))
    assert run(loader.preload(song)) is Preload.CURRENT


def test_a_stream_url_with_its_webpage_urls_expire_is_current(monkeypatch):
    _never_loads(monkeypatch)
    _clock(monkeypatch)
    url = _expiring_in(MINUTE)
    song = Song(SiteTypes.YT_DLP, url, url=url)
    assert run(loader.preload(song)) is Preload.CURRENT


def test_a_local_library_song_is_always_current(monkeypatch):
    _never_loads(monkeypatch)
    _clock(monkeypatch)
    song = Song(
        SiteTypes.LOCAL_LIBRARY,
        "file:///music/a.mp3",
        url=_expiring_in(-MINUTE),
    )
    assert run(loader.preload(song)) is Preload.CURRENT


def _custom(url=None, link=URL):
    return Song(SiteTypes.CUSTOM, link, url=url)


@pytest.mark.parametrize("delta", [-MINUTE, 9 * MINUTE])
def test_a_custom_song_near_or_past_expiry_is_reloaded(monkeypatch, delta):
    calls = _fresh_load(monkeypatch)
    _clock(monkeypatch)
    song = _custom(_expiring_in(delta))
    assert run(loader.preload(song)) is Preload.EXTRACTED
    assert calls == [URL]


@pytest.mark.parametrize("age", [11 * MINUTE, None])
def test_a_custom_song_with_no_expiry_and_a_stale_stamp_is_reloaded(
    monkeypatch, age
):
    calls = _fresh_load(monkeypatch)
    _clock(monkeypatch)
    song = _custom("https://stream.invalid/a")
    if age is not None:
        song.extracted_at = NOW - age
    assert run(loader.preload(song)) is Preload.EXTRACTED
    assert calls == [URL]


def test_a_custom_song_with_no_expiry_and_a_fresh_stamp_is_current(
    monkeypatch,
):
    _never_loads(monkeypatch)
    _clock(monkeypatch)
    song = _custom("https://stream.invalid/a")
    song.extracted_at = NOW - 9 * MINUTE
    assert run(loader.preload(song)) is Preload.CURRENT


@pytest.mark.parametrize("delta", [-MINUTE, 9 * MINUTE])
def test_a_custom_link_that_is_its_own_stream_is_reloaded_near_expiry(
    monkeypatch, delta
):
    calls = _fresh_load(monkeypatch)
    _clock(monkeypatch)
    url = _expiring_in(delta)
    assert run(loader.preload(_custom(url, link=url))) is Preload.EXTRACTED
    assert calls == [url]


def test_a_custom_link_that_is_its_own_stream_is_current_far_from_expiry(
    monkeypatch,
):
    _never_loads(monkeypatch)
    _clock(monkeypatch)
    url = _expiring_in(11 * MINUTE)
    assert run(loader.preload(_custom(url, link=url))) is Preload.CURRENT


def test_a_failed_custom_reload_reports_failure(monkeypatch):
    async def load(lane, token):
        raise loader.SongError("gone")

    monkeypatch.setattr(loader, "_load", load)
    _clock(monkeypatch)
    song = _custom(_expiring_in(-MINUTE))
    assert run(loader.preload(song)) is Preload.FAILED


def test_the_controller_seams_stub_preload_matches_the_real_one():
    from controller_seam import StubLoader

    assert [m.name for m in StubLoader.Preload] == [m.name for m in Preload]


def test_preload_loads_a_plain_string_with_no_admitted_link(monkeypatch):
    seen = []

    def load_song(track):
        seen.append((track, loader._admitted_link))
        return LoadResult.of_song(
            Song(SiteTypes.YT_DLP, track, url="https://s.invalid/new")
        )

    async def run_sync(lane, f, *args):
        return f(*args)

    monkeypatch.setattr(loader, "_load_song_admitting", load_song)
    monkeypatch.setattr(loader, "_run_sync", run_sync)
    assert run(loader.preload(_song())) is Preload.EXTRACTED
    assert seen == [(URL, None)]


def test_load_in_the_preload_lane_dispatches_there_and_stamps_the_clock(
    monkeypatch,
):
    _clock(monkeypatch)
    lanes = []

    async def run_sync(lane, f, *args):
        lanes.append(lane)
        return LoadResult.of_song(Song(SiteTypes.YT_DLP, URL))

    monkeypatch.setattr(loader, "_run_sync", run_sync)
    result = run(loader._load(loader.Lane.PRELOAD, URL))
    assert (lanes, result.songs[0].extracted_at) == (
        [loader.Lane.PRELOAD],
        NOW,
    )


def test_preload_extracts_in_the_preload_lane(monkeypatch):
    lanes = []

    async def load(lane, track):
        lanes.append(lane)
        return LoadResult.of_song(
            Song(SiteTypes.YT_DLP, track, url="https://s.invalid/new")
        )

    monkeypatch.setattr(loader, "_load", load)
    run(loader.preload(_song()))
    assert lanes == [loader.Lane.PRELOAD]


@pytest.mark.parametrize(
    "result",
    [
        LoadResult([Song(SiteTypes.YT_DLP, URL)] * 2, False),
        LoadResult(
            [Song(SiteTypes.YT_DLP, URL)],
            False,
            Truncation(5, TruncationReason.NO_API),
        ),
    ],
)
def test_a_result_that_is_not_single_fails_the_preload(monkeypatch, result):
    async def load(lane, track):
        return result

    monkeypatch.setattr(loader, "_load", load)
    song = _song(title="stale title")
    assert run(loader.preload(song)) is Preload.FAILED
    assert song.title == "stale title"


def test_a_flac_attachment_without_a_stream_url_is_extracted(
    monkeypatch, worker_loop
):
    cdn = "https://cdn.discordapp.com/attachments/1/2/a.flac?ex=65&is=1"

    fake_redirect_follower(monkeypatch, cdn)

    async def load(lane, track):
        # the worker runs it off the event loop, as the lane does
        return await asyncio.to_thread(loader._load_song, track)

    monkeypatch.setattr(loader, "_load", load)
    song = Song(SiteTypes.YT_DLP, cdn)
    assert run(loader.preload(song)) == Preload.EXTRACTED
    assert song.url is not None
