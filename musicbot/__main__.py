import logging
import sys
from traceback import print_exc

import discord
from discord.ext import commands

from config import config
from musicbot.bot import MusicBot
from musicbot.utils import check_dependencies


# discord.py's phrasing for "your code raised and I caught it" -
# every such call site starts its message this way (View items and
# modals, event handlers, prefix and app commands, background tasks,
# cog unload). Those tracebacks are the only record of the crash
# anywhere, so they are the ones worth keeping.
_KEEP_TRACEBACK = ("Ignoring exception", "Unhandled exception")


class _NoTracebackFormatter(logging.Formatter):
    """One line per record, except where the traceback is the point.

    Level cannot make this cut: discord.py logs every traceback it
    carries at ERROR (or DEBUG, which a WARNING threshold drops
    anyway), so keying on ERROR would strip nothing at all. The noise
    worth losing - "Attempting a reconnect in %.2fs", "Disconnected
    from voice... Reconnecting", a failed ffmpeg probe - is ERROR
    exactly like a crash in one of our own callbacks is. What
    separates them is the message, not the level."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.msg if isinstance(record.msg, str) else ""
        if msg.startswith(_KEEP_TRACEBACK):
            return super().format(record)
        # blanked around the call rather than dropped, since the
        # record belongs to the logging machinery, not to us
        exc_info, exc_text, stack_info = (
            record.exc_info,
            record.exc_text,
            record.stack_info,
        )
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text
            record.stack_info = stack_info


initial_extensions = [
    "musicbot.commands.music",
    "musicbot.commands.general",
    "musicbot.commands.developer",
]


intents = discord.Intents.default()
intents.voice_states = True
if config.BOT_PREFIX:
    intents.message_content = True
    prefix = config.BOT_PREFIX
else:
    prefix = " "  # messages can't start with space
if config.MENTION_AS_PREFIX:
    prefix = commands.when_mentioned_or(prefix)

if config.ENABLE_BUTTON_PLUGIN:
    intents.message_content = True
    initial_extensions.append("musicbot.plugins.button")

if config.ENABLE_LOCAL_LIBRARY:
    initial_extensions.append("musicbot.commands.library")

bot = MusicBot(
    initial_extensions=initial_extensions,
    command_prefix=prefix,
    case_insensitive=True,
    status=discord.Status.online,
    activity=discord.Game(name=config.STATUS_TEXT),
    intents=intents,
    allowed_mentions=discord.AllowedMentions.none(),
)


if __name__ == "__main__":
    # A log line must never be the thing that breaks a command. Console
    # output now carries library metadata - artist, album and track
    # names - and stdout is a pipe under run.py (as it is whenever it's
    # redirected to a file), which on Windows encodes with the locale
    # codec rather than UTF-8. One track title outside cp1252 would
    # otherwise raise UnicodeEncodeError out of print(), and in
    # queue_songs() that lands in the blanket `except Exception` and
    # fails the whole queue action. Only the error handling is relaxed,
    # not the encoding, so run.py's parent still decodes what it reads.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="backslashreplace")

    check_dependencies()
    config.warn_unknown_vars()

    # if "--run" in sys.argv:
    #     shutdown_task = bot.loop.create_task(read_shutdown())

    discord_log_formatter = _NoTracebackFormatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        "%Y-%m-%d %H:%M:%S",
        style="{",
    )

    try:
        bot.run(
            config.BOT_TOKEN,
            reconnect=True,
            log_level=logging.WARNING,
            log_formatter=discord_log_formatter,
        )
    except discord.LoginFailure:
        print_exc(file=sys.stderr)
        print(
            "Set the correct token in the .env file (BOT_TOKEN=your_token)",
            file=sys.stderr,
        )
        sys.exit(1)
    except RuntimeError as e:
        if e.args != ("Event loop is closed",):
            raise
