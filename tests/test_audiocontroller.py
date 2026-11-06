"""AudioController, built with no Discord connection.

Every test here goes through the `controller` fixture (tests/conftest.py,
built on tests/controller_seam.py): a real AudioController over a stub
musicbot.loader, a fake bot, guild and voice client, with the working
directory set to tmp_path so the controller's backup/ lands there.
"""

import asyncio
import pickle
import sys
import threading
import types

import pytest

from musicbot import playlists
from musicbot.library import LocalTrack
from musicbot.playlist import LoopMode, PauseState
from musicbot.commands.music import Music
from controller_helpers import (
    _backup,
    _backup_path,
    _fake_ffmpeg,
    _preloaded,
    command_ctx,
    _queue,
    _run,
    _run_while_playing,
    _settle,
    _song,
    _songs,
    _save_backup,
    _titles,
    sleep_forever,
)
from controller_seam import (
    PlaybackVoiceClient,
    RecordingPanel,
    StubLoader,
    controller_seam,
)
from db_seam import in_memory_db
from discord_fakes import EXPIRED
from helpers import attribute_names, imported_names, run

MIX = playlists.PlaylistRef("1234", "mix")


async def _start_playing(controller):
    """Starts the queue head on the running loop and lets it settle."""
    controller.bot.loop = asyncio.get_running_loop()
    await controller.ensure_playing()
    await _settle()


def _bounded(controller, scenario):
    """Runs `scenario` under a wall-clock bound, so a regression to
    cycling fails the test instead of hanging the suite."""

    async def bounded():
        await asyncio.wait_for(scenario(), timeout=2)

    _run(controller, bounded)


def _run_playing(controller, scenario, runner=_run):
    """Runs `scenario(controller)` through `runner(controller, main)`
    (`_run`, or `_bounded` for a wall-clock bound), with the queue head
    started first and the loop given time to settle afterwards."""

    async def main():
        await _start_playing(controller)
        await scenario(controller)

    runner(controller, main)


def _played(controller):
    return [
        source.original.url.rsplit("/", 1)[-1]
        for source in controller.guild.voice_client.played
    ]


def test_controller_pickles_its_playlist_into_the_temp_backup_dir(
    controller, tmp_path
):
    _queue(controller, "first track", "second track")

    _save_backup(controller)

    backup = tmp_path / "backup" / f"playlist_{controller.guild.id}.pickle"
    with open(backup, "rb") as f:
        restored = pickle.load(f)
    assert [song.title for song in restored.playque] == [
        "first track",
        "second track",
    ]


def test_controller_starts_at_the_guild_default_volume(controller):
    assert controller.volume == 70


def test_stub_loader_records_preload_calls(controller, stub_loader):
    song = object()
    assert run(stub_loader.preload(song)) is stub_loader.Preload.EXTRACTED
    assert stub_loader.calls == [("preload", (song,))]


def test_seam_leaves_no_stub_and_no_backup_behind(tmp_path, monkeypatch):
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    work = tmp_path / "work"
    before = sys.modules.get("musicbot.loader")
    from config import config

    announce_before = config.ANNOUNCE_DISCONNECT

    with controller_seam(work) as (built, _):
        assert isinstance(sys.modules["musicbot.loader"], StubLoader)
        _queue(built, "probe")
        _save_backup(built)

    after = sys.modules.get("musicbot.loader")
    assert after is before
    assert config.ANNOUNCE_DISCONNECT == announce_before
    assert not isinstance(
        getattr(sys.modules.get("musicbot"), "loader", None), StubLoader
    )
    controller_module = sys.modules.get("musicbot.audiocontroller")
    assert not isinstance(
        getattr(controller_module, "loader", None), StubLoader
    )
    assert not (caller / "backup").exists()
    assert (work / built.pickle_file).exists()


