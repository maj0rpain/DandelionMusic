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

from controller_seam import StubLoader, controller_seam
from musicbot.commands.music import Music


def test_controller_pickles_its_playlist_into_the_temp_backup_dir(
    controller, tmp_path
):
    # The Song and SiteTypes the controller module itself sees: the
    # seam imports it fresh, so they are not the ones a top-level
    # import in this file would bind.
    ac = sys.modules[type(controller).__module__]
    for title in ("first track", "second track"):
        controller.playlist.add(
            ac.Song(
                ac.SiteTypes.YT_DLP,
                f"https://example.invalid/{title}",
                title=title,
            )
        )

    controller._pickle_playlist()

    backup = tmp_path / "backup" / "playlist_1234.pickle"
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

    with controller_seam(work) as (built, _):
        assert isinstance(sys.modules["musicbot.loader"], StubLoader)
        built._pickle_playlist()

    after = sys.modules.get("musicbot.loader")
    assert after is before
    assert not isinstance(
        getattr(sys.modules.get("musicbot"), "loader", None), StubLoader
    )
    controller_module = sys.modules.get("musicbot.audiocontroller")
    assert not isinstance(
        getattr(controller_module, "loader", None), StubLoader
    )
    assert not (caller / "backup").exists()
    assert (work / built.pickle_file).exists()


def _queue(controller, *titles):
    ac = sys.modules[type(controller).__module__]
    for title in titles:
        controller.playlist.add(
            ac.Song(
                ac.SiteTypes.YT_DLP,
                f"https://example.invalid/{title}",
                url=f"https://stream.invalid/{title}",
                title=title,
            )
        )


class _FakeFFmpeg:
    """Stands in for ffmpeg: records the stream it was asked to open."""

    def __init__(self, url, **kwargs):
        self.url = url

    def read(self):
        return b""


async def _settle():
    # lets the preload task the controller scheduled run to the end
    for _ in range(10):
        await asyncio.sleep(0)


