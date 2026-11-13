"""Controller helpers the controller test modules share.

Built for tests that go through the `controller` fixture
(tests/conftest.py, on tests/controller_seam.py).
"""

import asyncio
import inspect
import pickle
import sys
import types

from musicbot.library import LocalTrack

from discord_fakes import admission_without_join
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
    songs = [
        ac.Song(
            ac.SiteTypes.YT_DLP,
            f"https://example.invalid/{title}",
            url=f"https://stream.invalid/{title}",
            title=title,
        )
        for title in titles
    ]
    for song in songs:
        song.download_info = {"id": song.title, "url": song.url}
    return songs


def _song(controller, title):
    return _songs(controller, title)[0]


def _queue(controller, *titles):
    songs = _songs(controller, *titles)
    for song in songs:
        controller.playlist.add(song)
    return songs


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
    """Stands in for ffmpeg: records the stream it was asked to open
    and the keyword arguments it was built with."""

    def __init__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs

    def read(self):
        # one whole 16-bit stereo PCM frame (20 ms at 48 kHz)
        return b"\x01\x00" * 1920


class FakeDownload:
    """Stands in for the yt-dlp process: its stdout is the stream url of
    the download info it was started with."""

    def __init__(self, download_info):
        self.download_info = download_info
        self.stdout = download_info.get("url")
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    def wait(self):
        self.waited = True


def _recording_downloads(monkeypatch):
    """Fakes the yt-dlp process start and returns the list of every
    FakeDownload started, in order, to assert killed/waited on."""
    started = []

    def start(download_info):
        started.append(FakeDownload(download_info))
        return started[-1]

    monkeypatch.setattr(
        sys.modules["musicbot.audiocontroller"], "_start_download", start
    )
    return started


def _fake_ffmpeg(monkeypatch):
    import discord

    ac = sys.modules["musicbot.audiocontroller"]
    monkeypatch.setattr(ac, "_start_download", FakeDownload)

    fake = type("FakeFFmpeg", (_FakeFFmpeg, discord.AudioSource), {})
    monkeypatch.setattr(discord, "FFmpegPCMAudio", fake)
    # a piped song's ffmpeg reads the download's stdout as its stdin
    monkeypatch.setattr(ac, "_piped_ffmpeg", fake)


def _backup_path(controller, tmp_path):
    return tmp_path / controller.pickle_file


def _backup(controller, tmp_path):
    with open(_backup_path(controller, tmp_path), "rb") as f:
        return [song.title for song in pickle.load(f).playque]


def _save_backup(controller):
    """Writes the playlist backup and nothing else; it skips the write
    only when the queue is empty and nothing is playing."""
    controller.playback.save_backup()


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


def _no_idle_timer(controller, monkeypatch):
    async def start_idle_timer(restart=False):
        pass

    monkeypatch.setattr(controller, "start_idle_timer", start_idle_timer)


def command_ctx(
    controller=None, on_send=None, announce_songs=False, db_session=None
):
    """The fake command context for `Music` callbacks, and the list of
    its replies (what `send` was given, as strings; an embed reply is
    its title). `announce_songs` is the guild's setting of that name;
    `db_session` is the bot's database-session factory.

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
        # no join is needed; enter() returns the same controller
        admitted=admission_without_join(lambda: controller),
        send=send,
        defer=defer,
        author=None,
        guild=guild,
        bot=types.SimpleNamespace(
            DbSession=db_session,
            sessions=types.SimpleNamespace(
                settings=lambda _guild: types.SimpleNamespace(
                    announce_songs=announce_songs
                )
            ),
        ),
    )
    return ctx, sent


def _local_batch(
    controller, stub_loader, monkeypatch, loadable, picked, gate=None
):
    """Stubs the loader to load the `loadable` titles (None for any other
    uri) and returns the local batch of the `picked` titles. With a
    `gate`, a load of more than one uri waits for it."""
    by_uri = {
        f"/music/{s.title}.mp3": s for s in _songs(controller, *loadable)
    }

    async def load_local_songs(uris):
        if gate is not None and len(uris) > 1:
            await gate.wait()
        return [by_uri.get(u) for u in uris]

    monkeypatch.setattr(stub_loader, "load_local_songs", load_local_songs)
    return [LocalTrack(f"/music/{t}.mp3", f"Name {t}") for t in picked]
