"""A request whose songs land with the bot out of voice is refused: the
controller places nothing, writes no backup and starts nothing."""

import asyncio

from musicbot.outcome import OutcomeKind
from musicbot.song import LoadResult
from controller_helpers import (
    _backup,
    _fake_ffmpeg,
    _local_batch,
    _no_idle_timer,
    _run,
    _save_backup,
    _queue,
    _settle,
    _songs,
    _titles,
)
from controller_seam import PlaybackVoiceClient


def _leave_voice(controller):
    controller.guild.voice_client = None


def _old_backup(controller):
    """Leaves a backup of one song, and an empty queue."""
    _queue(controller, "old")
    _save_backup(controller)
    controller.playlist.playque.clear()


def test_enqueue_out_of_voice_is_refused_and_changes_nothing(
    controller, tmp_path
):
    _old_backup(controller)
    _leave_voice(controller)
    (song,) = _songs(controller, "a")

    outcome = _run(controller, lambda: controller.enqueue([song]))

    assert outcome.kind is OutcomeKind.REFUSED_NOT_IN_VOICE
    assert _titles(controller) == []
    assert _backup(controller, tmp_path) == ["old"]


def test_enqueue_next_out_of_voice_is_refused_and_changes_nothing(
    controller, tmp_path
):
    _old_backup(controller)
    _leave_voice(controller)
    (song,) = _songs(controller, "a")

    outcome = _run(controller, lambda: controller.enqueue_next(song))

    assert outcome.kind is OutcomeKind.REFUSED_NOT_IN_VOICE
    assert _titles(controller) == []
    assert _backup(controller, tmp_path) == ["old"]


def test_a_library_pick_out_of_voice_is_refused_and_loads_nothing(
    controller, stub_loader, monkeypatch, tmp_path
):
    _old_backup(controller)
    batch = _local_batch(
        controller, stub_loader, monkeypatch, ["a", "b"], ["a", "b"]
    )
    _leave_voice(controller)
    stub_loader.calls.clear()

    outcome = _run(
        controller, lambda: controller.process_local_tracks(batch, "test")
    )

    assert outcome.kind is OutcomeKind.REFUSED_NOT_IN_VOICE
    assert _titles(controller) == []
    assert stub_loader.calls == []
    assert _backup(controller, tmp_path) == ["old"]


def test_a_voice_client_that_reads_not_connected_still_places_and_starts(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _no_idle_timer(controller, monkeypatch)
    controller.guild.voice_client.connected = False
    (song,) = _songs(controller, "a")

    outcome = _run(controller, lambda: controller.enqueue([song]))

    assert outcome.kind is OutcomeKind.STARTED
    assert _titles(controller) == ["a"]


def test_a_song_whose_load_finishes_after_a_disconnect_is_refused(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _no_idle_timer(controller, monkeypatch)
    _old_backup(controller)
    (song,) = _songs(controller, "slow")
    gate = asyncio.Event()

    async def load_song(track):
        await gate.wait()
        return LoadResult.of_song(song)

    monkeypatch.setattr(stub_loader, "load_song", load_song)

    async def scenario():
        request = asyncio.ensure_future(controller.process_song("slow"))
        await _settle()
        await controller.udisconnect("test")
        _leave_voice(controller)
        gate.set()
        return await request

    outcome = _run(controller, scenario)

    assert outcome.kind is OutcomeKind.REFUSED_NOT_IN_VOICE
    assert _titles(controller) == []
    assert _backup(controller, tmp_path) == ["old"]

    controller.guild.voice_client = PlaybackVoiceClient()
    (retry,) = _songs(controller, "retry")
    again = _run(controller, lambda: controller.enqueue([retry]))

    assert again.kind is OutcomeKind.STARTED
    assert _titles(controller) == ["retry"]
    assert _backup(controller, tmp_path) == ["retry"]


def test_a_library_pick_whose_tail_lands_after_a_disconnect_is_refused(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _no_idle_timer(controller, monkeypatch)
    gate = asyncio.Event()
    batch = _local_batch(
        controller,
        stub_loader,
        monkeypatch,
        ["a", "b", "c"],
        ["a", "b", "c"],
        gate=gate,
    )

    async def scenario():
        pick = asyncio.ensure_future(
            controller.process_local_tracks(batch, "test")
        )
        await _settle()
        assert _titles(controller) == ["a"]
        await controller.udisconnect("test")
        _leave_voice(controller)
        gate.set()
        return await pick

    outcome = _run(controller, scenario)

    assert outcome.kind is OutcomeKind.REFUSED_NOT_IN_VOICE
    assert _titles(controller) == []


def test_a_pick_whose_tail_all_fails_after_a_disconnect_is_refused(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _no_idle_timer(controller, monkeypatch)
    gate = asyncio.Event()
    batch = _local_batch(
        controller,
        stub_loader,
        monkeypatch,
        ["a"],
        ["a", "b", "c"],
        gate=gate,
    )

    async def scenario():
        pick = asyncio.ensure_future(
            controller.process_local_tracks(batch, "test")
        )
        await _settle()
        assert _titles(controller) == ["a"]
        await controller.udisconnect("test")
        _leave_voice(controller)
        gate.set()
        return await pick

    outcome = _run(controller, scenario)

    assert outcome.kind is OutcomeKind.REFUSED_NOT_IN_VOICE
    assert _titles(controller) == []
