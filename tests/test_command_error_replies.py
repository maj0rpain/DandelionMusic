"""The command error handler classifies each error: an unknown command
is ignored, bad input and a failed check get a reply without a
traceback, anything else a reply and a traceback."""

import types
from typing import Callable, NamedTuple

import pytest
from discord import app_commands
from discord.ext import commands

from config import config
from musicbot import bot as bot_module
from musicbot.loader import SongError
from musicbot.utils import CheckError
from discord_fakes import RecordingCtx, real_music_bot
from helpers import run


@pytest.fixture(scope="module")
def music_bot():
    return real_music_bot()


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


def _bare(error):
    return error


WRAPS = {
    "bare": _bare,
    "prefix": commands.CommandInvokeError,
    "hybrid": _hybrid,
    "app": _app,
}


async def _seek(ctx, name: str, count: int):
    pass


async def _seek_no_args(ctx):
    pass


def _command():
    return commands.Command(_seek, name="seek")


def _no_args_command():
    return commands.Command(_seek_no_args, name="seek")


def _missing():
    return commands.MissingRequiredArgument(_command().clean_params["name"])


def _literal():
    return commands.BadLiteralArgument(
        _command().clean_params["name"],
        ["a", "b"],
        [commands.BadArgument("x")],
        None,
    )


USAGE = "\nUsage: d!seek <name> <count>"


class Row(NamedTuple):
    id: str
    make_error: Callable
    make_command: Callable | None
    expected_sends: Callable  # given the error as received
    printed: bool
    wrapped: bool = False


ROWS = [
    Row(
        "unknown-command",
        lambda: commands.CommandNotFound('Command "x" is not found'),
        None,
        lambda e: [],
        False,
    ),
    Row(
        "missing-argument",
        _missing,
        _command,
        lambda e: [str(e) + USAGE],
        False,
    ),
    Row(
        "bad-argument",
        lambda: commands.BadArgument("bad value"),
        _command,
        lambda e: ["bad value" + USAGE],
        False,
    ),
    Row(
        "bad-literal",
        _literal,
        _command,
        lambda e: [str(e) + USAGE],
        False,
    ),
    Row(
        "missing-argument-no-command",
        _missing,
        None,
        lambda e: [str(e)],
        False,
    ),
    Row(
        "bad-argument-no-command",
        lambda: commands.BadArgument("bad value"),
        None,
        lambda e: ["bad value"],
        False,
    ),
    Row(
        "bad-literal-no-command",
        _literal,
        None,
        lambda e: [str(e)],
        False,
    ),
    Row(
        "empty-signature-no-trailing-space",
        lambda: commands.BadArgument("bad value"),
        _no_args_command,
        lambda e: ["bad value\nUsage: d!seek"],
        False,
    ),
    Row(
        "check-failure",
        lambda: commands.CheckFailure("check failed"),
        None,
        lambda e: ["check failed"],
        False,
    ),
    Row(
        "not-owner",
        lambda: commands.NotOwner("not owner"),
        None,
        lambda e: ["not owner"],
        False,
    ),
    Row(
        "check-error",
        lambda: CheckError(config.USER_MISSING_PERMISSIONS),
        None,
        lambda e: [config.USER_MISSING_PERMISSIONS],
        False,
    ),
    Row(
        "song-error",
        lambda: SongError("unsupported link"),
        None,
        lambda e: ["unsupported link"],
        False,
        wrapped=True,
    ),
    Row(
        "failure",
        lambda: RuntimeError("boom"),
        None,
        lambda e: [str(e)],
        True,
        wrapped=True,
    ),
]

CASES = [
    pytest.param(row, wrap, id=f"{row.id}-{wrap}")
    for row in ROWS
    for wrap in (WRAPS if row.wrapped else ["bare"])
]


def handle(bot, error, command=None):
    printed = []
    real = bot_module.print_exception
    bot_module.print_exception = lambda e: printed.append(e)
    try:

        async def scenario():
            ctx = RecordingCtx(types.SimpleNamespace(DbSession=object()))
            if command is not None:
                ctx.command = command
                ctx.prefix = "d!"
            await bot.on_command_error(ctx, error)
            return ctx.sent

        sent = run(scenario())
    finally:
        bot_module.print_exception = real
    return [str(m) for m in sent], printed


@pytest.mark.parametrize("row, wrap", CASES)
def test_command_error_is_classified(music_bot, row, wrap):
    error = row.make_error()
    received = WRAPS[wrap](error)
    command = row.make_command() if row.make_command else None
    sent, printed = handle(music_bot, received, command)
    # a failure is replied to and printed as received, so its text is the
    # wrapped error's
    reply_to = received if row.printed else error
    assert sent == row.expected_sends(reply_to)
    assert printed == ([received] if row.printed else [])
