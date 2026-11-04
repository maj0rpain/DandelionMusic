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

from controller_helpers import (
    _backup,
    _backup_path,
    _fake_ffmpeg,
    _preloaded,
    _queue,
    _run,
    _run_intent,
    _settle,
    _songs,
    _titles,
)
from musicbot import playlists
from musicbot.commands.music import Music
from controller_seam import FakeVoiceClient, StubLoader, controller_seam
from db_seam import in_memory_db
from discord_fakes import PLAYER_BUTTONS


MIX = playlists.PlaylistRef("1234", "mix")


class _FakeChannel:
    """A channel or context: records what the controller sends."""

    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


async def _start_playing(controller):
    """Starts the queue head on the running loop and lets it settle."""
    controller.bot.loop = asyncio.get_running_loop()
    await controller.ensure_playing()
    await _settle()


def _run_playing(controller, scenario):
    """Runs `scenario(controller)` on a loop, with the queue head
    started first and the loop given time to settle afterwards."""

    async def main():
        await _start_playing(controller)
        await scenario(controller)

    _run(controller, main)


def _played(controller):
    return [
        source.original.url.rsplit("/", 1)[-1]
        for source in controller.guild.voice_client.played
    ]


def test_controller_pickles_its_playlist_into_the_temp_backup_dir(
    controller, tmp_path
):
    _queue(controller, "first track", "second track")

    controller._pickle_playlist()

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
    assert (
        asyncio.run(stub_loader.preload(song)) is stub_loader.Preload.EXTRACTED
    )
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
        built._pickle_playlist()

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
    # "first" each time it starts; "second" by both queue preloads,
    # which run only once restore() has brought it back
    assert _preloaded(stub_loader) == ["first", "first", "second", "second"]


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
        controller._pickle_playlist()
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

    _run_intent(controller, controller.shuffle)

    titles = _titles(controller)
    assert sorted(titles) == ["a", "b", "c", "current", "d", "e"]
    assert _backup(controller, tmp_path) == titles
    assert _preloaded(stub_loader) == titles[1:]


def test_move_reorders_the_queue_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c")

    _run_intent(controller, lambda: controller.move(3, 1))

    assert _titles(controller) == ["current", "c", "a", "b"]
    assert _backup(controller, tmp_path) == ["current", "c", "a", "b"]
    assert _preloaded(stub_loader) == ["c", "a", "b"]


def test_remove_drops_a_track_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c")

    removed = _run_intent(controller, lambda: controller.remove(2))

    assert removed.title == "b"
    assert _titles(controller) == ["current", "a", "c"]
    assert _backup(controller, tmp_path) == ["current", "a", "c"]
    assert _preloaded(stub_loader) == ["a", "c"]