def test_ensure_playing_starts_the_head_of_the_queue_when_idle(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first track", "second track")

    async def scenario():
        await _start_playing(controller)

    _run(controller, scenario)

    played = controller.guild.voice_client.played
    assert [source.original.url for source in played] == [
        "https://stream.invalid/first track"
    ]
    assert _titles(controller) == ["first track", "second track"]
    # ensure_playing() writes no backup: that is enqueue()'s job
    assert not _backup_path(controller, tmp_path).exists()
    # play_song preloads the track it starts, then the queue behind it
    assert _preloaded(stub_loader) == ["first track", "second track"]


def test_ensure_playing_only_preloads_while_something_is_playing(
    controller, stub_loader, tmp_path
):
    controller.guild.voice_client.playing = True
    _queue(controller, "current track", "next track")

    async def scenario():
        await _start_playing(controller)

    _run(controller, scenario)

    assert controller.guild.voice_client.played == []
    assert _titles(controller) == ["current track", "next track"]
    assert not _backup_path(controller, tmp_path).exists()
    assert _preloaded(stub_loader) == ["next track"]


def test_skip_moves_on_to_the_next_track(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def scenario():
        await controller.ensure_playing()
        controller.skip()

    _run(controller, scenario)

    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == ["second", "third"]
    assert _backup(controller, tmp_path) == ["second", "third"]
    # play_song preloads the track it starts, then the queue behind
    # it: "third" once behind "first" (after skip moved past it) and
    # once behind "second"
    assert _preloaded(stub_loader) == ["first", "third", "second", "third"]


def test_stop_ends_playback_and_empties_the_queue_but_keeps_the_backup(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def scenario():
        await controller.ensure_playing()
        controller.stop()

    _run(controller, scenario)

    assert controller.guild.voice_client.stopped == 1
    assert _played(controller) == ["first"]
    assert _titles(controller) == []
    # the snapshot is taken before the queue is cleared: d!restore
    # brings back what was queued at the time of d!stop
    assert _backup(controller, tmp_path) == ["first", "second", "third"]
    # only the track that started; the queue behind it was gone
    # before its preload ran
    assert _preloaded(stub_loader) == ["first"]


def test_after_a_stop_track_changes_refresh_the_backup_again(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")

    async def scenario():
        await controller.ensure_playing()
        controller.stop()
        _queue(controller, "second", "third")
        await controller.ensure_playing()
        controller.skip()

    _run(controller, scenario)

    assert _played(controller) == ["first", "second", "third"]
    assert _backup(controller, tmp_path) == ["third"]


def test_a_stop_while_idle_does_not_hold_back_the_next_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)

    async def scenario():
        controller.stop()
        _queue(controller, "first", "second")
        await controller.ensure_playing()
        controller.skip()

    _run(controller, scenario)

    assert _played(controller) == ["first", "second"]
    assert _backup(controller, tmp_path) == ["second"]


def test_a_late_teardown_callback_does_not_overwrite_the_stop_backup(
    controller, monkeypatch, tmp_path
):
    # discord.py runs the `after` callback later, on its audio thread:
    # here it fires only once a new track has already started
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    voice_client = controller.guild.voice_client

    async def scenario():
        await controller.ensure_playing()
        stale_after = voice_client.detach_after()
        controller.stop()
        _queue(controller, "third", "fourth", "fifth")
        await controller.ensure_playing()
        stale_after(None)
        await _settle()
        frozen = _backup(controller, tmp_path)
        controller.skip()
        await _settle()
        return frozen

    assert _run(controller, scenario) == ["first", "second"]
    # the stale callback neither ended "third" nor used up the latch,
    # so the skip after it - a genuine advance - snapshots
    assert _played(controller) == ["first", "third", "fourth"]
    assert _backup(controller, tmp_path) == ["fourth", "fifth"]


def test_restore_brings_back_the_queue_a_stop_cleared(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        await controller.ensure_playing()
        controller.stop()
        restored = await controller.restore()
        await _settle()
        return restored

    ac = sys.modules[type(controller).__module__]
    assert _run(controller, scenario) is ac.RestoreResult.RESTORED
    assert _played(controller) == ["first", "first"]
    assert _titles(controller) == ["first", "second"]
    assert _backup(controller, tmp_path) == ["first", "second"]
    # "first" each time it starts; "second" by the one sweep that
    # survives: stop() ended the first, and restore() started the next
    assert _preloaded(stub_loader) == ["first", "first", "second"]


def test_restore_with_no_backup_and_an_empty_queue_plays_nothing(
    controller,
):
    async def scenario():
        return await controller.restore()

    ac = sys.modules[type(controller).__module__]
    assert _run(controller, scenario) is ac.RestoreResult.NOTHING_TO_RESTORE
    assert controller.guild.voice_client.played == []


def _restore_while_active(controller, monkeypatch, tmp_path, pause):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    backup = _backup_path(controller, tmp_path)

    async def scenario():
        await controller.ensure_playing()
        _save_backup(controller)
        if pause:
            controller.guild.voice_client.pause()
        before = (controller.playlist, backup.read_bytes())
        result = await controller.restore()
        await _settle()
        return before, result

    return _run(controller, scenario)


@pytest.mark.parametrize("pause", [False, True])
def test_restore_while_active_is_refused_and_changes_nothing(
    controller, monkeypatch, tmp_path, pause
):
    ac = sys.modules[type(controller).__module__]
    (playlist, backup_bytes), result = _restore_while_active(
        controller, monkeypatch, tmp_path, pause
    )

    assert result is ac.RestoreResult.REFUSED_WHILE_ACTIVE
    assert controller.guild.voice_client.disconnected is False
    assert _played(controller) == ["first"]
    assert _titles(controller) == ["first", "second"]
    assert controller.playlist is playlist
    backup = _backup_path(controller, tmp_path)
    assert backup.read_bytes() == backup_bytes


def test_set_volume_sets_the_playing_source_volume(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "next")

    controller.set_volume(30)

    assert controller.volume == 30
    assert controller.guild.voice_client.source.volume == 0.3
    # the queue, its backup and its preloads are left alone
    assert _titles(controller) == ["current", "next"]
    assert not _backup_path(controller, tmp_path).exists()
    assert stub_loader.calls == []


def test_shuffle_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c", "d", "e")

    _run_while_playing(controller, controller.shuffle)

    titles = _titles(controller)
    assert sorted(titles) == ["a", "b", "c", "current", "d", "e"]
    assert _backup(controller, tmp_path) == titles
    assert _preloaded(stub_loader) == titles[1:]


def test_move_reorders_the_queue_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c")

    _run_while_playing(controller, lambda: controller.move(3, 1))

    assert _titles(controller) == ["current", "c", "a", "b"]
    assert _backup(controller, tmp_path) == ["current", "c", "a", "b"]
    assert _preloaded(stub_loader) == ["c", "a", "b"]


def test_remove_drops_a_track_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c")

    removed = _run_while_playing(controller, lambda: controller.remove(2))

    assert removed.title == "b"
    assert _titles(controller) == ["current", "a", "c"]
    assert _backup(controller, tmp_path) == ["current", "a", "c"]
    assert _preloaded(stub_loader) == ["a", "c"]


def test_clear_keeps_the_current_track_refreshes_backup_and_preloads_nothing(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b")

    _run_while_playing(controller, controller.clear)

    assert _titles(controller) == ["current"]
    assert _backup(controller, tmp_path) == ["current"]
    assert stub_loader.calls == []


def test_enqueue_appends_while_playing_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current")
    songs = _songs(controller, "a", "b")

    async def scenario():
        controller.guild.voice_client.playing = True
        await controller.enqueue(songs)

    _run(controller, scenario)

    assert controller.guild.voice_client.played == []
    assert _titles(controller) == ["current", "a", "b"]
    assert _backup(controller, tmp_path) == ["current", "a", "b"]
    assert _preloaded(stub_loader) == ["a", "b"]


def test_enqueue_starts_the_first_track_when_idle(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    songs = _songs(controller, "a", "b")

    async def scenario():
        await controller.enqueue(songs)

    _run(controller, scenario)

    assert _played(controller) == ["a"]
    assert _titles(controller) == ["a", "b"]
    assert _backup(controller, tmp_path) == ["a", "b"]
    # play_song preloads the track it starts, then the queue behind it
    assert _preloaded(stub_loader) == ["a", "b"]


def test_enqueue_no_longer_takes_playnext(controller):
    songs = _songs(controller, "a", "b")

    async def scenario():
        await controller.enqueue(songs, playnext=True)

    with pytest.raises(TypeError):
        _run(controller, scenario)


def test_dispose_cancels_the_timer_and_every_pending_task(
    controller, monkeypatch
):

    async def scenario():
        await controller.timer.start()
        timer_task = controller.timer._task
        pending = []

        controller.add_task(sleep_forever(pending))
        await _settle()
        (pending_task,) = pending

        await controller.dispose()
        await _settle()
        return timer_task, pending_task

    timer_task, pending_task = _run(controller, scenario)

    assert timer_task.cancelled()
    assert pending_task.cancelled()
    assert controller.guild.voice_client.disconnected


class _RecordingLoop:
    """Stands in for bot.loop: records call_soon_threadsafe and runs
    nothing."""

    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))


def _in_thread(fn, *args):
    """Runs fn(*args) on a fresh plain thread, as discord.py's audio
    thread would; returns (thread id, exception or None)."""
    outcome = {}

    def body():
        outcome["thread"] = threading.get_ident()
        try:
            fn(*args)
        except BaseException as e:
            outcome["error"] = e

    thread = threading.Thread(target=body)
    thread.start()
    thread.join()
    return outcome["thread"], outcome.get("error")


def _end_track(voice_client):
    """Ends the playing track as the audio itself running out does:
    the player goes idle, and the track's `after` callback is handed
    back for the test to fire from whichever thread it likes."""
    voice_client.playing = False
    return voice_client.detach_after()


async def _start_and_end_first_track(controller):
    """Starts the queue head on the running loop, then ends it as in
    _end_track(), returning its `after` callback."""
    await _start_playing(controller)
    return _end_track(controller.guild.voice_client)


async def _end_first_track_on_audio_thread(controller, error=None):
    """Starts the queue head on the running loop, fires its `after`
    callback with `error` from a plain thread, as discord.py's audio
    thread would, and lets the loop run the hop."""
    after = await _start_and_end_first_track(controller)
    await _fire_without_raising(after, error)


def _record_threads(controller, monkeypatch):
    """Wraps the queue writes and the backup file write to record the
    thread each one runs on."""
    seen = []

    def recording(name, original):
        def wrapper(*args, **kwargs):
            seen.append((name, threading.get_ident()))
            return original(*args, **kwargs)

        return wrapper

    ac = sys.modules[type(controller).__module__]
    monkeypatch.setattr(ac.pickle, "dump", recording("pickle", ac.pickle.dump))
    # on the class: the playlist itself is pickled, and a wrapper set
    # on the instance would be pickled along with it
    playlist_type = type(controller.playlist)
    for name in ("next", "add_name"):
        monkeypatch.setattr(
            playlist_type,
            name,
            recording(name, getattr(playlist_type, name)),
        )
    return seen


def test_the_after_callback_only_hands_the_track_end_to_the_loop(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        after = await _start_and_end_first_track(controller)
        seen = _record_threads(controller, monkeypatch)
        fake_loop = controller.bot.loop = _RecordingLoop()

        _, error = _in_thread(after, None)

        assert error is None
        assert seen == []
        assert _titles(controller) == ["first", "second"]
        assert not _backup_path(controller, tmp_path).exists()
        ((_, _),) = fake_loop.calls

    _run(controller, scenario)


def test_a_track_end_writes_the_queue_and_backup_only_on_the_loop(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    seen = _record_threads(controller, monkeypatch)

    async def scenario():
        await _end_first_track_on_audio_thread(controller)
        return threading.get_ident()

    loop_thread = _run(controller, scenario)

    assert _played(controller) == ["first", "second"]
    assert {name for name, _ in seen} == {"pickle", "next", "add_name"}
    assert {thread for _, thread in seen} == {loop_thread}


def _track_end_stderr(controller, capsys, monkeypatch, error):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    _run(
        controller,
        lambda: _end_first_track_on_audio_thread(controller, error),
    )
    return capsys.readouterr().err


def test_a_track_ended_by_an_error_logs_the_error_and_advances_once(
    controller, capsys, monkeypatch
):
    err = _track_end_stderr(
        controller, capsys, monkeypatch, RuntimeError("boom")
    )

    expected = (
        f"Playback error in guild 'guild {controller.guild.id}' "
        f"({controller.guild.id}): RuntimeError('boom')"
    )
    assert expected in err
    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == ["second", "third"]


def test_a_track_started_from_idle_logs_it_with_the_guild_label(
    controller, capsys, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")

    _run(controller, lambda: _start_playing(controller))

    guild = controller.guild
    assert (
        "Started playing 'first'" f" in guild 'guild {guild.id}' ({guild.id})"
    ) in capsys.readouterr().out


def test_a_pending_start_is_not_active_until_the_track_plays(
    controller, stub_loader, monkeypatch
):
    seen = []

    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow"
        )
        await _settle()
        seen.append(controller.is_active())
        release.set()
        await _finish(task)
        seen.append(controller.is_active())

    _run(controller, scenario)

    assert seen == [False, True]


def test_a_track_ended_without_an_error_logs_no_playback_error(
    controller, capsys, monkeypatch
):
    err = _track_end_stderr(controller, capsys, monkeypatch, None)

    assert "Playback error" not in err
    assert _played(controller) == ["first", "second"]


def test_next_song_off_the_loop_raises(controller):
    _queue(controller, "first", "second")

    _, error = _in_thread(controller.next_song)

    assert isinstance(error, RuntimeError)
    assert _titles(controller) == ["first", "second"]


def test_a_track_end_from_a_replaced_track_changes_nothing(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    voice_client = controller.guild.voice_client

    async def scenario():
        await _start_playing(controller)
        # the first track's end is held back while its head is started
        # anew, then fires late
        stale_after = _end_track(voice_client)
        await controller.ensure_playing()
        await _settle()
        seen = _record_threads(controller, monkeypatch)
        _in_thread(stale_after, None)
        await _settle()
        return seen

    seen = _run(controller, scenario)

    assert seen == []
    assert _titles(controller) == ["first", "second"]
    assert voice_client.stopped == 0
    assert voice_client.is_playing()
    assert _played(controller) == ["first", "first"]
    assert not _backup_path(controller, tmp_path).exists()


def test_a_track_end_from_a_replaced_track_drops_its_pending_pick(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def scenario():
        await _start_playing(controller)
        # a skip picks "second" and stops "first"; before its hop
        # runs, the queue head - "second" - is started anew
        controller.skip()
        await controller.ensure_playing()
        await _settle()
        after = _end_track(controller.guild.voice_client)
        _in_thread(after, None)

    _run(controller, scenario)

    assert _played(controller) == ["first", "second", "third"]


def test_a_stop_skips_the_backup_after_the_hop(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    backup = tmp_path / "backup" / "playlist_1234.pickle"

    async def scenario():
        await controller.ensure_playing()
        controller.stop()
        # stop() snapshots the queue itself; what follows is the hop
        snapshot = backup.read_bytes() if backup.exists() else None
        seen = _record_threads(controller, monkeypatch)
        await _settle()
        return snapshot, seen

    snapshot, seen = _run(controller, scenario)

    # the hop ran - it advanced the emptied queue - and wrote no backup
    assert "next" in [name for name, _ in seen]
    after = backup.read_bytes() if backup.exists() else None
    assert after == snapshot
    assert controller.guild.voice_client.stopped == 1


def test_the_idle_timer_starts_after_the_last_track_ends(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def scenario():
        after = await _start_and_end_first_track(controller)
        assert controller.timer._task is None
        _in_thread(after, None)
        await _settle()
        return controller.timer._task

    timer_task = _run(controller, scenario)

    assert timer_task is not None
    assert _titles(controller) == []


def test_the_after_callback_drops_the_track_end_once_the_loop_is_closed(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    # run() closes its loop on the way out, as bot shutdown does
    after = run(_start_and_end_first_track(controller))
    assert controller.bot.loop.is_closed()

    _, error = _in_thread(after, None)

    assert error is None
    assert _titles(controller) == ["first", "second"]
    assert _played(controller) == ["first"]


SAVED_URL = "https://example.invalid/track"


def _queue_saved_playlist_song(controller, from_saved_playlist=True):
    """Queues one song behind a playing track, stored as "stale title"
    in a saved playlist and loaded as "fresh title"; lets the preload
    run, and returns the saved playlist's entries and the queue."""

    async def queue_and_preload(ac, factory):
        await controller.enqueue(
            [
                ac.Song(
                    ac.SiteTypes.YT_DLP,
                    SAVED_URL,
                    title="fresh title",
                    saved_entry=(
                        ac.playlists.SavedEntry(
                            MIX,
                            SAVED_URL,
                        )
                        if from_saved_playlist
                        else None
                    ),
                )
            ]
        )
        await _settle(controller)
        contents = await ac.playlists.get(factory, MIX)
        return contents.entries, _titles(controller)

    return _with_saved_playlist_db(controller, queue_and_preload)


def _entry(controller, title):
    ac = sys.modules[type(controller).__module__]
    return ac.playlists.PlaylistEntry(SAVED_URL, title)


@pytest.mark.parametrize(
    "from_saved_playlist, stored_title",
    [(True, "fresh title"), (False, "stale title")],
)
def test_preloading_a_saved_playlist_song_refreshes_its_stored_title(
    controller, from_saved_playlist, stored_title
):
    entries, _ = _queue_saved_playlist_song(controller, from_saved_playlist)
    assert entries == [_entry(controller, stored_title)]


@pytest.mark.parametrize(
    "outcome, queue",
    [
        ("CURRENT", ["playing", "fresh title"]),
        # a failed preload leaves the song queued
        ("FAILED", ["playing", "fresh title"]),
    ],
)
def test_a_preload_that_extracted_nothing_leaves_the_stored_title_stale(
    controller, stub_loader, outcome, queue
):
    stub_loader.results["preload"] = stub_loader.Preload[outcome]
    entries, titles = _queue_saved_playlist_song(controller)
    assert entries == [_entry(controller, "stale title")]
    assert titles == queue


def test_a_failing_title_refresh_still_counts_as_a_successful_preload(
    controller, monkeypatch
):
    ac = sys.modules[type(controller).__module__]

    async def set_title(*args):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(ac.playlists, "set_title", set_title)
    entries, titles = _queue_saved_playlist_song(controller)
    assert entries == [_entry(controller, "stale title")]
    assert titles == ["playing", "fresh title"]


# an external disconnect is not a track end (#202)


async def _fire_without_raising(after, error=None):
    """Fires the after= callback from a plain thread and lets the loop
    run the hop; fails if either the thread or the loop raised."""
    loop = asyncio.get_running_loop()
    raised = []
    loop.set_exception_handler(lambda _loop, context: raised.append(context))
    _, thread_error = _in_thread(after, error)
    await _settle()
    assert thread_error is None
    assert raised == []


async def _start_then_disconnect(controller):
    """Starts the queue head, then writes its backup, then drops the
    guild's voice client as discord.py's cleanup does and fires the
    track's after= callback."""
    controller.bot.loop = asyncio.get_running_loop()
    await controller.ensure_playing()
    _save_backup(controller)
    await _settle()
    after = _end_track(controller.guild.voice_client)
    controller.guild.voice_client = None
    await _fire_without_raising(after)


def _fresh_voice_client(controller):
    controller.guild.voice_client = PlaybackVoiceClient()


def test_an_external_disconnect_leaves_the_queue_history_and_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    old = controller.guild.voice_client

    _run(controller, lambda: _start_then_disconnect(controller))

    assert _titles(controller) == ["first", "second"]
    assert _history(controller) == ["first"]
    assert _backup(controller, tmp_path) == ["first", "second"]
    assert len(old.played) == 1


# --- d!history: a song is recorded when it starts ---------------------


def _history(controller):
    return list(controller.playlist.trackname_history)


async def _nothing(controller):
    pass


async def _skip(controller):
    controller.skip()


def test_a_skip_in_the_middle_of_the_queue_records_the_skipped_song(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")
    _run_playing(controller, _skip)

    assert _history(controller) == ["first", "second"]


def test_skipping_the_last_song_records_it(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")
    _run_playing(controller, _skip)

    assert _history(controller) == ["only"]


def test_a_skip_with_loop_all_records_the_skipped_song(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")
    ac = sys.modules[type(controller).__module__]
    controller.playlist.loop = ac.LoopMode.ALL
    _run_playing(controller, _skip)

    assert _history(controller) == ["first", "second"]


def test_two_skips_before_the_end_hops_back_record_each_started_song_once(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third", "fourth")

    async def scenario(c):
        c.skip()
        c.skip()

    _run_playing(controller, scenario)

    assert _history(controller) == ["first", "third"]


def _announced(controller):
    return [song.title for song in controller.panel.announced]


def test_a_stop_right_after_a_skip_plays_nothing_more(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")
    backup = _backup_path(controller, tmp_path)
    snapshots = []

    async def scenario(c):
        c.skip()
        c.stop()
        snapshots.append(backup.read_bytes() if backup.exists() else None)

    _run_playing(controller, scenario)

    assert _played(controller) == ["first"]
    assert _announced(controller) == ["first"]
    assert _titles(controller) == []
    assert controller.is_idle()
    after = backup.read_bytes() if backup.exists() else None
    assert after == snapshots[0]


def test_a_disconnect_right_after_a_skip_starts_nothing(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def scenario(c):
        c.skip()
        await c.udisconnect("test")

    _run_playing(controller, scenario)

    assert _played(controller) == ["first"]
    assert _announced(controller) == ["first"]
    assert controller.is_idle()
    assert not controller.guild.voice_client.is_playing()


def test_a_stop_right_after_prev_starts_nothing_new(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario(c):
        c.skip()
        await _settle()
        c.prev_song()
        c.stop()

    _run_playing(controller, scenario)

    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == []
    assert controller.is_idle()


def test_two_quick_skips_play_the_third_song_never_the_second(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def scenario(c):
        c.skip()
        c.skip()

    _run_playing(controller, scenario)

    assert _played(controller) == ["first", "third"]
    assert _announced(controller) == ["first", "third"]


def test_a_skip_after_a_kick_plays_the_current_queue(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def scenario(c):
        c.skip()
        await c.udisconnect("removed from voice channel")
        await _settle()
        _queue(c, "old", "fresh")
        c.skip()
        await _settle()

    _run_playing(controller, scenario)

    assert "second" not in _played(controller)
    assert _played(controller)[-1] == "fresh"


def test_prev_while_playing_records_the_outgoing_song(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario(c):
        c.skip()
        await _settle()
        c.prev_song()

    _run_playing(controller, scenario)

    assert _history(controller) == ["first", "second", "first"]


def test_a_stop_while_playing_leaves_the_stopped_song_in_the_history(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario(c):
        c.stop()

    _run_playing(controller, scenario)

    assert _history(controller) == ["first"]


def test_a_disconnect_while_playing_leaves_the_song_in_the_history(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario(c):
        await c.udisconnect("test")

    _run_playing(controller, scenario)

    assert _history(controller) == ["first"]


def test_a_song_that_fails_to_preload_is_not_recorded(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "broken")
    stub_loader.results["preload"] = stub_loader.Preload.FAILED

    _run_playing(controller, _nothing)

    assert _history(controller) == []
    assert _played(controller) == []


def test_a_song_without_a_direct_url_is_not_recorded(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "no url")
    controller.playlist[0].url = None

    _run_playing(controller, _nothing)

    assert _history(controller) == []
    assert _played(controller) == []


def test_a_play_the_voice_client_refuses_is_not_recorded(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "refused")
    controller.guild.voice_client.playing = True

    async def scenario():
        await controller.play_song(controller.playlist[0])

    _run(controller, scenario)
    assert _history(controller) == []
    # the on_play_failed teardown snapshots the queue, then empties it
    assert _backup(controller, tmp_path) == ["refused"]
    assert _titles(controller) == []


def test_a_source_that_cannot_be_built_goes_the_play_failed_path(
    controller, monkeypatch, tmp_path
):
    import discord

    _fake_ffmpeg(monkeypatch)
    _queue(controller, "no ffmpeg")

    def refuse(song):
        raise discord.ClientException("ffmpeg was not found.")

    monkeypatch.setattr(controller.playback, "_make_source", refuse)

    async def scenario():
        await controller.play_song(controller.playlist[0])

    _run(controller, scenario)
    assert _history(controller) == []
    assert _backup(controller, tmp_path) == ["no ffmpeg"]
    assert _titles(controller) == []


def test_a_song_that_finishes_on_its_own_is_recorded_once(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")

    _run(controller, lambda: _end_first_track_on_audio_thread(controller))

    assert _history(controller) == ["first"]


def test_the_song_playing_now_is_in_the_history(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    _run_playing(controller, _nothing)

    assert controller.track_history().splitlines()[-1] == "first"


def test_each_repeat_of_a_single_track_loop_is_recorded(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")
    ac = sys.modules[type(controller).__module__]
    controller.playlist.loop = ac.LoopMode.SINGLE

    async def scenario(c):
        for _ in range(2):
            _in_thread(_end_track(c.guild.voice_client), None)
            await _settle()

    _run_playing(controller, scenario)

    assert _history(controller) == ["first", "first", "first"]


def test_restore_after_an_external_disconnect_starts_the_cut_off_song(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")

    async def scenario():
        await _start_then_disconnect(controller)
        assert _titles(controller) == ["first"]
        assert _backup(controller, tmp_path) == ["first"]
        await controller.udisconnect("test")
        _fresh_voice_client(controller)
        restored = await controller.restore()
        await _settle()
        return restored

    ac = sys.modules[type(controller).__module__]
    assert _run(controller, scenario) is ac.RestoreResult.RESTORED
    assert _played(controller) == ["first"]


def test_rejoining_before_the_timeout_plays_the_cut_off_song_again(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        await _start_then_disconnect(controller)
        _fresh_voice_client(controller)
        await controller.ensure_playing()

    _run(controller, scenario)

    assert _played(controller) == ["first"]
    assert _titles(controller) == ["first", "second"]


def test_a_track_end_while_reconnecting_still_advances(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        await _start_playing(controller)
        voice_client = controller.guild.voice_client
        voice_client.connected = False
        _in_thread(_end_track(voice_client), None)

    _run(controller, scenario)

    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == ["second"]


def test_the_teardown_hop_after_a_disconnect_keeps_the_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    old = controller.guild.voice_client

    async def scenario():
        after = await _start_and_end_first_track(controller)
        await controller.udisconnect("test")
        controller.guild.voice_client = None
        await _fire_without_raising(after)

    _run(controller, scenario)

    assert _backup(controller, tmp_path) == ["first", "second"]
    assert len(old.played) == 1


def _gated_preload(stub_loader, title):
    """Makes preload() of `title` wait on one future per call, in call
    order; returns those futures. Other titles succeed at once."""
    futures = []
    original = stub_loader.preload

    async def preload(song):
        if song.title != title:
            return await original(song)
        stub_loader.calls.append(("preload", (song,)))
        future = asyncio.get_running_loop().create_future()
        futures.append(future)
        return await future

    stub_loader.preload = preload
    return futures


def test_a_background_preload_failing_after_the_song_became_head_skips_only_it(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        # the queue preload of "b" is pending; skipping makes "b" the
        # head, and the player's own preload of it pends too
        controller.skip()
        await _settle()
        assert len(futures) == 2
        futures[0].set_result(stub_loader.Preload.FAILED)
        await _settle()
        futures[1].set_result(stub_loader.Preload.FAILED)

    _run(controller, scenario)

    assert _played(controller) == ["a", "c"]


def test_every_preload_failing_leaves_a_long_queue_whole_and_silent(
    controller, stub_loader
):
    _queue(controller, "current")
    songs = _songs(controller, *[f"s{i}" for i in range(100)])
    stub_loader.results["preload"] = stub_loader.Preload.FAILED

    async def scenario():
        controller.guild.voice_client.playing = True
        await controller.enqueue(songs)

    _run(controller, scenario)

    assert len(_titles(controller)) == 101
    assert controller.panel.skipped == []


def test_a_song_failing_in_the_background_plays_when_its_own_preload_works(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        futures[0].set_result(stub_loader.Preload.FAILED)
        await _settle()
        controller.skip()
        await _settle()
        futures[1].set_result(stub_loader.Preload.EXTRACTED)

    _run(controller, scenario)

    assert _played(controller) == ["a", "b"]
    assert controller.panel.skipped == []


def test_a_song_failing_in_the_background_and_at_the_head_is_reported_once(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        futures[0].set_result(stub_loader.Preload.FAILED)
        await _settle()
        controller.skip()
        await _settle()
        futures[1].set_result(stub_loader.Preload.FAILED)

    _run(controller, scenario)

    assert _played(controller) == ["a", "c"]
    assert [song.title for song in controller.panel.skipped] == ["b"]


def test_stop_ends_a_sweep_even_when_songs_are_queued_again(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    songs = _queue(controller, "a", "b", "c")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        controller.stop()
        await _settle()
        controller.playlist.add(songs[2])
        futures[0].set_result(stub_loader.Preload.EXTRACTED)

    _run(controller, scenario)

    assert "c" not in _preloaded(stub_loader)


def test_a_new_sweep_supersedes_the_running_one(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")
    extra = _songs(controller, "d")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        await controller.enqueue(extra)
        await _settle()
        for future in futures:
            future.set_result(stub_loader.Preload.EXTRACTED)

    _run(controller, scenario)

    assert _preloaded(stub_loader).count("c") == 1


def test_teardown_ends_a_sweep_awaiting_a_preload(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    songs = _queue(controller, "a", "b", "c")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        await controller.udisconnect("test")
        await _settle()
        controller.playlist.add(songs[2])
        futures[0].set_result(stub_loader.Preload.EXTRACTED)

    _run(controller, scenario)

    assert "c" not in _preloaded(stub_loader)


def _shared_gated_preload(stub_loader, title):
    """Like loader.preload for `title`: one in-flight future per song
    shared by its callers, resolved as failed when the caller that
    started it is cancelled. Returns the in-flight futures by song."""
    inflight = {}
    original = stub_loader.preload

    async def preload(song):
        if song.title != title:
            return await original(song)
        future = inflight.get(song)
        if future is not None:
            return await asyncio.shield(future)
        stub_loader.calls.append(("preload", (song,)))
        future = inflight[song] = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if not future.done():
                future.set_result(stub_loader.Preload.FAILED)
            raise

    stub_loader.preload = preload
    return inflight


def test_ending_a_sweep_does_not_fail_the_head_play_sharing_its_preload(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b")
    extra = _songs(controller, "c")

    async def scenario():
        inflight = _shared_gated_preload(stub_loader, "b")
        await _start_playing(controller)
        controller.skip()
        await _settle()
        # a new sweep supersedes the one awaiting "b"
        await controller.enqueue(extra)
        await _settle()
        for future in inflight.values():
            if not future.done():
                future.set_result(stub_loader.Preload.EXTRACTED)

    _run(controller, scenario)

    assert _played(controller) == ["a", "b"]
    assert controller.panel.skipped == []


def test_a_sweep_skips_a_song_removed_while_it_awaited_an_earlier_one(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    songs = _queue(controller, "a", "b", "c", "d")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        controller.playlist.playque.remove(songs[2])
        futures[0].set_result(stub_loader.Preload.EXTRACTED)

    _run(controller, scenario)

    assert "c" not in _preloaded(stub_loader)
    assert "d" in _preloaded(stub_loader)


def _idle_timer_that_fires_at_once(controller, monkeypatch):
    """Lets the idle timer fire straight away, and returns the list a
    voice client disconnect() appends to: one entry per disconnect."""
    from config import config

    monkeypatch.setattr(config, "VC_TIMEOUT", 0)
    controller.bot.sessions.settings(controller.guild).vc_timeout = True
    voice_client = controller.guild.voice_client
    disconnects = []
    original = voice_client.disconnect

    async def disconnect(*, force=False):
        disconnects.append(force)
        await original(force=force)

    voice_client.disconnect = disconnect
    return disconnects


def _during_disconnect(controller, during, yields=1):
    """Makes the voice client's disconnect() run `during()` first, then
    yield `yields` times before disconnecting."""
    voice_client = controller.guild.voice_client
    original = voice_client.disconnect

    async def disconnect(*, force=False):
        during()
        for _ in range(yields):
            await asyncio.sleep(0)
        await original(force=force)

    voice_client.disconnect = disconnect


def test_a_track_end_hop_during_a_disconnect_starts_no_idle_timer(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def scenario():
        await _start_playing(controller)
        disconnects = _idle_timer_that_fires_at_once(controller, monkeypatch)
        after = controller.guild.voice_client.detach_after()
        _during_disconnect(
            controller,
            lambda: after(None),
            yields=3,  # the hop and the task it schedules run first
        )
        await controller.udisconnect("test")
        await _settle()
        return disconnects

    # a timer started by the hop would disconnect a second time
    assert len(_run(controller, scenario)) == 1


def test_a_disposed_controller_leaves_no_timer_running(
    controller, monkeypatch
):
    async def scenario():
        disconnects = _idle_timer_that_fires_at_once(controller, monkeypatch)
        _during_disconnect(
            controller, lambda: controller.add_task(controller.timer.start())
        )
        await controller.dispose()
        await _settle()
        return disconnects

    assert len(_run(controller, scenario)) == 1


def test_a_track_end_after_a_plain_stop_starts_the_idle_timer(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def scenario():
        await _start_playing(controller)
        _idle_timer_that_fires_at_once(controller, monkeypatch)
        controller.stop()
        await _settle()
        return controller.guild.voice_client.disconnected

    assert _run(controller, scenario)


def test_a_track_end_after_the_idle_timer_expired_starts_it_again(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def scenario():
        await _start_playing(controller)
        disconnects = _idle_timer_that_fires_at_once(controller, monkeypatch)
        after = controller.guild.voice_client.detach_after()
        controller.guild.voice_client.playing = False
        await controller.timer.start()
        await _settle()
        assert len(disconnects) == 1
        after(None)
        await _settle()
        return disconnects

    assert len(_run(controller, scenario)) == 2


def test_volume_down_lowers_the_volume_to_zero_from_five(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    controller.set_volume(5)

    controller.volume_down()

    assert controller.volume == 0


def test_the_snapshot_is_inactive_with_nothing_playing_or_paused(controller):
    _queue(controller, "current")

    snapshot = controller.panel_snapshot()

    assert not snapshot.active
    assert snapshot.current_song is None


def test_the_snapshot_of_a_playing_track(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True

    snapshot = controller.panel_snapshot()

    assert snapshot.active and not snapshot.paused
    assert snapshot.current_song is controller.playlist[0]
    assert snapshot.volume == 70


def test_the_snapshot_of_a_paused_track(controller):
    _queue(controller, "current")
    controller.guild.voice_client.paused = True

    snapshot = controller.panel_snapshot()

    assert snapshot.active and snapshot.paused


def test_the_snapshot_has_next_only_with_a_song_behind_the_current_one(
    controller,
):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    assert not controller.panel_snapshot().has_next

    _queue(controller, "behind")

    assert controller.panel_snapshot().has_next


def test_the_snapshot_has_previous_only_with_history(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    assert not controller.panel_snapshot().has_previous

    controller.playlist.playhistory.append(_song(controller, "old"))

    assert controller.panel_snapshot().has_previous


def test_current_song_is_nothing_when_active_with_an_empty_queue(controller):
    controller.guild.voice_client.playing = True

    assert controller.current_song is None
    assert controller.panel_snapshot().current_song is None


def test_the_snapshot_reports_an_empty_queue(controller):
    controller.guild.voice_client.playing = True

    assert controller.panel_snapshot().queue_empty
    _queue(controller, "current")
    assert not controller.panel_snapshot().queue_empty


def test_the_snapshot_follows_the_loop_mode_and_the_volume(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    controller.loop()
    controller.set_volume(30)

    snapshot = controller.panel_snapshot()

    assert snapshot.loop_mode == controller.playlist.loop
    assert snapshot.loop_mode != "off"
    assert snapshot.volume == 30


def test_the_queue_embed_is_the_playlists_queue_embed(controller):
    _queue(controller, "current")

    assert controller.queue_embed().to_dict() == (
        controller.playlist.queue_embed().to_dict()
    )


def _rewriting_preload(stub_loader, normalised):
    """Makes every extraction rewrite the song's web page URL, as
    yt-dlp does when it normalises it or resolves a Spotify entry."""

    async def preload(song):
        song.webpage_url = normalised
        return stub_loader.Preload.EXTRACTED

    stub_loader.preload = preload


def _with_saved_playlist_db(controller, run_in_db):
    """Queues a playing track and a "stale title" saved playlist `MIX`,
    awaits `run_in_db(ac, factory)` with the module and the DB session
    factory, and returns what it returns."""
    ac = sys.modules[type(controller).__module__]
    _queue(controller, "playing")

    async def scenario():
        async with in_memory_db(sys.modules["musicbot.settings"]) as factory:
            await ac.playlists.save(
                factory,
                MIX,
                [ac.playlists.PlaylistEntry(SAVED_URL, "stale title")],
            )
            controller.bot.DbSession = factory
            controller.guild.voice_client.playing = True
            return await run_in_db(ac, factory)

    return _run(controller, scenario)


def _stored_titles(ac, factory):
    async def get():
        contents = await ac.playlists.get(factory, MIX)
        return [entry.title for entry in contents.entries]

    return get()


def test_a_song_loaded_from_a_saved_playlist_keeps_its_title_refresh(
    controller, stub_loader
):
    _rewriting_preload(stub_loader, "https://example.invalid/normalised")

    async def load_and_preload(ac, factory):
        ctx, _ = command_ctx(
            controller, db_session=factory, announce_songs=True
        )
        await Music._playlist_load.callback(Music(None), ctx, "mix")
        await _settle(controller)
        song = controller.playlist.playque[-1]
        titles = []
        for n in range(2):
            # the stream URL expired: the song is extracted again
            song.title = f"fresh title {n}"
            assert await controller._preload(song)
            titles.append(await _stored_titles(ac, factory))
        return titles

    assert _with_saved_playlist_db(controller, load_and_preload) == [
        ["fresh title 0"],
        ["fresh title 1"],
    ]


# --- a pending start: the gap between committing to a track and
# handing it to the voice client (#193, #203, #218) ---


def _pending_start(controller, stub_loader, monkeypatch, *titles, result=None):
    """Queues `titles` and starts the first one with its preload held.
    Returns (release event, the start's task, its song)."""
    _fake_ffmpeg(monkeypatch)
    _queue(controller, *titles)
    slow = controller.playlist[0]
    release = stub_loader.hold(slow, result)
    task = asyncio.ensure_future(controller.ensure_playing())
    return release, task, slow


def _local_batch(controller, stub_loader, monkeypatch, loadable, picked):
    """Stubs the loader to load the `loadable` titles (None for any other
    uri) and returns the local batch of the `picked` titles."""
    by_uri = {
        f"/music/{s.title}.mp3": s for s in _songs(controller, *loadable)
    }

    async def load_local_songs(uris):
        return [by_uri.get(u) for u in uris]

    monkeypatch.setattr(stub_loader, "load_local_songs", load_local_songs)
    return [LocalTrack(f"/music/{t}.mp3", f"Name {t}") for t in picked]


def _without_url(controller, title):
    song = _song(controller, title)
    song.url = None
    return song


def _old_track(controller):
    song = _song(controller, "old")
    controller.playlist.playhistory.append(song)
    return song


async def _finish(task):
    await _settle()
    await task
    await _settle()


def test_queueing_during_a_pending_start_starts_nothing_new(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow", "next"
        )
        await _settle()
        await controller.enqueue(_songs(controller, "added"))
        release.set()
        await _finish(task)

    _run(controller, scenario)

    assert _played(controller) == ["slow"]
    assert controller.guild.voice_client.disconnected is False
    assert _titles(controller) == ["slow", "next", "added"]


def test_the_inactivity_timeout_during_a_pending_start_does_not_disconnect(
    controller, stub_loader, monkeypatch
):
    import types

    sett = controller.bot.sessions.settings(controller.guild)
    sett.vc_timeout = True
    controller.guild.voice_client.channel.members = [
        types.SimpleNamespace(bot=False)
    ]

    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow"
        )
        await _settle()
        await controller.timeout_handler()
        release.set()
        await _finish(task)

    _run(controller, scenario)

    assert controller.guild.voice_client.disconnected is False
    assert _played(controller) == ["slow"]


def test_the_inactivity_timeout_of_a_paused_bot_disconnects(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")
    controller.bot.sessions.settings(controller.guild).vc_timeout = True
    voice_client = controller.guild.voice_client
    voice_client.channel.members = [types.SimpleNamespace(bot=False)]

    async def scenario():
        await _start_playing(controller)
        controller.pause()
        assert voice_client.is_paused()
        await controller.timeout_handler()

    _run(controller, scenario)

    assert voice_client.disconnected is True


def test_a_local_batch_during_a_pending_start_starts_nothing_new(
    controller, stub_loader, monkeypatch
):
    batch = _local_batch(
        controller, stub_loader, monkeypatch, ("a", "b"), ["a", "b"]
    )

    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow"
        )
        await _settle()
        await controller.process_local_tracks(batch, "test")
        release.set()
        await _finish(task)

    _run(controller, scenario)

    assert _played(controller) == ["slow"]
    assert controller.guild.voice_client.disconnected is False
    assert _titles(controller) == ["slow", "a", "b"]


def test_restore_during_a_pending_start_is_refused(
    controller, stub_loader, monkeypatch
):
    ac = sys.modules[type(controller).__module__]

    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow", "next"
        )
        await _settle()
        result = await controller.restore()
        release.set()
        await _finish(task)
        return result

    result = _run(controller, scenario)

    assert result is ac.RestoreResult.REFUSED_WHILE_ACTIVE
    assert _played(controller) == ["slow"]
    assert controller.guild.voice_client.disconnected is False


def test_prev_during_a_pending_start_plays_the_previous_track_only(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow", "next"
        )
        await _settle()
        _old_track(controller)
        assert controller.prev_song()
        await _settle()
        before = len(stub_loader.calls)
        release.set()
        await _finish(task)
        await _settle()
        return _preloaded(stub_loader, since=before)

    later = _run(controller, scenario)

    assert _played(controller) == ["old"]
    assert "slow" not in later
    assert controller.guild.voice_client.disconnected is False
    assert controller.panel.skipped == []


def test_next_during_a_pending_start_skips_it_and_plays_the_one_after(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow", "next", "third"
        )
        await _settle()
        controller.next_song()
        await _settle()
        before = len(stub_loader.calls)
        release.set()
        await _finish(task)
        await _settle()
        return _preloaded(stub_loader, since=before)

    later = _run(controller, scenario)

    assert _played(controller) == ["next"]
    assert later == []
    assert controller.guild.voice_client.disconnected is False
    assert _titles(controller) == ["next", "third"]
    assert controller.panel.skipped == []


def _preloads_after_release(controller, stub_loader, monkeypatch, act):
    """Holds a start's preload, runs `act`, releases the preload and
    returns the titles preloaded after the release."""

    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow", "next"
        )
        await _settle()
        await act()
        before = len(stub_loader.calls)
        release.set()
        await _finish(task)
        return _preloaded(stub_loader, since=before)

    return _run(controller, scenario)


def test_a_stop_during_a_pending_start_leaves_nothing_playing(
    controller, stub_loader, monkeypatch
):
    async def act():
        controller.stop()

    later = _preloads_after_release(controller, stub_loader, monkeypatch, act)

    assert _played(controller) == []
    assert later == []
    assert controller.panel.skipped == []
    assert controller.guild.voice_client.disconnected is False


def test_a_disconnect_during_a_pending_start_leaves_nothing_playing(
    controller, stub_loader, monkeypatch
):
    async def act():
        await controller.udisconnect("test")

    later = _preloads_after_release(controller, stub_loader, monkeypatch, act)

    assert _played(controller) == []
    assert later == []
    assert controller.panel.skipped == []
    assert controller.guild.voice_client.disconnected is True


def test_losing_the_voice_client_during_a_pending_start_plays_nothing(
    controller, stub_loader, monkeypatch
):
    voice_client = controller.guild.voice_client

    async def act():
        controller.guild.voice_client = None

    later = _preloads_after_release(controller, stub_loader, monkeypatch, act)

    assert voice_client.played == []
    assert later == []
    assert controller.panel.skipped == []
    # the abandoned start no longer holds the bot busy
    controller.guild.voice_client = voice_client
    assert controller.is_idle()


def test_a_stale_failed_preload_skips_nothing_and_moves_nothing(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller,
            stub_loader,
            monkeypatch,
            "slow",
            "next",
            result=stub_loader.Preload.FAILED,
        )
        await _settle()
        _old_track(controller)
        controller.prev_song()
        release.set()
        await _finish(task)

    _run(controller, scenario)
    assert _played(controller) == ["old"]
    assert controller.panel.skipped == []
    # the queue preload behind "old" met a FAILED result for "slow" and
    # left it queued; "next" was not started or dropped by the stale one
    assert _titles(controller) == ["old", "slow", "next"]


def test_a_current_failed_preload_skips_to_the_next_track(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller,
            stub_loader,
            monkeypatch,
            "slow",
            "next",
            result=stub_loader.Preload.FAILED,
        )
        await _settle()
        release.set()
        await _finish(task)

    _run(controller, scenario)

    assert len(controller.panel.skipped) == 1
    assert _played(controller) == ["next"]


def test_a_current_failed_preload_with_nothing_after_it_ends_the_start(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller,
            stub_loader,
            monkeypatch,
            "slow",
            result=stub_loader.Preload.FAILED,
        )
        await _settle()
        release.set()
        await _finish(task)
        await controller.enqueue(_songs(controller, "later"))

    _run(controller, scenario)

    assert _played(controller) == ["later"]


def test_a_stale_start_without_a_url_moves_nothing(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        controller.playlist.add(_without_url(controller, "slow"))
        _queue(controller, "next")
        _fake_ffmpeg(monkeypatch)
        release = stub_loader.hold(controller.playlist[0])
        task = asyncio.ensure_future(controller.ensure_playing())
        await _settle()
        _old_track(controller)
        controller.prev_song()
        release.set()
        await _finish(task)

    _run(controller, scenario)

    assert _played(controller) == ["old"]
    assert _titles(controller) == ["old", "slow", "next"]


def test_a_current_start_without_a_url_ends_the_start(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        controller.playlist.add(_without_url(controller, "slow"))
        _fake_ffmpeg(monkeypatch)
        await controller.ensure_playing()
        await controller.enqueue(_songs(controller, "later"))

    _run(controller, scenario)

    assert _played(controller) == ["later"]


def test_a_superseded_start_dropping_out_leaves_the_newer_one_pending(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow", "next"
        )
        await _settle()
        old = _old_track(controller)
        newer = stub_loader.hold(old)
        controller.prev_song()
        await _settle()
        before = len(stub_loader.calls)
        release.set()
        await _finish(task)
        later = _preloaded(stub_loader, since=before)
        await controller.enqueue(_songs(controller, "added"))
        await _settle()
        assert _played(controller) == []
        newer.set()
        await _settle()
        return later

    later = _run(controller, scenario)

    assert _played(controller) == ["old"]
    assert "slow" not in later
    assert controller.guild.voice_client.disconnected is False


# An idle controller with an empty queue must leave the last real backup
# alone: it is what d!restore reloads.


def test_a_second_disconnect_leaves_the_backup_holding_the_queue(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        await controller.ensure_playing()
        await controller.udisconnect("d!disconnect")
        controller.guild.voice_client = None
        await controller.udisconnect("voice state")

    _run(controller, scenario)

    assert _backup(controller, tmp_path) == ["first", "second"]


def test_a_disconnect_after_a_stop_leaves_the_stop_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        await controller.ensure_playing()
        controller.stop()
        await controller.udisconnect("inactivity timeout")

    _run(controller, scenario)

    assert _backup(controller, tmp_path) == ["first", "second"]


def test_disconnecting_a_new_idle_controller_keeps_an_existing_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "kept", "also kept")

    async def scenario():
        await controller.ensure_playing()
        controller.stop()
        restarted = type(controller)(
            controller.bot, controller.guild, controller.panel
        )
        await restarted.udisconnect("shutdown")

    _run(controller, scenario)

    assert _backup(controller, tmp_path) == ["kept", "also kept"]


@pytest.mark.parametrize("intent", ["pause", "clear"])
def test_pause_and_clear_on_an_idle_empty_queue_keep_the_backup(
    controller, monkeypatch, tmp_path, intent
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "kept")

    async def scenario():
        await controller.ensure_playing()
        controller.stop()

    _run(controller, scenario)
    assert _titles(controller) == []

    getattr(controller, intent)()

    assert _backup(controller, tmp_path) == ["kept"]


def test_a_queue_that_plays_to_its_end_leaves_an_empty_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def scenario():
        after = await _start_and_end_first_track(controller)
        after(None)

    _run(controller, scenario)

    assert _backup(controller, tmp_path) == []


def test_disconnecting_while_playing_leaves_the_backup_holding_the_queue(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def scenario():
        await controller.ensure_playing()
        await controller.udisconnect("d!disconnect")

    _run(controller, scenario)

    assert _backup(controller, tmp_path) == ["first", "second"]


def _title_less_song(controller):
    song = _song(controller, "untitled")
    song.title = None
    controller.playlist.add(song)
    return song


def test_a_title_less_song_is_in_the_history_by_its_webpage_url(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    song = _title_less_song(controller)

    _run_playing(controller, _nothing)

    assert song.webpage_url in controller.track_history()


def test_a_title_less_song_whose_preload_fails_is_skipped_by_its_url(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        song = _title_less_song(controller)
        stub_loader.results["preload"] = stub_loader.Preload.FAILED
        await controller.play_song(song)
        return song

    song = _run(controller, scenario)

    (reported,) = controller.panel.skipped
    assert reported is song


def test_remove_names_a_title_less_song_by_its_webpage_url(controller):
    _queue(controller, "current")
    song = _title_less_song(controller)
    ctx, sent = command_ctx(controller)

    async def scenario():
        await Music._remove.callback(Music(None), ctx, 2)

    _run(controller, scenario)

    assert sent == [f"Removed #2: {song.webpage_url}"]


def test_start_idle_timer_starts_the_timer(controller):
    async def scenario():
        await controller.start_idle_timer(True)
        return controller.timer._task

    assert run(scenario()) is not None


def test_start_idle_timer_starts_none_while_disconnecting(controller):
    async def scenario():
        observed = []

        async def attempt():
            await controller.start_idle_timer(True)
            observed.append(controller.timer._task)

        _during_disconnect(controller, lambda: controller.add_task(attempt()))
        await controller.udisconnect("test")
        return observed

    assert _run(controller, scenario) == [None]


def test_pause_at_the_first_await_of_a_disconnect_leaves_no_timer(controller):
    async def scenario():
        controller.bot.loop = asyncio.get_running_loop()
        controller.guild.voice_client.playing = True
        reached = asyncio.Event()
        release = asyncio.Event()

        async def playback_ended():
            reached.set()
            await release.wait()

        controller.panel.playback_ended = playback_ended
        disconnect = asyncio.create_task(controller.udisconnect("test"))
        await reached.wait()
        controller.pause()
        release.set()
        await disconnect
        await _settle()
        return controller.timer._task

    assert run(scenario()) is None


@pytest.mark.parametrize(
    "loaded, requested, skipped",
    [
        (("a", "c"), ["a", "b", "c"], ["b"]),
        (("a", "b"), ["a", "b"], []),
    ],
)
def test_a_local_batch_reports_what_it_loaded_and_skipped(
    controller, stub_loader, monkeypatch, loaded, requested, skipped
):
    batch = _local_batch(
        controller, stub_loader, monkeypatch, loaded, requested
    )

    async def scenario():
        return await controller.process_local_tracks(batch, "test")

    outcome = _run(controller, scenario)

    assert (outcome.kind.value, outcome.count, outcome.skipped) == (
        "library_pick",
        len(loaded),
        tuple(f"Name {t}" for t in skipped),
    )


def test_a_disconnect_tells_the_panel_that_playback_ended(controller):
    _run(controller, lambda: controller.udisconnect("test"))

    assert controller.panel.ended == 1


def test_the_controller_imports_nothing_from_discord_ui():
    import musicbot.audiocontroller as module

    imported = imported_names(module)

    assert [m for m in imported if m.startswith("discord.ui")] == []
    assert "musicbot.player_panel" not in imported
    assert "discord.ui" not in attribute_names(module)


def test_a_play_starts_announced_on_the_panel(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")

    _run(controller, lambda: _start_playing(controller))

    assert [s.display_name for s in controller.panel.announced] == ["first"]


# --- the expired-token regression: the real panel over a channel whose
# send raises 50027 never stalls playback ---


def test_a_failed_announcement_still_lets_the_next_preload_be_scheduled(
    expired_panel_seam, monkeypatch
):
    real, stub_loader = expired_panel_seam
    _fake_ffmpeg(monkeypatch)
    _queue(real, "first", "second")

    _run(real, lambda: _start_playing(real))

    assert _preloaded(stub_loader) == ["first", "second"]


def test_a_failed_skipped_notice_still_lets_the_queue_advance(
    expired_panel_seam, monkeypatch
):
    real, stub_loader = expired_panel_seam

    async def scenario():
        release, task, _ = _pending_start(
            real,
            stub_loader,
            monkeypatch,
            "slow",
            "next",
            result=stub_loader.Preload.FAILED,
        )
        await _settle()
        release.set()
        await _finish(task)

    _run(real, scenario)

    assert _played(real) == ["next"]


def test_the_controller_keeps_no_command_channel(controller):
    assert not hasattr(controller, "command_channel")


def test_a_remote_song_opens_only_the_http_protocol_stack(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    source = controller._make_source(_song(controller, "remote"))

    before = source.original.kwargs["before_options"]
    assert "-protocol_whitelist http,https,tcp,tls,crypto" in before
    assert "-reconnect 1" in before


def test_a_local_library_song_opens_only_file(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    ac = sys.modules[type(controller).__module__]
    song = ac.Song(
        ac.SiteTypes.LOCAL_LIBRARY,
        "file:///music/a.mp3",
        url="/music/a.mp3",
        title="local",
    )
    source = controller._make_source(song)

    assert source.original.kwargs["before_options"] == (
        "-protocol_whitelist file"
    )


class _UnregisteringVoiceClient(PlaybackVoiceClient):
    """As discord.py's: closing it unregisters whatever client the guild
    has registered."""

    def __init__(self, guild):
        super().__init__()
        self.guild = guild

    async def disconnect(self, *, force=False):
        await super().disconnect(force=force)
        self.guild.voice_client = None


def _announcing_disconnect(controller, monkeypatch, during):
    """Switches ANNOUNCE_DISCONNECT on (monkeypatch restores it) and
    makes the announcement end after one poll, running `during()` at
    that poll; the poll's sleep does not wait."""
    from config import config

    _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", True)
    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)
    client = controller.guild.voice_client
    polls = []

    def is_playing():
        if not client.played:  # the sound has not started yet
            return client.playing
        polls.append(1)
        if len(polls) == 1:
            during()
            return True
        client.playing = False
        return False

    client.is_playing = is_playing
    return client


def test_a_disconnect_survives_the_voice_client_vanishing_mid_sound(
    controller, monkeypatch
):
    guild = controller.guild
    _announcing_disconnect(
        controller, monkeypatch, lambda: setattr(guild, "voice_client", None)
    )

    assert _run(controller, lambda: controller.udisconnect("test")) is True


def test_a_disconnect_leaves_a_replacement_voice_client_alone(
    controller, monkeypatch
):
    guild = controller.guild
    replacement = PlaybackVoiceClient()
    old = _announcing_disconnect(
        controller,
        monkeypatch,
        lambda: setattr(guild, "voice_client", replacement),
    )

    result = _run(controller, lambda: controller.udisconnect("test"))

    assert result is True
    assert not old.disconnected
    assert not replacement.disconnected
    assert guild.voice_client is replacement


def test_a_disconnect_closes_the_voice_client_after_the_sound(
    controller, monkeypatch
):
    client = _announcing_disconnect(controller, monkeypatch, lambda: None)

    assert _run(controller, lambda: controller.udisconnect("test")) is True
    assert client.disconnected


def test_a_disconnect_with_no_connection_returns_false(controller):
    controller.guild.voice_client = None

    assert _run(controller, lambda: controller.udisconnect("test")) is False


def test_two_concurrent_disconnects_both_succeed(controller, monkeypatch):
    from config import config

    _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", True)
    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)
    guild = controller.guild
    client = guild.voice_client = _UnregisteringVoiceClient(guild)

    async def scenario():
        both = asyncio.gather(
            controller.udisconnect("one"), controller.udisconnect("two")
        )
        await _settle()  # both are inside the announcement
        client.playing = False
        return await both

    assert _run(controller, scenario) == [True, True]
    assert guild.voice_client is None


def test_dispose_cancels_tasks_and_timer_when_the_disconnect_raises(
    controller,
):
    async def failing_disconnect(*, force=False):
        raise RuntimeError("disconnect failed")

    controller.guild.voice_client.disconnect = failing_disconnect

    async def scenario():
        await controller.timer.start()
        timer_task = controller.timer._task
        pending = []
        controller.add_task(sleep_forever(pending))
        await _settle()
        (pending_task,) = pending
        with pytest.raises(RuntimeError):
            await controller.dispose()
        await _settle()
        # read before the loop's own shutdown cancels what is left
        return timer_task.cancelled(), pending_task.cancelled()

    assert _run(controller, scenario) == (True, True)


# -- a backup write that fails, and a backup that cannot be read -----------


def _tmp_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "backup").glob("*.tmp"))


def _fail_write(controller, monkeypatch, target, name, error):
    ac = sys.modules[type(controller).__module__]

    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(getattr(ac, target), name, failing)


def _fail_replace(controller, monkeypatch, error=None):
    _fail_write(
        controller, monkeypatch, "os", "replace", error or OSError("disk full")
    )


def test_a_track_end_starts_the_next_song_when_the_backup_write_fails(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")
    _fail_replace(controller, monkeypatch)

    _run(controller, lambda: _end_first_track_on_audio_thread(controller))

    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == ["second", "third"]
    assert not controller.is_idle()


def test_stop_empties_the_queue_when_the_backup_write_fails(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    _fail_replace(controller, monkeypatch)

    async def scenario():
        await controller.ensure_playing()
        controller.stop()

    _run(controller, scenario)

    assert controller.guild.voice_client.stopped == 1
    assert _titles(controller) == []


def test_a_disconnect_goes_through_when_the_backup_write_fails(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    _fail_replace(controller, monkeypatch)

    reported = []

    async def scenario():
        await controller.ensure_playing()
        reported.append(await controller.udisconnect("d!disconnect"))

    _run(controller, scenario)

    assert reported == [True]
    assert controller.guild.voice_client.disconnected is True
    assert controller.guild.voice_client.stopped == 1
    assert _titles(controller) == []


def test_pause_and_resume_work_when_the_backup_write_fails(
    controller, monkeypatch
):
    _queue(controller, "current")
    _fail_replace(controller, monkeypatch)
    results = []

    async def scenario():
        client = controller.guild.voice_client
        client.playing = True
        results.append(controller.pause())
        results.append((client.is_paused(), client.is_playing()))
        results.append(controller.pause())
        results.append((client.is_paused(), client.is_playing()))

    _run(controller, scenario)

    assert results == [
        PauseState.PAUSED,
        (True, False),
        PauseState.RESUMED,
        (False, True),
    ]


def test_an_enqueue_works_when_the_backup_write_fails(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "current")
    songs = _songs(controller, "a")
    _fail_replace(controller, monkeypatch)

    async def scenario():
        await controller.enqueue(songs)

    _run(controller, scenario)

    assert _titles(controller) == ["current", "a"]


def test_back_starts_the_previous_song_when_the_backup_write_fails(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    _fail_replace(controller, monkeypatch)
    results = []

    async def scenario(c):
        c.skip()
        await _settle()
        results.append(c.prev_song())

    _run_playing(controller, scenario)

    assert results == [True]
    assert _titles(controller)[0] == "first"
    assert not controller.is_idle()


def _fail_pickle(controller, monkeypatch, error=None):
    ac = sys.modules[type(controller).__module__]

    def failing(*args, **kwargs):
        raise error or RuntimeError("cannot pickle")

    monkeypatch.setattr(ac.pickle, "dump", failing)


@pytest.mark.parametrize("fail", [_fail_replace, _fail_pickle])
def test_a_failed_write_keeps_the_previous_backup_and_leaves_no_temp_file(
    controller, monkeypatch, tmp_path, fail
):
    _queue(controller, "good")
    _save_backup(controller)
    before = _backup_path(controller, tmp_path).read_bytes()
    _queue(controller, "newer")
    fail(controller, monkeypatch)

    _save_backup(controller)

    assert _backup_path(controller, tmp_path).read_bytes() == before
    assert _tmp_files(tmp_path) == []


def test_an_os_error_write_failure_logs_the_guild_and_no_traceback(
    controller, monkeypatch, capsys
):
    _queue(controller, "first")
    _fail_replace(controller, monkeypatch)

    _save_backup(controller)

    err = capsys.readouterr().err
    assert f"({controller.guild.id})" in err
    assert "disk full" in err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_a_non_os_error_write_failure_logs_the_traceback(
    controller, monkeypatch, capsys
):
    _queue(controller, "first")
    _fail_replace(controller, monkeypatch, RuntimeError("cannot pickle"))

    _save_backup(controller)

    err = capsys.readouterr().err
    assert f"({controller.guild.id})" in err
    assert "Traceback" in err
    assert "cannot pickle" in err


@pytest.mark.parametrize("content", [b"", b"\x80\x04garbage", b"not pickle"])
def test_restore_of_an_unreadable_backup_changes_nothing(
    controller, tmp_path, capsys, content
):
    _queue(controller, "keep")
    _backup_path(controller, tmp_path).write_bytes(content)

    async def scenario():
        return await controller.restore()

    ac = sys.modules[type(controller).__module__]
    assert _run(controller, scenario) is ac.RestoreResult.BACKUP_UNREADABLE
    assert _titles(controller) == ["keep"]
    assert controller.guild.voice_client.played == []
    assert _backup_path(controller, tmp_path).read_bytes() == content
    err = capsys.readouterr().err
    assert f"({controller.guild.id})" in err
    assert "Error(" in err


def _failing_preloads(stub_loader, *titles):
    """Makes the preload of every song titled in `titles` fail."""
    original = stub_loader.preload

    async def preload(song):
        result = await original(song)
        if song.title in titles:
            return stub_loader.Preload.FAILED
        return result

    stub_loader.preload = preload


def _preload_counts(stub_loader):
    counts = {}
    for title in _preloaded(stub_loader):
        counts[title] = counts.get(title, 0) + 1
    return counts


def _loop_all(controller):
    controller.playlist.loop = LoopMode.ALL


def test_loop_all_one_song_whose_preload_fails_is_reported_once_and_idles(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "broken")
    _loop_all(controller)
    _failing_preloads(stub_loader, "broken")

    _bounded(controller, lambda: _start_playing(controller))

    assert [s.title for s in controller.panel.skipped] == ["broken"]
    assert _titles(controller) == []
    assert controller.timer._task is not None
    assert _played(controller) == []
    assert _preload_counts(stub_loader) == {"broken": 1}
    assert _backup(controller, tmp_path) == []
    assert _history_songs(controller) == []


def _history_songs(controller):
    return [s.title for s in controller.playlist.playhistory]


def test_loop_all_every_preload_failing_drains_to_idle(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")
    _loop_all(controller)
    _failing_preloads(stub_loader, "a", "b", "c")

    _bounded(controller, lambda: _start_playing(controller))

    assert [s.title for s in controller.panel.skipped] == ["a", "b", "c"]
    assert _titles(controller) == []
    assert controller.timer._task is not None
    assert _played(controller) == []
    assert _preload_counts(stub_loader) == {"a": 1, "b": 1, "c": 1}
    assert _backup(controller, tmp_path) == []
    assert _history_songs(controller) == []


def test_loop_all_song_without_a_url_is_reported_once_and_removed(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    controller.playlist.add(_without_url(controller, "no url"))
    _loop_all(controller)

    _bounded(controller, lambda: _start_playing(controller))

    assert [s.title for s in controller.panel.skipped] == ["no url"]
    assert _titles(controller) == []
    assert controller.timer._task is not None
    assert _played(controller) == []
    assert _preload_counts(stub_loader) == {"no url": 1}
    assert _backup(controller, tmp_path) == []
    assert _history_songs(controller) == []


def _failing_at_the_head(controller, stub_loader, *titles):
    """Makes the preload of a song titled in `titles` fail only while
    it is the queue head, so the preload sweep behind the head leaves
    it for the failed-track path."""
    original = stub_loader.preload

    async def preload(song):
        result = await original(song)
        queue = controller.playlist.playque
        if song.title in titles and queue and queue[0] is song:
            return stub_loader.Preload.FAILED
        return result

    stub_loader.preload = preload


def test_loop_all_mixed_queue_drops_failing_songs_and_keeps_the_rest(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "bad", "good", "worse", "fine")
    _loop_all(controller)
    _failing_at_the_head(controller, stub_loader, "bad", "worse")

    async def next_round():
        controller.playback.skip()
        await _settle()
        controller.guild.voice_client.playing = False
        await _settle()

    async def scenario():
        await _start_playing(controller)
        assert _played(controller) == ["good"]
        # "worse" rotates to the head and fails there; "fine" follows
        await next_round()
        assert _played(controller) == ["good", "fine"]
        await next_round()

    _bounded(controller, scenario)

    assert [s.title for s in controller.panel.skipped] == ["bad", "worse"]
    assert sorted(_titles(controller)) == ["fine", "good"]
    assert _history_songs(controller) == []


class _HookedPanel(RecordingPanel):
    """A recording panel whose failure notice runs `on_notice(song)`
    after recording it; a raising hook stands for a failed send, which
    a real panel logs and swallows."""

    on_notice = None

    async def report_skipped(self, song):
        await super().report_skipped(song)
        if self.on_notice is not None:
            try:
                await self.on_notice(song)
            except Exception:
                pass


@pytest.fixture
def hooked_controller(tmp_path):
    with controller_seam(tmp_path, lambda guild, get: _HookedPanel()) as (
        built
    ):
        yield built


def test_a_start_overtaken_after_its_report_drops_and_starts_nothing(
    hooked_controller, monkeypatch
):
    controller, stub_loader = hooked_controller
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "broken", "next")
    _failing_preloads(stub_loader, "broken")

    voice = controller.guild.voice_client

    async def disconnect_during_notice(song):
        controller.guild.voice_client = None

    controller.panel.on_notice = disconnect_during_notice

    _bounded(controller, lambda: _start_playing(controller))

    assert [s.title for s in controller.panel.skipped] == ["broken"]
    assert _titles(controller) == ["broken", "next"]
    assert voice.played == []


def test_a_failed_notice_send_still_drops_and_moves_on(
    expired_panel_seam, monkeypatch
):
    real, stub_loader = expired_panel_seam
    _fake_ffmpeg(monkeypatch)
    _queue(real, "broken", "next")
    _failing_preloads(stub_loader, "broken")
    sends = []

    class RefusingChannel:
        async def send(self, *args, **kwargs):
            sends.append(args)
            raise EXPIRED

    real.panel.direct_to(RefusingChannel())

    _bounded(real, lambda: _start_playing(real))

    assert sends
    assert _played(real) == ["next"]
    assert _titles(real)[0] == "next"
    assert "broken" not in _titles(real)


# --- a track that ends on its own having produced no audio ------------


def _silent_voice(controller):
    controller.guild.voice_client.produces_nothing = True


@pytest.mark.parametrize("mode", list(LoopMode))
def test_a_track_ending_on_its_own_with_no_audio_is_a_failed_track(
    controller, stub_loader, monkeypatch, tmp_path, mode
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "dead", "next")
    controller.playlist.loop = mode
    _silent_voice(controller)

    _bounded(controller, lambda: _end_first_track_on_audio_thread(controller))

    assert [s.title for s in controller.panel.skipped] == ["dead"]
    assert _titles(controller) == ["next"]
    assert _played(controller) == ["dead", "next"]
    assert _preload_counts(stub_loader)["dead"] == 1
    assert _backup(controller, tmp_path) == ["next"]
    assert _history_songs(controller) == []
    assert "dead" in _history(controller)


def test_a_lone_track_ending_with_no_audio_under_loop_single_idles(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "dead")
    controller.playlist.loop = LoopMode.SINGLE
    _silent_voice(controller)

    _bounded(controller, lambda: _end_first_track_on_audio_thread(controller))

    assert [s.title for s in controller.panel.skipped] == ["dead"]
    assert _titles(controller) == []
    assert _played(controller) == ["dead"]
    assert controller.timer._task is not None


async def _end_with_notice_in_flight(controller, command):
    """Ends the silent head on its own with the failure notice held,
    runs `command(controller)`, then lets the notice finish."""
    gate = asyncio.Event()

    async def held_notice(song):
        await gate.wait()

    controller.panel.on_notice = held_notice
    after = await _start_and_end_first_track(controller)
    await _fire_without_raising(after)
    assert [s.title for s in controller.panel.skipped] == ["first"]
    await command(controller)
    gate.set()
    await _settle()


async def _cmd_stop(controller):
    controller.stop()


async def _cmd_skip(controller):
    controller.skip()


async def _cmd_back(controller):
    controller.prev_song()


async def _cmd_newer_start(controller):
    await controller.play_song(controller.playlist[1])


async def _cmd_disconnect(controller):
    controller.guild.voice_client = None


@pytest.mark.parametrize(
    "command, played, titles",
    [
        (_cmd_stop, ["first"], []),
        (_cmd_skip, ["first", "second"], ["second"]),
        (_cmd_back, ["first", "old"], ["old", "first", "second"]),
        (_cmd_newer_start, ["first", "second"], ["first", "second"]),
    ],
)
def test_a_command_overtakes_a_failure_notice_in_flight(
    hooked_controller, monkeypatch, command, played, titles
):
    controller, _ = hooked_controller
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    _silent_voice(controller)
    _old_track(controller)

    _bounded(
        controller, lambda: _end_with_notice_in_flight(controller, command)
    )

    assert _played(controller) == played
    assert _titles(controller) == titles


def test_a_disconnect_overtaking_a_failure_notice_keeps_backup_no_idle(
    hooked_controller, monkeypatch, tmp_path
):
    controller, _ = hooked_controller
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    _silent_voice(controller)

    async def scenario():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        _save_backup(controller)
        await _end_with_notice_in_flight(controller, _cmd_disconnect)

    _bounded(controller, scenario)

    assert _titles(controller) == ["first", "second"]
    assert _backup(controller, tmp_path) == ["first", "second"]
    assert controller.timer._task is None


@pytest.mark.parametrize("mode", list(LoopMode))
@pytest.mark.parametrize(
    "command, played",
    [
        (_cmd_stop, []),
        (_cmd_skip, ["second"]),
        (_cmd_back, None),
    ],
)
def test_a_user_command_on_a_track_with_no_audio_is_not_a_failure(
    controller, monkeypatch, command, played, mode
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    controller.playlist.loop = mode
    _silent_voice(controller)
    _old_track(controller)

    async def scenario(c):
        await command(c)
        await _settle()

    _run_playing(controller, scenario, runner=_bounded)

    assert controller.panel.skipped == []
    if command is _cmd_back:
        # the queue's real step-back: history under OFF/SINGLE, rotation
        # under ALL
        played = ["second"] if mode == LoopMode.ALL else ["old"]
    assert _played(controller)[1:] == played
    if command is _cmd_stop:
        assert _titles(controller) == []
    elif command is _cmd_skip:
        # a skip follows the loop mode's normal advance: rotation under
        # ALL, the song into the play history otherwise
        if mode == LoopMode.ALL:
            assert _titles(controller) == ["second", "first"]
            assert _history_songs(controller) == ["old"]
        else:
            assert _titles(controller) == ["second"]
            assert _history_songs(controller) == ["old", "first"]
    else:
        # loose on purpose (deviation, #1012)
        assert "first" in _titles(controller)


@pytest.mark.parametrize(
    "mode, played",
    [
        (LoopMode.OFF, ["first", "second"]),
        (LoopMode.SINGLE, ["first", "first"]),
    ],
)
def test_a_track_with_audio_then_an_error_advances_as_finished(
    controller, stub_loader, monkeypatch, mode, played
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    controller.playlist.loop = mode

    _bounded(
        controller,
        lambda: _end_first_track_on_audio_thread(
            controller, RuntimeError("boom")
        ),
    )

    assert _played(controller) == played
    assert controller.panel.skipped == []


def test_a_volume_change_reaches_the_wrapped_source(controller, monkeypatch):
    import discord

    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")
    built = []

    class RecordingTransformer(discord.PCMVolumeTransformer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(discord, "PCMVolumeTransformer", RecordingTransformer)

    async def scenario(c):
        c.set_volume(25)

    _run_playing(controller, scenario)

    given = controller.guild.voice_client.source
    assert given.volume == 0.25
    assert len(built) == 1
    assert built[0].volume == 0.25
