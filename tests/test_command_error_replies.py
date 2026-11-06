"""The command error handler classifies each error: an unknown command
is ignored, bad input and a failed check get a reply without a
traceback, anything else a reply and a traceback."""

import types

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot import bot as bot_module
from musicbot.bot import MusicBot
from musicbot.loader import SongError
from musicbot.utils import CheckError
from discord_fakes import RecordingCtx
from helpers import run


def make_music_bot():
    return MusicBot([], command_prefix="d!", intents=discord.Intents.default())


def _hybrid(error):
    return commands.HybridCommandError(
        app_commands.CommandInvokeError(
            types.SimpleNamespace(name="play"), error
        )
    )


def _app(error):
    return app_commands.CommandInvokeError(
        types.SimpleNamespace(name="play"), error
    )


WRAPS = [
    pytest.param(lambda e: e, id="bare"),
    pytest.param(commands.CommandInvokeError, id="prefix"),
    pytest.param(_hybrid, id="hybrid"),
    pytest.param(_app, id="app"),
]


async def _noop(ctx, name: str, count: int):
    pass


def _command(callback=_noop):
    return commands.Command(callback, name="seek")


def _bare_command():
    async def seek(ctx):
        pass

    return commands.Command(seek, name="seek")


def _missing():
    return commands.MissingRequiredArgument(
        _command().clean_params["name"]
    )


def _literal():
    return commands.BadLiteralArgument(
        _command().clean_params["name"], ["a", "b"], ValueError("x"), None
    )


def _usage(command, prefix="d!"):
    return f"\nUsage: {prefix}seek {command.signature}"


# (error factory, sends the message, usage, prints)
IGNORED = [lambda: commands.CommandNotFound('Command "x" is not found')]
USER_ERRORS = [
    _missing,
    lambda: commands.BadArgument("bad value"),
    _literal,
]
REFUSALS = [
    lambda: commands.CheckFailure("check failed"),
    lambda: commands.NotOwner("not owner"),
    lambda: CheckError(config.USER_MISSING_PERMISSIONS),
]


def handle(error, command=None, prefix="d!"):
    printed = []
    real = bot_module.print_exception
    bot_module.print_exception = lambda e: printed.append(e)
    try:

        async def scenario():
            bot = make_music_bot()
            ctx = RecordingCtx(types.SimpleNamespace(DbSession=object()))
            if command is not None:
                ctx.command = command
                ctx.prefix = prefix
            await bot.on_command_error(ctx, error)
            return ctx.sent

        sent = run(scenario())
    finally:
        bot_module.print_exception = real
    return [str(m) for m in sent], printed


@pytest.mark.parametrize("wrap", WRAPS)
@pytest.mark.parametrize("make", IGNORED)
def test_unknown_command_gets_no_reply_and_no_log(wrap, make):
    assert handle(wrap(make())) == ([], [])


@pytest.mark.parametrize("wrap", WRAPS)
@pytest.mark.parametrize("make", USER_ERRORS)
def test_user_error_replies_with_message_and_usage(wrap, make):
    error = make()
    command = _command()
    sent, printed = handle(wrap(error), command)
    assert sent == [str(error) + _usage(command)]
    assert printed == []


@pytest.mark.parametrize("make", USER_ERRORS)
def test_user_error_without_a_command_replies_with_message_alone(make):
    error = make()
    assert handle(error) == ([str(error)], [])


def test_usage_has_no_trailing_space_for_an_empty_signature():
    error = commands.BadArgument("bad value")
    sent, printed = handle(error, _bare_command())
    assert sent == ["bad value\nUsage: d!seek"]
    assert printed == []


@pytest.mark.parametrize("wrap", WRAPS)
@pytest.mark.parametrize("make", REFUSALS)
def test_failed_check_replies_with_its_message(wrap, make):
    error = make()
    assert handle(wrap(error)) == ([str(error)], [])


@pytest.mark.parametrize("wrap", WRAPS)
def test_song_error_replies_with_its_message(wrap):
    assert handle(wrap(SongError("unsupported link"))) == (
        ["unsupported link"],
        [],
    )


@pytest.mark.parametrize("wrap", WRAPS)
def test_failure_is_replied_to_and_printed_as_received(wrap):
    wrapped = wrap(RuntimeError("boom"))
    sent, printed = handle(wrapped)
    assert sent == [str(wrapped)]
    assert printed == [wrapped]
