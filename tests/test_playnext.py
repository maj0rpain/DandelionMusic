"""d!playnext: the track is placed in the "next" slot as it is queued,
so a command that resolves at the same moment cannot take its place.

Queue order is what the user sees in d!queue: titles by index.
"""

import asyncio
import pickle
import sys
import types

import pytest

from musicbot.commands.music import Music


def _song(controller, title):
    ac = sys.modules[type(controller).__module__]
    return ac.Song(
        ac.SiteTypes.YT_DLP,
        f"https://example.invalid/{title}",
        url=f"https://stream.invalid/{title}",
        title=title,
    )


def _queue(controller, *titles):
    for title in titles:
        controller.playlist.add(_song(controller, title))


def _titles(controller):
    return [s.title for s in controller.playlist.playque]


def _backup(tmp_path):
    with open(tmp_path / "backup" / "playlist_1234.pickle", "rb") as f:
        return [s.title for s in pickle.load(f).playque]


def _preloaded(stub_loader):
    return [a[0].title for n, a in stub_loader.calls if n == "preload"]


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


def _run(controller, coro_fn):
    controller.guild.voice_client.playing = True

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        result = await coro_fn()
        await _settle()
        return result

    return asyncio.run(run())


def _suspended_loads(controller, stub_loader):
    """Makes load_song wait on a per-track future the test resolves."""
    futures = {}

    async def load_song(track):
        future = asyncio.get_running_loop().create_future()
        futures[track] = future
        return await future

    stub_loader.load_song = load_song
    return futures


@pytest.mark.parametrize("plain_first", [True, False])
def test_interleaved_playnext_and_plain_keep_their_slots(
    controller, stub_loader, plain_first
):
    _queue(controller, "current", "a", "b")
    futures = _suspended_loads(controller, stub_loader)

    async def go():
        pn = asyncio.ensure_future(
            controller.process_song("pn", playnext=True)
        )
        plain = asyncio.ensure_future(controller.process_song("plain"))
        await _settle()
        order = ["plain", "pn"] if plain_first else ["pn", "plain"]
        for track in order:
            futures[track].set_result(_song(controller, track))
            await _settle()
        await asyncio.gather(pn, plain)

    _run(controller, go)

    assert _titles(controller) == ["current", "pn", "a", "b", "plain"]


def test_playnext_goes_to_index_1_and_refreshes_backup_and_preload_once(
    controller, stub_loader, tmp_path
):
    _queue(controller, "current", "a", "b")
    stub_loader.results["load_song"] = _song(controller, "x")

    _run(controller, lambda: controller.process_song("x", playnext=True))

    assert _titles(controller) == ["current", "x", "a", "b"]
    assert _backup(tmp_path) == ["current", "x", "a", "b"]
    assert _preloaded(stub_loader) == ["x", "a", "b"]


def test_playnext_with_only_the_current_track(controller, stub_loader):
    _queue(controller, "current")
    stub_loader.results["load_song"] = _song(controller, "x")

    _run(controller, lambda: controller.process_song("x", playnext=True))

    assert _titles(controller) == ["current", "x"]


def test_playnext_on_an_empty_queue_starts_the_track(
    controller, stub_loader, monkeypatch
):
    from test_audiocontroller import _fake_ffmpeg

    _fake_ffmpeg(monkeypatch)
    stub_loader.results["load_song"] = _song(controller, "x")

    async def go():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.process_song("x", playnext=True)
        await _settle()

    asyncio.run(go())

    assert _titles(controller) == ["x"]
    played = controller.guild.voice_client.played
    assert [s.original.url for s in played] == ["https://stream.invalid/x"]


def test_playnext_one_item_playlist_goes_next(controller, stub_loader):
    _queue(controller, "current", "a")
    stub_loader.results["load_song"] = [_song(controller, "x")]

    _run(controller, lambda: controller.process_song("x", playnext=True))

    assert _titles(controller) == ["current", "x", "a"]


def test_playnext_multi_track_playlist_is_appended(controller, stub_loader):
    _queue(controller, "current", "a")
    stub_loader.results["load_song"] = [
        _song(controller, "x"),
        _song(controller, "y"),
    ]

    result = _run(
        controller, lambda: controller.process_song("x", playnext=True)
    )

    assert _titles(controller) == ["current", "a", "x", "y"]
    assert result is sys.modules[type(controller).__module__].PLAYLIST


@pytest.mark.parametrize("failure", ["none", "error"])
def test_unsupported_or_unloadable_playnext_leaves_everything_alone(
    controller, stub_loader, tmp_path, failure
):
    _queue(controller, "current", "a")
    if failure == "none":
        stub_loader.results["load_song"] = None
    else:

        async def load_song(track):
            raise stub_loader.SongError("nope")

        stub_loader.load_song = load_song

    async def go():
        if failure == "error":
            with pytest.raises(stub_loader.SongError):
                await controller.process_song("x", playnext=True)
        else:
            assert await controller.process_song("x", playnext=True) is None

    _run(controller, go)

    assert _titles(controller) == ["current", "a"]
    assert not (tmp_path / "backup" / "playlist_1234.pickle").exists()
    assert _preloaded(stub_loader) == []


def test_play_song_helper_keeps_playnext_slot_when_a_reply_interleaves(
    controller, stub_loader, monkeypatch
):
    _queue(controller, "current", "a")
    songs = {
        "pn": _song(controller, "pn"),
        "plain": _song(controller, "plain"),
    }

    async def load_song(track):
        return songs[track]

    stub_loader.load_song = load_song

    class Timer:
        async def start(self, *_):
            pass

    controller.timer = Timer()
    sent = []

    async def send(message=None, **kwargs):
        sent.append(message)
        if len(sent) == 1:
            await controller.process_song("plain")

    ctx = types.SimpleNamespace(
        audiocontroller=controller,
        send=send,
        author=None,
        guild=controller.guild,
    )
    music = Music(None)
    monkeypatch.setattr(
        "musicbot.commands.music.utils.get_settings",
        lambda c: types.SimpleNamespace(announce_songs=False),
    )

    async def go():
        await music._play_song(ctx, "pn", playnext=True)

    _run(controller, go)

    assert _titles(controller) == ["current", "pn", "a", "plain"]
