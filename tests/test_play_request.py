"""d!play and d!playnext share one request path: they differ only in
whether the track is queued as the next one."""

import asyncio
import types

import pytest

from config import config
from musicbot.commands.music import Music
from discord_fakes import RecordingCtx


COMMANDS = [("_play", False), ("_play_next", True)]


def run_command(name, ctx, monkeypatch, **kwargs):
    reached = []

    async def play_song(self, ctx, track, playnext=False):
        reached.append((track, playnext))

    monkeypatch.setattr(Music, "_play_song", play_song)
    command = getattr(Music, name)
    asyncio.run(command.callback(Music(None), ctx, **kwargs))
    return reached


@pytest.mark.parametrize("name,playnext", COMMANDS)
def test_no_track_and_no_file_replies_args_missing(
    name, playnext, monkeypatch
):
    ctx = RecordingCtx()
    reached = run_command(name, ctx, monkeypatch)
    assert ctx.sent == [config.PLAY_ARGS_MISSING]
    assert reached == []


@pytest.mark.parametrize("name,playnext", COMMANDS)
def test_a_track_reaches_the_play_path(name, playnext, monkeypatch):
    ctx = RecordingCtx()
    reached = run_command(name, ctx, monkeypatch, track="song")
    assert reached == [("song", playnext)]
    assert ctx.deferred


@pytest.mark.parametrize("name,playnext", COMMANDS)
def test_a_slash_file_is_played_by_its_url(name, playnext, monkeypatch):
    ctx = RecordingCtx()
    ctx.interaction = object()
    file = types.SimpleNamespace(url="https://cdn/a.mp3")
    reached = run_command(name, ctx, monkeypatch, file=file)
    assert reached == [("https://cdn/a.mp3", playnext)]