def test_clear_keeps_the_current_track_refreshes_backup_and_preloads_nothing(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b")

    _run_intent(controller, controller.clear)

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


def test_enqueue_next_puts_the_song_after_the_current_track(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a")
    song = _songs(controller, "next")[0]

    async def scenario():
        controller.guild.voice_client.playing = True
        await controller.enqueue_next(song)

    _run(controller, scenario)

    assert controller.guild.voice_client.played == []
    assert _titles(controller) == ["current", "next", "a"]
    assert _backup(controller, tmp_path) == ["current", "next", "a"]
    assert _preloaded(stub_loader) == ["next", "a"]


def test_enqueue_next_starts_the_song_when_idle(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    song = _songs(controller, "next")[0]

    async def scenario():
        await controller.enqueue_next(song)

    _run(controller, scenario)

    assert _played(controller) == ["next"]
    assert _titles(controller) == ["next"]
    assert _backup(controller, tmp_path) == ["next"]


def test_enqueue_no_longer_takes_playnext(controller):
    songs = _songs(controller, "a", "b")

    async def scenario():
        await controller.enqueue(songs, playnext=True)

    with pytest.raises(TypeError):
        _run(controller, scenario)


async def _resolved(value):
    return value


class _FakeMessage:
    """A sent message: records every view it is edited to."""

    def __init__(self):
        self.edits = []

    async def edit(self, *, view):
        self.edits.append(view)


def test_attach_view_moves_the_buttons_onto_the_new_message(controller):
    import discord

    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    sent = []
    new_message = _FakeMessage()

    async def send(view):
        sent.append(view)
        return new_message

    async def scenario():
        old_message = _FakeMessage()
        await controller.attach_view(lambda view: _resolved(old_message))
        result = await controller.attach_view(send)
        return old_message, result

    old_message, result = _run(controller, scenario)

    assert old_message.edits == [None]
    assert len(sent) == 1 and isinstance(sent[0], discord.ui.View)
    assert result is new_message
    # the next attach_view takes the buttons off the new message
    asyncio.run(controller.attach_view(lambda view: _resolved(_FakeMessage())))
    assert new_message.edits == [None]


def test_attach_view_records_an_interactions_original_response(controller):
    import discord

    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    response = _FakeMessage()

    class FakeInteraction(discord.Interaction):
        def __init__(self):
            pass

        async def original_response(self):
            return response

    interaction = FakeInteraction()

    async def send(view):
        return interaction

    result = asyncio.run(controller.attach_view(send))

    assert result is interaction
    # the buttons the next attach_view takes off are the response's

    async def send_plain(view):
        return _FakeMessage()

    asyncio.run(controller.attach_view(send_plain))
    assert response.edits == [None]


def test_dispose_cancels_the_timer_and_every_pending_task(
    controller, monkeypatch
):

    async def scenario():
        await controller.timer.start()
        timer_task = controller.timer._task
        controller.add_task(asyncio.sleep(3600))
        (pending_task,) = controller._tasks

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
    """Wraps the queue writes and the pickle to record the thread
    each one runs on."""
    seen = []

    def recording(name, original):
        def wrapper(*args, **kwargs):
            seen.append((name, threading.get_ident()))
            return original(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        controller,
        "_pickle_playlist",
        recording("pickle", controller._pickle_playlist),
    )
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

    after = asyncio.run(_start_and_end_first_track(controller))
    seen = _record_threads(controller, monkeypatch)
    fake_loop = controller.bot.loop = _RecordingLoop()

    _, error = _in_thread(after, None)

    assert error is None
    assert fake_loop.calls == [
        (controller._on_track_end, (None, controller._generation))
    ]
    assert seen == []
    assert _titles(controller) == ["first", "second"]
    assert not _backup_path(controller, tmp_path).exists()


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
        f"Playback error in guild {controller.guild.id}: "
        "RuntimeError('boom')"
    )
    assert expected in err
    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == ["second", "third"]


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
        seen = _record_threads(controller, monkeypatch)
        controller._on_track_end(None, controller._generation - 1)
        await _settle()
        return seen

    seen = _run(controller, scenario)

    assert seen == []
    assert _titles(controller) == ["first", "second"]
    assert voice_client.stopped == 0
    assert voice_client.is_playing()
    assert _played(controller) == ["first"]
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
        controller._next_song = controller.playlist.next(True)
        stale_after = _end_track(controller.guild.voice_client)
        await controller.ensure_playing()
        stale_after(None)
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

    # asyncio.run() closes its loop on the way out, as bot shutdown does
    after = asyncio.run(_start_and_end_first_track(controller))
    assert controller.bot.loop.is_closed()

    _, error = _in_thread(after, None)

    assert error is None
    assert _titles(controller) == ["first", "second"]
    assert _played(controller) == ["first"]


async def _drain(controller):
    while controller._tasks:
        await asyncio.gather(*controller._tasks)


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
        await _drain(controller)
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
        # a failed preload drops the song, as before
        ("FAILED", ["playing"]),
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
    """Starts the queue head with its backup written, then drops the
    guild's voice client as discord.py's cleanup does and fires the
    track's after= callback."""
    controller._pickle_playlist()
    after = await _start_and_end_first_track(controller)
    controller.guild.voice_client = None
    await _fire_without_raising(after)


def _fresh_voice_client(controller):
    controller.guild.voice_client = FakeVoiceClient()


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

    assert _history(controller) == ["first", "second"]


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
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "refused")
    controller.guild.voice_client.playing = True

    async def scenario():
        await controller.play_song(controller.playlist[0])

    _run(controller, scenario)
    assert _history(controller) == []


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


def test_a_failed_non_head_preload_still_removes_the_song(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")

    async def scenario():
        futures = _gated_preload(stub_loader, "b")
        await _start_playing(controller)
        futures[0].set_result(stub_loader.Preload.FAILED)

    _run(controller, scenario)

    assert _titles(controller) == ["a", "c"]


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
        generation = controller._generation
        _during_disconnect(
            controller,
            lambda: controller._on_track_end(None, generation),
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
        controller.guild.voice_client.playing = False
        await controller.timer.start()
        await _settle()
        assert len(disconnects) == 1
        controller._on_track_end(None, controller._generation)
        await _settle()
        return disconnects

    assert len(_run(controller, scenario)) == 2


def _button(view, custom_id):
    return next(c for c in view.children if c.custom_id == custom_id)


def test_volume_down_lowers_the_volume_to_zero_from_five(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    controller.set_volume(5)

    controller.volume_down()

    assert controller.volume == 0


def test_the_volume_down_button_is_disabled_at_zero_only(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True

    controller.set_volume(10)
    assert not _button(controller.make_view(), "volume_down").disabled
    controller.set_volume(0)
    view = controller.make_view()
    assert _button(view, "volume_down").disabled
    assert not _button(view, "volume_up").disabled
    controller.set_volume(100)
    assert _button(controller.make_view(), "volume_up").disabled


def test_a_current_song_click_with_no_current_song_replies_queue_empty(
    controller,
):
    from config import config

    ctx = _FakeChannel()

    asyncio.run(controller.current_song_callback(ctx))

    assert ctx.sent == [((config.QUEUE_EMPTY,), {})]


def test_a_current_song_click_while_a_track_plays_shows_the_song_info(
    controller,
):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    ctx = _FakeChannel()

    asyncio.run(controller.current_song_callback(ctx))

    assert len(ctx.sent) == 1 and "embed" in ctx.sent[0][1]


def _http_exception(status, code):
    import discord

    class _Response:
        def __init__(self):
            self.status = status
            self.reason = "x"

    return discord.HTTPException(_Response(), {"code": code, "message": "x"})


class _FlakyMessage(_FakeMessage):
    """Fails its first edit with `error`, then edits normally."""

    def __init__(self, error):
        super().__init__()
        self.error = error
        self.id = 1
        self.channel = self

    async def edit(self, *, view):
        if self.error:
            error, self.error = self.error, None
            raise error
        self.edits.append(view)

    async def fetch_message(self, _id):
        return self


def test_the_retry_after_an_expired_token_edit_applies_the_view(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    message = _FlakyMessage(_http_exception(401, 50027))

    async def scenario():
        await controller.attach_view(lambda view: _resolved(message))
        controller.set_volume(0)  # the buttons now differ
        await controller.update_view()

    _run(controller, scenario)

    assert len(message.edits) == 1 and message.edits[0] is not None


def test_a_refresh_after_a_failed_edit_applies_the_view_again(controller):
    _queue(controller, "current")
    voice = controller.guild.voice_client
    message = _FlakyMessage(_http_exception(500, 0))

    async def scenario():
        await controller.attach_view(lambda view: _resolved(message))
        voice.playing = True
        await controller.update_view()
        await controller.update_view()

    _run(controller, scenario)

    assert len(message.edits) == 1 and message.edits[0] is not None


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
        async def noop(*args, **kwargs):
            pass

        ctx = types.SimpleNamespace(
            bot=types.SimpleNamespace(DbSession=factory),
            guild=controller.guild,
            audiocontroller=controller,
            defer=noop,
            send=noop,
        )
        await Music._playlist_load.callback(Music(None), ctx, "mix")
        await _drain(controller)
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
    controller.command_channel = _FakeChannel()
    _queue(controller, *titles)
    slow = controller.playlist[0]
    release = stub_loader.hold(slow, result)
    task = asyncio.ensure_future(controller.ensure_playing())
    return release, task, slow


def _without_url(controller, title):
    song = _songs(controller, title)[0]
    song.url = None
    return song


def _old_track(controller):
    song = _songs(controller, "old")[0]
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


def test_a_local_batch_during_a_pending_start_starts_nothing_new(
    controller, stub_loader, monkeypatch
):
    by_title = {s.title: s for s in _songs(controller, "a", "b")}

    async def load_local_songs(tracks):
        return [by_title[t] for t in tracks]

    monkeypatch.setattr(stub_loader, "load_local_songs", load_local_songs)

    async def scenario():
        release, task, _ = _pending_start(
            controller, stub_loader, monkeypatch, "slow"
        )
        await _settle()
        await controller.process_local_tracks(["a", "b"], "test")
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
    assert controller.command_channel.sent == []


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
    assert controller.command_channel.sent == []


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
    assert controller.command_channel.sent == []
    assert controller.guild.voice_client.disconnected is False


def test_a_disconnect_during_a_pending_start_leaves_nothing_playing(
    controller, stub_loader, monkeypatch
):
    async def act():
        await controller.udisconnect("test")

    later = _preloads_after_release(controller, stub_loader, monkeypatch, act)

    assert _played(controller) == []
    assert later == []
    assert controller.command_channel.sent == []
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
    assert controller.command_channel.sent == []
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
    assert controller.command_channel.sent == []
    # "slow" is gone because the queue preload behind "old" met its
    # FAILED result; "next" was not started or dropped by the stale one
    assert _titles(controller) == ["old", "next"]


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

    assert len(controller.command_channel.sent) == 1
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
        controller.command_channel = _FakeChannel()
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
        restarted = type(controller)(controller.bot, controller.guild)
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
    song = _songs(controller, "untitled")[0]
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
        controller.command_channel = _FakeChannel()
        song = _title_less_song(controller)
        stub_loader.results["preload"] = stub_loader.Preload.FAILED
        await controller.play_song(song)
        return song

    song = _run(controller, scenario)

    ((args, _),) = controller.command_channel.sent
    assert f"(Skipped: {song.webpage_url})" in args[0]


def test_remove_names_a_title_less_song_by_its_webpage_url(controller):
    _queue(controller, "current")
    song = _title_less_song(controller)
    sent = []

    async def send(text):
        sent.append(text)

    ctx = types.SimpleNamespace(audiocontroller=controller, send=send)

    async def scenario():
        await Music._remove.callback(Music(None), ctx, 2)

    _run(controller, scenario)

    assert sent == [f"Removed #2: {song.webpage_url}"]


def test_the_player_view_buttons_are_the_player_buttons_in_order(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True

    ids = [c.custom_id for c in controller.make_view().children]

    assert ids == PLAYER_BUTTONS


def test_start_idle_timer_starts_the_timer(controller):
    async def run():
        await controller.start_idle_timer(True)
        return controller.timer._task

    assert asyncio.run(run()) is not None


def test_start_idle_timer_starts_none_while_disconnecting(controller):
    async def run():
        controller._disconnecting = True
        await controller.start_idle_timer(True)
        return controller.timer._task

    assert asyncio.run(run()) is None


def test_pause_at_the_first_await_of_a_disconnect_leaves_no_timer(controller):
    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        controller.guild.voice_client.playing = True
        reached = asyncio.Event()
        release = asyncio.Event()

        async def update_view(view=None):
            reached.set()
            await release.wait()

        controller.update_view = update_view
        disconnect = asyncio.create_task(controller.udisconnect("test"))
        await reached.wait()
        controller.pause()
        release.set()
        await disconnect
        await _settle()
        return controller.timer._task

    assert asyncio.run(run()) is None
