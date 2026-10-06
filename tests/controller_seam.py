"""Builds a real AudioController with no Discord connection.

Importing musicbot.loader is inert, but calling it is not: its
preload and load_* functions run extraction in a spawned worker that
only loader.init() starts, and reach the network from there. The seam
puts a StubLoader in sys.modules first and imports
musicbot.audiocontroller fresh against it, which keeps the tests
offline and records what the controller asks the loader for.

Everything is put back on the way out: every musicbot module imported
under the stub is dropped from sys.modules (each one could have bound
the stub), the ones that existed before are restored, and so are the
package's attributes - otherwise a later test module's
`from musicbot import loader` would get the stub.
"""

import contextlib
import importlib
import os
import sys
import types
from pathlib import Path
from typing import Iterator, Tuple


class StubLoader(types.ModuleType):
    """Stands in for musicbot.loader and records what it is asked.

    Every call lands in `calls` as (name, args). `preload` succeeds by
    default; the load_* functions return what `results` holds for
    their name, an empty value otherwise.
    """

    class SongError(Exception):
        pass

    def __init__(self):
        super().__init__("musicbot.loader")
        self.calls = []
        self.results = {"preload": True}

    def _record(self, name, *args, default=None):
        self.calls.append((name, args))
        return self.results.get(name, default)

    async def preload(self, song, bot):
        return self._record("preload", song, bot)

    async def load_song(self, track):
        return self._record("load_song", track)

    async def load_local_songs(self, tracks):
        return self._record("load_local_songs", tracks, default=[])


class FakeSettings:
    def __init__(self, default_volume=70):
        self.default_volume = default_volume
        self.announce_songs = False


class FakeBot:
    """`settings[guild]` and `loop`: what AudioController reads."""

    def __init__(self, loop=None):
        self.settings = {}
        self.loop = loop


class FakeSource:
    def __init__(self):
        self.volume = 1.0


class FakeChannel:
    def __init__(self):
        self.members = []


class FakeVoiceClient:
    """Behaves like discord.py's VoiceClient where the controller can
    tell: play() refuses while something is playing, and stop() ends
    the track and runs the `after` callback play() was given."""

    def __init__(self):
        self.playing = False
        self.stopped = 0
        self.source = FakeSource()
        self.played = []
        self.channel = FakeChannel()
        self.disconnected = False
        self._after = None

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def play(self, source, *, after=None):
        import discord

        if self.playing:
            raise discord.ClientException("Already playing audio.")
        self.played.append(source)
        self.source = source
        self.playing = True
        self._after = after

    def stop(self):
        self.stopped += 1
        self.playing = False
        after, self._after = self._after, None
        if after is not None:
            after(None)

    async def disconnect(self, *, force=False):
        self.disconnected = True


class FakeGuild:
    def __init__(self, guild_id=1234, voice_client=None):
        self.id = guild_id
        self.name = f"guild {guild_id}"
        self.voice_client = voice_client


def _musicbot_modules():
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "musicbot" or name.startswith("musicbot.")
    }


@contextlib.contextmanager
def controller_seam(workdir: Path) -> Iterator[Tuple[object, StubLoader]]:
    """Yields (controller, stub loader), run from `workdir`."""
    stub = StubLoader()
    saved_modules = _musicbot_modules()
    package = saved_modules.get("musicbot")
    saved_attrs = dict(vars(package)) if package is not None else None
    old_cwd = os.getcwd()

    workdir.mkdir(parents=True, exist_ok=True)
    try:
        sys.modules.pop("musicbot.audiocontroller", None)
        sys.modules["musicbot.loader"] = stub
        if package is not None:
            package.loader = stub
        os.chdir(workdir)
        audiocontroller = importlib.import_module("musicbot.audiocontroller")

        voice_client = FakeVoiceClient()
        guild = FakeGuild(voice_client=voice_client)
        bot = FakeBot()
        bot.settings[guild] = FakeSettings()
        yield audiocontroller.AudioController(bot, guild), stub
    finally:
        os.chdir(old_cwd)
        for name in _musicbot_modules():
            del sys.modules[name]
        sys.modules.update(saved_modules)
        if package is not None:
            for name in set(vars(package)) - set(saved_attrs):
                delattr(package, name)
            for name, value in saved_attrs.items():
                setattr(package, name, value)
