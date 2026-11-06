"""A SongError reaches the user as its own message: from d!playlist
add_song, and, as a backstop, from the command error handler."""

import types

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot.bot import MusicBot
from musicbot.commands.music import Music
from musicbot.loader import SongError
from musicbot.utils import CheckError
from musicbot import bot as bot_module
from musicbot.commands import music as music_module
from discord_fakes import RecordingCtx
from helpers import run


def make_music_bot():
    return MusicBot([], command_prefix="d!", intents=discord.Intents.default())


def db_ctx():
    return RecordingCtx(types.SimpleNamespace(DbSession=object()))


def test_add_song_replies_with_the_song_error_and_saves_nothing(monkeypatch):
    add_calls = []

    async def load_song(track):
        raise SongError("unsupported link")

    async def add_songs(*args):
        add_calls.append(args)

    monkeypatch.setattr(music_module.loader, "load_song", load_song)
    monkeypatch.setattr(music_module.playlists, "add_songs", add_songs)
    group = next(c for c in Music(None).get_commands() if c.name == "playlist")
    command = group.get_command("add_song")
    ctx = db_ctx()
    run(command.callback(Music(None), ctx, "mine", "bad link"))
    assert [str(m) for m in ctx.sent] == ["unsupported link"]
    assert add_calls == []
    assert config.PLAYLIST_UPDATED not in ctx.sent


def _hybrid(error):
    return commands.HybridCommandError(
        app_commands.CommandInvokeError(
            types.SimpleNamespace(name="play"), error
        )
    )


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(commands.CommandInvokeError, id="prefix"),
        pytest.param(_hybrid, id="hybrid"),
    ],
)
def test_command_error_handler_answers_a_song_error(monkeypatch, wrap):
    printed = []
    monkeypatch.setattr(
        bot_module, "print_exception", lambda e: printed.append(e)
    )
    error = SongError("unsupported link")
    wrapped = wrap(error)

    async def scenario():
        bot = make_music_bot()
        ctx = db_ctx()
        await bot.on_command_error(ctx, wrapped)
        return ctx.sent

    sent = run(scenario())
    assert [str(m) for m in sent] == ["unsupported link"]
    assert printed == []


def test_command_error_handler_replies_with_a_check_refusal(monkeypatch):
    printed = []
    monkeypatch.setattr(
        bot_module, "print_exception", lambda e: printed.append(e)
    )

    async def scenario():
        bot = make_music_bot()
        ctx = db_ctx()
        await bot.on_command_error(
            ctx, CheckError(config.USER_MISSING_PERMISSIONS)
        )
        return ctx.sent

    sent = run(scenario())
    assert [str(m) for m in sent] == [config.USER_MISSING_PERMISSIONS]
    assert printed == []
