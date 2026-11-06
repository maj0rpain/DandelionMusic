"""What `enqueue`, `enqueue_next` and `process_song` report, against a
real controller with the loader stubbed."""

import asyncio

from musicbot.commands.music import Music
from musicbot.outcome import BatchReport, OutcomeKind
from musicbot.song import LoadResult
from musicbot.truncation import Truncation, TruncationReason
from controller_helpers import (
    _no_idle_timer,
    _fake_ffmpeg,
    _queue,
    _run,
    _run_while_playing,
    _settle,
    _song,
    _songs,
    _titles,
    command_ctx,
)


def test_a_first_track_whose_preload_fails_is_start_failed(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _no_idle_timer(controller, monkeypatch)
    stub_loader.results["preload"] = stub_loader.Preload.FAILED
    stub_loader.results["load_song"] = LoadResult.of_song(
        _song(controller, "a")
    )
    ctx, sent = command_ctx(controller)

    _run(controller, lambda: Music(None)._play_song(ctx, "a"))

    assert sent == []
    assert len(controller.panel.skipped) == 1


def test_two_concurrent_enqueues_while_idle_start_then_queue(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    first, second = _songs(controller, "first", "second")
    release = stub_loader.hold(first)

    async def scenario():
        one = asyncio.ensure_future(controller.enqueue([first]))
        await _settle()
        two = asyncio.ensure_future(controller.enqueue([second]))
        await _settle()
        release.set()
        return await asyncio.gather(one, two)

    started, queued = _run(controller, scenario)

    assert started.kind is OutcomeKind.STARTED
    assert queued.kind is OutcomeKind.QUEUED
    assert queued.position == 2


def test_enqueue_while_active_reports_its_d_queue_position(controller):
    _queue(controller, "current", "a", "b")
    (added,) = _songs(controller, "added")

    outcome = _run_while_playing(
        controller, lambda: controller.enqueue([added])
    )

    assert (outcome.kind, outcome.position) == (OutcomeKind.QUEUED, 4)
    assert _remove(controller, 4) == added


def test_enqueue_next_while_active_reports_position_2(controller):
    _queue(controller, "current", "a")
    (added,) = _songs(controller, "added")

    outcome = _run_while_playing(
        controller, lambda: controller.enqueue_next(added)
    )

    assert (outcome.kind, outcome.position) == (OutcomeKind.QUEUED, 2)
    assert _remove(controller, 2) == added


def _remove(controller, position):
    removed = []
    real = controller.remove

    def remove(index):
        song = real(index)
        removed.append(song)
        return song

    controller.remove = remove
    ctx, _ = command_ctx(controller)
    _run_while_playing(
        controller, lambda: Music._remove.callback(Music(None), ctx, position)
    )
    return removed[0]


def _process(controller, **kwargs):
    return _run(controller, lambda: controller.process_song("x", **kwargs))


def test_process_song_of_a_single_result_gives_a_single_song_outcome(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    stub_loader.results["load_song"] = LoadResult.of_song(
        _song(controller, "a")
    )

    outcome = _process(controller)

    assert outcome.kind is OutcomeKind.STARTED
    assert outcome.song.title == "a"


def test_enqueue_of_one_song_with_batch_is_a_playlist(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    songs = _songs(controller, "a")

    outcome = _run(
        controller, lambda: controller.enqueue(songs, batch=BatchReport())
    )

    assert outcome.kind is OutcomeKind.PLAYLIST
    assert outcome.count == 1
    assert outcome.truncation is None
    assert _titles(controller) == ["a"]


def test_process_song_playnext_of_a_one_item_playlist_keeps_truncation(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    cut = Truncation(5, next(iter(TruncationReason)))
    stub_loader.results["load_song"] = LoadResult(
        [_song(controller, "a")], False, cut
    )

    outcome = _process(controller, playnext=True)

    assert outcome.kind is OutcomeKind.PLAYLIST
    assert (outcome.count, outcome.truncation) == (1, cut)


def test_ensure_playing_returns_none_when_nothing_starts(controller):
    assert _run(controller, controller.ensure_playing) is None


def test_ensure_playing_returns_the_start_result(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a")

    assert _run(controller, controller.ensure_playing) is True
