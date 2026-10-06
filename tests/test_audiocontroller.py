"""AudioController, built with no Discord connection.

Every test here goes through the `controller` fixture (tests/conftest.py,
built on tests/controller_seam.py): a real AudioController over a stub
musicbot.loader, a fake bot, guild and voice client, with the working
directory set to tmp_path so the controller's backup/ lands there.
"""

import asyncio
import pickle
import sys
from pathlib import Path

from controller_seam import StubLoader, controller_seam


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
    assert asyncio.run(stub_loader.preload(song, controller.bot)) is True
    assert stub_loader.calls == [("preload", (song, controller.bot))]


def test_seam_leaves_no_stub_and_no_backup_behind(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(tmp_path)
    before = sys.modules.get("musicbot.loader")

    with controller_seam(tmp_path / "work") as (built, _):
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
    assert not (repo / "backup").exists()


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


def test_after_a_stop_track_changes_no_longer_refresh_the_backup(
    controller, monkeypatch, tmp_path
):
    # today's behaviour, defect included: _stopping is a one-way
    # latch (#25), so the backup stays frozen at the d!stop snapshot
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
    assert _backup(tmp_path) == ["first"]


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

    assert asyncio.run(run()) is True
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

    assert asyncio.run(run()) is False
    assert controller.guild.voice_client.played == []


def test_restore_during_playback_disconnects(
    controller, monkeypatch, tmp_path
):
    # today's behaviour, defect included (#24): restore() starts the
    # head of the queue without checking that something is already
    # playing, discord.py refuses, and the controller disconnects
    from config import config

    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "first", "second")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        controller._pickle_playlist()
        await controller.restore()
        await _settle()

    asyncio.run(run())

    assert controller.guild.voice_client.disconnected is True
    assert _titles(controller) == []


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
    import concurrent.futures

    from config import config

    monkeypatch.setattr(config, "ANNOUNCE_DISCONNECT", False)

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.timer.start()
        timer_task = controller.timer._task
        controller.add_task(asyncio.sleep(3600))
        (pending_task,) = controller._tasks
        # what add_task() records when called off the loop's thread
        pending_future = concurrent.futures.Future()
        controller._tasks.add(pending_future)

        await controller.dispose()
        await _settle()
        return timer_task, pending_task, pending_future

    timer_task, pending_task, pending_future = asyncio.run(run())

    assert timer_task.cancelled()
    assert pending_task.cancelled()
    assert pending_future.cancelled()
    assert controller.guild.voice_client.disconnected
