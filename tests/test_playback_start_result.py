"""`Playback.start` (through the controller's `play_song`) returns
whether it handed the song to the voice client."""

import asyncio

from controller_helpers import (
    _fake_ffmpeg,
    _queue,
    _run,
    _settle,
    _song,
)


def _start_held(controller, stub_loader, monkeypatch, result=None):
    """Queues one song and starts it with its preload held. Returns
    (release event, the start's task)."""
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "song")
    song = controller.playlist[0]
    release = stub_loader.hold(song, result)
    task = asyncio.ensure_future(controller.play_song(song))
    return release, task


async def _release(release, task):
    await _settle()
    release.set()
    await _settle()
    return await task


def test_start_returns_true_when_the_song_reaches_the_voice_client(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "song")

    async def scenario():
        return await controller.play_song(controller.playlist[0])

    assert _run(controller, scenario) is True
    assert len(controller.guild.voice_client.played) == 1


def test_start_returns_false_for_a_failed_preload(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task = _start_held(
            controller,
            stub_loader,
            monkeypatch,
            stub_loader.Preload.FAILED,
        )
        controller.command_channel = types_channel()
        return await _release(release, task)

    assert _run(controller, scenario) is False
    assert controller.guild.voice_client.played == []


def types_channel():
    class Channel:
        async def send(self, *args, **kwargs):
            pass

    return Channel()


def test_start_returns_false_for_a_song_without_a_direct_url(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    song = _song(controller, "no url")
    song.url = None
    controller.playlist.add(song)

    async def scenario():
        return await controller.play_song(song)

    assert _run(controller, scenario) is False
    assert controller.guild.voice_client.played == []


def test_start_returns_false_when_a_stop_overtakes_it(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task = _start_held(controller, stub_loader, monkeypatch)
        await _settle()
        controller.stop()
        return await _release(release, task)

    assert _run(controller, scenario) is False
    assert controller.guild.voice_client.played == []


def test_start_returns_false_when_a_disconnect_overtakes_it(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task = _start_held(controller, stub_loader, monkeypatch)
        await _settle()
        await controller.udisconnect("test")
        return await _release(release, task)

    assert _run(controller, scenario) is False
    assert controller.guild.voice_client is None or (
        controller.guild.voice_client.played == []
    )


def test_start_returns_false_when_a_newer_start_overtakes_it(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task = _start_held(controller, stub_loader, monkeypatch)
        await _settle()
        newer = _song(controller, "newer")
        controller.playlist.add(newer)
        second = asyncio.ensure_future(controller.play_song(newer))
        await _settle()
        first = await _release(release, task)
        return first, await second

    assert _run(controller, scenario) == (False, True)


def test_start_returns_false_when_the_bot_leaves_voice_during_it(
    controller, stub_loader, monkeypatch
):
    async def scenario():
        release, task = _start_held(controller, stub_loader, monkeypatch)
        await _settle()
        voice_client = controller.guild.voice_client
        controller.guild.voice_client = None
        result = await _release(release, task)
        return result, voice_client

    result, voice_client = _run(controller, scenario)
    assert result is False
    assert voice_client.played == []


def test_start_returns_false_when_play_raises(controller, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _queue(controller, "song")
    controller.guild.voice_client.playing = True

    async def scenario():
        return await controller.play_song(controller.playlist[0])

    assert _run(controller, scenario) is False