def test_ensure_playing_starts_the_head_of_the_queue_when_idle(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first track", "second track")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()

    asyncio.run(run())

    played = controller.guild.voice_client.played
    assert [source.original.url for source in played] == [
        "https://stream.invalid/first track"
    ]
    assert _titles(controller) == ["first track", "second track"]
    # ensure_playing() writes no backup: that is queue()'s job
    assert not (tmp_path / "backup" / "playlist_1234.pickle").exists()
    # play_song preloads the track it starts, then the queue behind it
    assert _preloaded(stub_loader) == ["first track", "second track"]


def test_ensure_playing_only_preloads_while_something_is_playing(
    controller, stub_loader, tmp_path
):
    controller.guild.voice_client.playing = True
    _queue(controller, "current track", "next track")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()

    asyncio.run(run())

    assert controller.guild.voice_client.played == []
    assert _titles(controller) == ["current track", "next track"]
    assert not (tmp_path / "backup" / "playlist_1234.pickle").exists()
    assert _preloaded(stub_loader) == ["next track"]


def _fake_ffmpeg(monkeypatch):
    import discord

    monkeypatch.setattr(
        discord,
        "FFmpegPCMAudio",
        type("FakeFFmpeg", (_FakeFFmpeg, discord.AudioSource), {}),
    )


def _played(controller):
    return [
        source.original.url.rsplit("/", 1)[-1]
        for source in controller.guild.voice_client.played
    ]


def _backup(tmp_path):
    with open(tmp_path / "backup" / "playlist_1234.pickle", "rb") as f:
        return [song.title for song in pickle.load(f).playque]


def _titles(controller):
    return [song.title for song in controller.playlist.playque]


def test_skip_moves_on_to_the_next_track(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller.skip()
        await _settle()

    asyncio.run(run())

    assert _played(controller) == ["first", "second"]
    assert _titles(controller) == ["second", "third"]
    assert _backup(tmp_path) == ["second", "third"]
    # play_song preloads the track it starts, then the queue behind
    # it: "third" once behind "first" (after skip moved past it) and
    # once behind "second"
    assert _preloaded(stub_loader) == ["first", "third", "second", "third"]


def test_stop_ends_playback_and_empties_the_queue_but_keeps_the_backup(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller.stop()
        await _settle()

    asyncio.run(run())

    assert controller.guild.voice_client.stopped == 1
    assert _played(controller) == ["first"]
    assert _titles(controller) == []
    # the snapshot is taken before the queue is cleared: d!restore
    # brings back what was queued at the time of d!stop
    assert _backup(tmp_path) == ["first", "second", "third"]
    # only the track that started; the queue behind it was gone
    # before its preload ran
    assert _preloaded(stub_loader) == ["first"]


def test_after_a_stop_track_changes_refresh_the_backup_again(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller.stop()
        _queue(controller, "second", "third")
        await controller.ensure_playing()
        controller.skip()
        await _settle()

    asyncio.run(run())

    assert _played(controller) == ["first", "second", "third"]
    assert _backup(tmp_path) == ["third"]


def test_a_stop_while_idle_does_not_hold_back_the_next_backup(
    controller, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        controller.stop()
        _queue(controller, "first", "second")
        await controller.ensure_playing()
        controller.skip()
        await _settle()

    asyncio.run(run())

    assert _played(controller) == ["first", "second"]
    assert _backup(tmp_path) == ["second"]


def test_a_late_teardown_callback_does_not_overwrite_the_stop_backup(
    controller, monkeypatch, tmp_path
):
    # discord.py runs the `after` callback later, on its audio thread:
    # here it fires only once a new track has already started
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    voice_client = controller.guild.voice_client

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        stale_after = voice_client.detach_after()
        controller.stop()
        _queue(controller, "third", "fourth", "fifth")
        await controller.ensure_playing()
        stale_after(None)
        await _settle()
        frozen = _backup(tmp_path)
        controller.skip()
        await _settle()
        return frozen

    assert asyncio.run(run()) == ["first", "second"]
    # the stale callback neither ended "third" nor used up the latch,
    # so the skip after it - a genuine advance - snapshots
    assert _played(controller) == ["first", "third", "fourth"]
    assert _backup(tmp_path) == ["fourth", "fifth"]


def test_restore_brings_back_the_queue_a_stop_cleared(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller.stop()
        restored = await controller.restore()
        await _settle()
        return restored

    ac = sys.modules[type(controller).__module__]
    assert asyncio.run(run()) is ac.RestoreResult.RESTORED
    assert _played(controller) == ["first", "first"]
    assert _titles(controller) == ["first", "second"]
    assert _backup(tmp_path) == ["first", "second"]
    # "first" each time it starts; "second" by both queue preloads,
    # which run only once restore() has brought it back
    assert _preloaded(stub_loader) == ["first", "first", "second", "second"]


def test_restore_with_no_backup_and_an_empty_queue_plays_nothing(
    controller,
):
    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        return await controller.restore()

    ac = sys.modules[type(controller).__module__]
    assert asyncio.run(run()) is ac.RestoreResult.NOTHING_TO_RESTORE
    assert controller.guild.voice_client.played == []


def _restore_while_active(controller, monkeypatch, tmp_path, pause):
    from config import config

    # were restore() to disconnect, it must not announce it to a
    # channel the fakes do not have
    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    backup = tmp_path / "backup" / "playlist_1234.pickle"

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller._pickle_playlist()
        if pause:
            controller.guild.voice_client.pause()
        before = (controller.playlist, backup.read_bytes())
        result = await controller.restore()
        await _settle()
        return before, result

    return asyncio.run(run())


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
    backup = tmp_path / "backup" / "playlist_1234.pickle"
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
    assert not (tmp_path / "backup" / "playlist_1234.pickle").exists()
    assert stub_loader.calls == []


def _preloaded(stub_loader):
    return [args[0].title for name, args in stub_loader.calls]


def _while_playing(controller, intent):
    """Runs `intent` with a track already playing, then lets the
    preload it scheduled finish."""
    controller.guild.voice_client.playing = True

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        result = intent()
        await _settle()
        return result

    return asyncio.run(run())


def test_move_reorders_the_queue_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c")

    _while_playing(controller, lambda: controller.move(3, 1))

    assert _titles(controller) == ["current", "c", "a", "b"]
    assert _backup(tmp_path) == ["current", "c", "a", "b"]
    assert _preloaded(stub_loader) == ["c", "a", "b"]


def test_remove_drops_a_track_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b", "c")

    removed = _while_playing(controller, lambda: controller.remove(2))

    assert removed.title == "b"
    assert _titles(controller) == ["current", "a", "c"]
    assert _backup(tmp_path) == ["current", "a", "c"]
    assert _preloaded(stub_loader) == ["a", "c"]


def test_clear_keeps_the_current_track_refreshes_backup_and_preloads_nothing(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b")

    _while_playing(controller, controller.clear)

    assert _titles(controller) == ["current"]
    assert _backup(tmp_path) == ["current"]
    assert stub_loader.calls == []


def _songs(controller, *titles):
    ac = sys.modules[type(controller).__module__]
    return [
        ac.Song(
            ac.SiteTypes.YT_DLP,
            f"https://example.invalid/{title}",
            url=f"https://stream.invalid/{title}",
            title=title,
        )
        for title in titles
    ]


def test_queue_appends_while_playing_and_refreshes_backup_and_preload(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current")
    songs = _songs(controller, "a", "b")

    async def run():
        controller.guild.voice_client.playing = True
        controller.bot.loop = asyncio.get_running_loop()
        await controller.queue(songs)
        await _settle()

    asyncio.run(run())

    assert controller.guild.voice_client.played == []
    assert _titles(controller) == ["current", "a", "b"]
    assert _backup(tmp_path) == ["current", "a", "b"]
    assert _preloaded(stub_loader) == ["a", "b"]


def test_queue_starts_the_first_track_when_idle(
    controller, stub_loader, monkeypatch, tmp_path
):
    _fake_ffmpeg(monkeypatch)
    songs = _songs(controller, "a", "b")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.queue(songs)
        await _settle()

    asyncio.run(run())

    assert _played(controller) == ["a"]
    assert _titles(controller) == ["a", "b"]
    assert _backup(tmp_path) == ["a", "b"]
    # play_song preloads the track it starts, then the queue behind it
    assert _preloaded(stub_loader) == ["a", "b"]


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

    async def run():
        old_message = _FakeMessage()
        await controller.attach_view(lambda view: _return(old_message))
        result = await controller.attach_view(send)
        return old_message, result

    async def _return(message):
        return message

    old_message, result = asyncio.run(run())

    assert old_message.edits == [None]
    assert len(sent) == 1 and isinstance(sent[0], discord.ui.View)
    assert result is new_message
    # the next attach_view takes the buttons off the new message
    asyncio.run(controller.attach_view(lambda view: _return(_FakeMessage())))
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
    from config import config

    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.timer.start()
        timer_task = controller.timer._task
        controller.add_task(asyncio.sleep(3600))
        (pending_task,) = controller._tasks

        await controller.dispose()
        await _settle()
        return timer_task, pending_task

    timer_task, pending_task = asyncio.run(run())

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
    controller.bot.loop = asyncio.get_running_loop()
    await controller.ensure_playing()
    await _settle()
    return _end_track(controller.guild.voice_client)


async def _end_first_track_on_audio_thread(controller, error=None):
    """Starts the queue head on the running loop, fires its `after`
    callback with `error` from a plain thread, as discord.py's audio
    thread would, and lets the loop run the hop."""
    after = await _start_and_end_first_track(controller)
    _in_thread(after, error)
    await _settle()


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
    assert not (tmp_path / "backup" / "playlist_1234.pickle").exists()


def test_a_track_end_writes_the_queue_and_backup_only_on_the_loop(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")
    seen = _record_threads(controller, monkeypatch)

    async def run():
        await _end_first_track_on_audio_thread(controller)
        return threading.get_ident()

    loop_thread = asyncio.run(run())

    assert _played(controller) == ["first", "second"]
    assert {name for name, _ in seen} == {"pickle", "next", "add_name"}
    assert {thread for _, thread in seen} == {loop_thread}


def _track_end_stderr(controller, capsys, monkeypatch, error):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    asyncio.run(_end_first_track_on_audio_thread(controller, error))
    return capsys.readouterr().err


def test_a_track_ended_by_an_error_logs_the_error_and_advances_once(
    controller, capsys, monkeypatch
):
    err = _track_end_stderr(
        controller, capsys, monkeypatch, RuntimeError("boom")
    )

    assert "Playback error in guild 1234: RuntimeError('boom')" in err
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

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()
        seen = _record_threads(controller, monkeypatch)
        controller._on_track_end(None, controller._generation - 1)
        await _settle()
        return seen

    seen = asyncio.run(run())

    assert seen == []
    assert _titles(controller) == ["first", "second"]
    assert voice_client.stopped == 0
    assert voice_client.is_playing()
    assert _played(controller) == ["first"]
    assert not (tmp_path / "backup" / "playlist_1234.pickle").exists()


def test_a_track_end_from_a_replaced_track_drops_its_pending_pick(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second", "third")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()
        # a skip picks "second" and stops "first"; before its hop
        # runs, the queue head - "second" - is started anew
        controller._next_song = controller.playlist.next(True)
        stale_after = _end_track(controller.guild.voice_client)
        await controller.ensure_playing()
        stale_after(None)
        await _settle()
        after = _end_track(controller.guild.voice_client)
        _in_thread(after, None)
        await _settle()

    asyncio.run(run())

    assert _played(controller) == ["first", "second", "third"]


def test_a_stop_skips_the_backup_after_the_hop(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller.stop()
        # stop() snapshots the queue itself; what follows is the hop
        seen = _record_threads(controller, monkeypatch)
        await _settle()
        return seen

    seen = asyncio.run(run())

    # the hop ran - it consumed stop()'s flag and advanced the
    # emptied queue - and wrote no backup
    assert not controller._stopping
    assert [name for name, _ in seen] == ["next"]
    assert controller.guild.voice_client.stopped == 1


def test_the_idle_timer_starts_after_the_last_track_ends(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def run():
        after = await _start_and_end_first_track(controller)
        assert controller.timer._task is None
        _in_thread(after, None)
        await _settle()
        return controller.timer._task

    timer_task = asyncio.run(run())

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


async def _saved_playlist_db(ac, guild_id, name, entries):
    """An in-memory database holding one saved playlist, built on the
    playlists and settings modules the controller itself sees."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    settings = sys.modules["musicbot.settings"]
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.connect() as conn:
        await conn.run_sync(settings.run_migrations)
    factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await ac.playlists.save(
        factory, ac.playlists.PlaylistRef(guild_id, name), entries
    )
    return engine, factory


async def _drain(controller):
    while controller._tasks:
        await asyncio.gather(*controller._tasks)


SAVED_URL = "https://example.invalid/track"


def _queue_saved_playlist_song(controller, from_saved_playlist=True):
    """Queues one song behind a playing track, stored as "stale title"
    in a saved playlist and loaded as "fresh title"; lets the preload
    run, and returns the saved playlist's entries and the queue."""
    ac = sys.modules[type(controller).__module__]
    _queue(controller, "playing")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        engine, factory = await _saved_playlist_db(
            ac,
            "1234",
            "mix",
            [ac.playlists.PlaylistEntry(SAVED_URL, "stale title")],
        )
        controller.bot.DbSession = factory
        try:
            controller.guild.voice_client.playing = True
            await controller.queue(
                [
                    ac.Song(
                        ac.SiteTypes.YT_DLP,
                        SAVED_URL,
                        title="fresh title",
                        saved_playlist=(
                            ac.playlists.PlaylistRef("1234", "mix")
                            if from_saved_playlist
                            else None
                        ),
                        saved_url=SAVED_URL if from_saved_playlist else None,
                    )
                ]
            )
            await _drain(controller)
            contents = await ac.playlists.get(
                factory, ac.playlists.PlaylistRef("1234", "mix")
            )
            return contents.entries, _titles(controller)
        finally:
            await engine.dispose()

    return asyncio.run(run())


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


def _gated_preload(stub_loader, title, outcomes):
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

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        futures = _gated_preload(stub_loader, "b", None)
        await controller.ensure_playing()
        await _settle()
        # the queue preload of "b" is pending; skipping makes "b" the
        # head, and the player's own preload of it pends too
        controller.skip()
        await _settle()
        assert len(futures) == 2
        futures[0].set_result(stub_loader.Preload.FAILED)
        await _settle()
        futures[1].set_result(stub_loader.Preload.FAILED)
        await _settle()

    asyncio.run(run())

    assert _played(controller) == ["a", "c"]


def test_a_failed_non_head_preload_still_removes_the_song(
    controller, stub_loader, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "a", "b", "c")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        futures = _gated_preload(stub_loader, "b", None)
        await controller.ensure_playing()
        await _settle()
        futures[0].set_result(stub_loader.Preload.FAILED)
        await _settle()

    asyncio.run(run())

    assert _titles(controller) == ["a", "c"]


def _announce_disconnect_with(controller, monkeypatch, during):
    """Makes the voice client's disconnect() run `during()` first."""
    from config import config

    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)
    voice_client = controller.guild.voice_client
    original = voice_client.disconnect

    async def disconnect(*, force=False):
        during()
        await asyncio.sleep(0)
        await original(force=force)

    voice_client.disconnect = disconnect


def test_a_track_end_hop_during_a_disconnect_starts_no_idle_timer(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()
        generation = controller._generation
        _announce_disconnect_with(
            controller,
            monkeypatch,
            lambda: controller._on_track_end(None, generation),
        )
        await controller.udisconnect("test")
        await _settle()
        return controller.timer._task

    assert asyncio.run(run()) is None


def test_a_disposed_controller_leaves_no_timer_running(
    controller, monkeypatch
):
    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        _announce_disconnect_with(
            controller,
            monkeypatch,
            lambda: controller.add_task(controller.timer.start()),
        )
        await controller.dispose()
        await _settle()
        return controller.timer._task

    assert asyncio.run(run()) is None


def test_a_track_end_after_a_plain_stop_starts_the_idle_timer(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "only")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()
        controller.stop()
        await _settle()
        return controller.timer._task

    assert asyncio.run(run()) is not None


def test_a_track_end_after_the_idle_timer_expired_starts_it_again(
    controller, monkeypatch
):
    from config import config

    _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(config, "VC_TIMEOUT", 0)
    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)
    controller.bot.sessions.settings(controller.guild).vc_timeout = True
    _queue(controller, "only")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()
        controller.guild.voice_client.playing = False
        await controller.timer.start()
        await _settle()
        assert controller.guild.voice_client.disconnected
        assert controller.timer._task is None
        controller._on_track_end(None, controller._generation)
        await asyncio.sleep(0)
        return controller.timer._task

    assert asyncio.run(run()) is not None


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


class _FakeCtx:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def test_a_current_song_click_with_no_current_song_replies_queue_empty(
    controller,
):
    from config import config

    ctx = _FakeCtx()

    asyncio.run(controller.current_song_callback(ctx))

    assert ctx.sent == [((config.QUEUE_EMPTY,), {})]


def test_a_current_song_click_while_a_track_plays_shows_the_song_info(
    controller,
):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    ctx = _FakeCtx()

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


async def _ret(value):
    return value


def test_the_retry_after_an_expired_token_edit_applies_the_view(controller):
    _queue(controller, "current")
    controller.guild.voice_client.playing = True
    message = _FlakyMessage(_http_exception(401, 50027))

    async def run():
        await controller.attach_view(lambda view: _ret(message))
        controller.set_volume(0)  # the buttons now differ
        await controller.update_view()

    asyncio.run(run())

    assert len(message.edits) == 1 and message.edits[0] is not None


def test_a_refresh_after_a_failed_edit_applies_the_view_again(
    controller, capsys
):
    _queue(controller, "current")
    voice = controller.guild.voice_client
    message = _FlakyMessage(_http_exception(500, 0))

    async def run():
        await controller.attach_view(lambda view: _ret(message))
        voice.playing = True
        await controller.update_view()
        await controller.update_view()

    asyncio.run(run())

    assert len(message.edits) == 1 and message.edits[0] is not None

def _rewriting_preload(stub_loader, normalised):
    """Makes every extraction rewrite the song's web page URL, as
    yt-dlp does when it normalises it or resolves a Spotify entry."""

    async def preload(song):
        song.webpage_url = normalised
        return stub_loader.Preload.EXTRACTED

    stub_loader.preload = preload


def _stored_titles_after_preloading(controller, make_song, preloads=1):
    """Queues the song `make_song(ac)` behind a playing track with
    "stale title" stored, preloads it `preloads` times, and returns
    the stored titles."""
    ac = sys.modules[type(controller).__module__]
    _queue(controller, "playing")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        engine, factory = await _saved_playlist_db(
            ac,
            "1234",
            "mix",
            [ac.playlists.PlaylistEntry(SAVED_URL, "stale title")],
        )
        controller.bot.DbSession = factory
        try:
            controller.guild.voice_client.playing = True
            song = make_song(ac)
            controller.playlist.add(song)
            for n in range(preloads):
                song.title = f"fresh title {n}"
                assert await controller._preload(song)
            contents = await ac.playlists.get(
                factory, ac.playlists.PlaylistRef("1234", "mix")
            )
            return [entry.title for entry in contents.entries]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_song_loaded_from_a_saved_playlist_keeps_its_title_refresh(
    controller, stub_loader
):
    ac = sys.modules[type(controller).__module__]
    _queue(controller, "playing")
    _rewriting_preload(stub_loader, "https://example.invalid/normalised")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        engine, factory = await _saved_playlist_db(
            ac,
            "1234",
            "mix",
            [ac.playlists.PlaylistEntry(SAVED_URL, "stale title")],
        )

        async def noop(*args, **kwargs):
            pass

        ctx = types.SimpleNamespace(
            bot=types.SimpleNamespace(DbSession=factory),
            guild=controller.guild,
            audiocontroller=controller,
            defer=noop,
            send=noop,
        )
        controller.bot.DbSession = factory
        try:
            controller.guild.voice_client.playing = True
            await Music._playlist_load.callback(Music(None), ctx, "mix")
            await _drain(controller)
            song = controller.playlist.playque[-1]
            titles = []
            for n in range(2):
                # the stream URL expired: the song is extracted again
                song.title = f"fresh title {n}"
                assert await controller._preload(song)
                contents = await ac.playlists.get(
                    factory, ac.playlists.PlaylistRef("1234", "mix")
                )
                titles.append([e.title for e in contents.entries])
            return titles
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == [["fresh title 0"], ["fresh title 1"]]


def test_a_song_without_a_saved_url_refreshes_no_stored_title(
    controller, stub_loader
):
    _rewriting_preload(stub_loader, SAVED_URL)

    def make_song(ac):
        song = ac.Song(
            ac.SiteTypes.YT_DLP,
            SAVED_URL,
            saved_playlist=ac.playlists.PlaylistRef("1234", "mix"),
        )
        # as unpickled from a backup written before saved_url existed
        song.__dict__.pop("saved_url", None)
        return song

    assert _stored_titles_after_preloading(controller, make_song) == [
        "stale title"
    ]
