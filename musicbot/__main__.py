import logging
import sys
from traceback import print_exc

import discord
from discord.ext import commands

from config import config
from musicbot.bot import MusicBot
from musicbot.utils import check_dependencies


class _NoTracebackFormatter(logging.Formatter):
    """One line per record below ERROR, traceback and all.

    discord.py attaches a full traceback to routine trouble - a
    gateway reconnect, a rate limit it already handled - which is
    noise in a console. ERROR is different: uncaught exceptions in a
    View item, an event handler or a command reach discord.py's logger
    and nowhere else, so stripping those would leave a bare "Ignoring
    exception in view ..." with no file, line or exception type behind
    it, and no other record anywhere. Those keep their traceback."""

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno >= logging.ERROR:
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
