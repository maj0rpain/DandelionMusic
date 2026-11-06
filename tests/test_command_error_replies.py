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


@pytest.fixture(scope="module")
def music_bot():
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

# One row per error: (id, error factory, command factory or None, wrappable,
# expected sends given the error as received, whether it is printed).
# A row that is not wrappable runs bare only.
ROWS = [
    (
        "unknown-command",
        lambda: commands.CommandNotFound('Command "x" is not found'),
        None,
        True,
        lambda e: [],
        False,
    ),
    (
        "missing-argument",
        _missing,
        _command,
        True,
        lambda e: [str(e) + USAGE],
        False,
    ),
    (
        "bad-argument",
        lambda: commands.BadArgument("bad value"),
        _command,
        True,
        lambda e: ["bad value" + USAGE],
        False,
    ),
    (
        "bad-literal",
        _literal,
        _command,
        True,
        lambda e: [str(e) + USAGE],
        False,
    ),
    (
        "missing-argument-no-command",
        _missing,
        None,
        False,
        lambda e: [str(e)],
        False,
    ),
    (
        "bad-argument-no-command",
        lambda: commands.BadArgument("bad value"),
        None,
        False,
        lambda e: ["bad value"],
        False,
    ),
    (
        "bad-literal-no-command",
        _literal,
        None,
        False,
        lambda e: [str(e)],
        False,
    ),
    (
        "empty-signature-no-trailing-space",
        lambda: commands.BadArgument("bad value"),
        _no_args_command,
        False,
        lambda e: ["bad value\nUsage: d!seek"],
        False,
    ),
    (
        "check-failure",
        lambda: commands.CheckFailure("check failed"),
        None,
        True,
        lambda e: ["check failed"],
        False,
    ),
    (
        "not-owner",
        lambda: commands.NotOwner("not owner"),
        None,
        True,
        lambda e: ["not owner"],
        False,
    ),
    (
        "check-error",
        lambda: CheckError(config.USER_MISSING_PERMISSIONS),
        None,
        True,
        lambda e: [config.USER_MISSING_PERMISSIONS],
        False,
    ),
    (
        "song-error",
        lambda: SongError("unsupported link"),
        None,
        True,
        lambda e: ["unsupported link"],
        False,
    ),
    (
        "failure",
        lambda: RuntimeError("boom"),
        None,
        True,
        lambda e: [str(e)],
        True,
    ),
]

CASES = [
    pytest.param(row, wrap, id=f"{row[0]}-{wrap}")
    for row in ROWS
    for wrap in (WRAPS if row[3] else ["bare"])
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
    _, make, make_command, _, expected_sends, is_printed = row
    error = make()
    received = WRAPS[wrap](error)
    command = make_command() if make_command else None
    sent, printed = handle(music_bot, received, command)
    # a failure is replied to and printed as received, so its text is the
    # wrapped error's
    reply_to = received if is_printed else error
    assert sent == expected_sends(reply_to)
    assert printed == ([received] if is_printed else [])
