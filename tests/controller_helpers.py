"""Controller helpers the controller test modules share.

Built for tests that go through the `controller` fixture
(tests/conftest.py, on tests/controller_seam.py).
"""

import asyncio
import inspect
import pickle
import sys
import types

from helpers import run


async def sleep_forever(started):
    # a task that records itself in `started`, then never finishes
    started.append(asyncio.current_task())
    await asyncio.sleep(3600)


async def _settle(controller=None):
    # lets the preload task the controller scheduled run to the end;
    # given the controller, waits until it has no task left pending
    for _ in range(10):
        await asyncio.sleep(0)
    while controller is not None and controller._tasks:
        await asyncio.gather(*controller._tasks)


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


def _song(controller, title):
    return _songs(controller, title)[0]


def _queue(controller, *titles):
    for song in _songs(controller, *titles):
        controller.playlist.add(song)


def _run(controller, scenario):
    """Runs `scenario()` on a fresh event loop the controller's bot
    uses, lets the loop settle afterwards, and returns its result."""

    async def main():
        controller.bot.loop = asyncio.get_running_loop()
        result = await scenario()
        await _settle()
        return result

    return run(main())


def _run_while_playing(controller, intent):
    """Runs `intent` (a plain callable, or one returning a coroutine,
    which is awaited) with a track already playing, then lets the
    preload it scheduled finish."""
    controller.guild.voice_client.playing = True

    async def scenario():
        result = intent()
        if inspect.isawaitable(result):
            result = await result
        return result

    return _run(controller, scenario)


class _FakeFFmpeg:
    """Stands in for ffmpeg: records the stream it was asked to open."""

    def __init__(self, url, **kwargs):
        self.url = url

    def read(self):
        return b""


def _fake_ffmpeg(monkeypatch):
    import discord

    monkeypatch.setattr(
        discord,
        "FFmpegPCMAudio",
        type("FakeFFmpeg", (_FakeFFmpeg, discord.AudioSource), {}),
    )


def _backup_path(controller, tmp_path):
    return tmp_path / controller.pickle_file


def _backup(controller, tmp_path):
    with open(_backup_path(controller, tmp_path), "rb") as f:
        return [song.title for song in pickle.load(f).playque]


def _write_backup(controller):
    """Writes the playlist backup and nothing else; it skips the write
    only when the queue is empty and nothing is playing."""
    controller.playback.queue_changed()


def _titles(controller):
    return [song.title for song in controller.playlist.playque]


def _preloaded(stub_loader, since=0):
    """The titles preloaded in the calls from index `since` on."""
    return [
        args[0].title
        for name, args in stub_loader.calls[since:]
        if name == "preload"
    ]


class ProcessController:
    """A guild-less controller whose `process_song` returns `result`, or
    raises it when it is an exception instance."""

    def __init__(self, result):
        self.result = result

    async def start_idle_timer(self, restart=False):
        pass

    async def process_song(self, track, user=None, playnext=False):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def command_ctx(controller=None, on_send=None, announce_songs=False):
    """The fake command context for `Music` callbacks, and the list of
    its replies (what `send` was given, as strings; an embed reply is
    its title). `announce_songs` is the guild's setting of that name.

    `controller` is what the callback reads from `ctx.audiocontroller`;
    `controller=None` for a command that reads no controller.
    `on_send(sent)`, when given, is awaited after each reply. The
    controller is never modified.
    """

    sent = []

    async def send(message=None, **kwargs):
        embed = kwargs.get("embed")
        sent.append(str(message) if embed is None else embed.title)
        if on_send is not None:
            await on_send(sent)

    async def defer(**kwargs):
        pass

    guild = types.SimpleNamespace(id=1)
    if controller is not None:
        guild = getattr(controller, "guild", None) or guild
    ctx = types.SimpleNamespace(
        audiocontroller=controller,
        send=send,
        defer=defer,
        author=None,
        guild=guild,
        bot=types.SimpleNamespace(
            DbSession=None,
            sessions=types.SimpleNamespace(
                settings=lambda _guild: types.SimpleNamespace(
                    announce_songs=announce_songs
                )
            ),
        ),
    )
    return ctx, sent
