import logging
import sys
from traceback import print_exc

import discord
from discord.ext import commands

from config import config
from musicbot.bot import MusicBot
from musicbot.utils import check_dependencies


class _NoTracebackFormatter(logging.Formatter):
    """discord.py's own formatter appends a full traceback whenever a
    record carries exc_info/stack_info (e.g. gateway reconnect
    errors) - fine for a log file, unreadable in a console. Returning
    "" from both hooks drops that regardless of level, leaving just
    the one-line message."""

    def formatException(self, ei) -> str:
        return ""

    def formatStack(self, stack_info: str) -> str:
        return ""


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
