from __future__ import annotations
import re
import sys
import _thread
import asyncio
from enum import Enum
from threading import Thread
from subprocess import DEVNULL, check_call
from typing import TYPE_CHECKING, Callable, Awaitable, Optional, Union

from discord import (
    __version__ as pycord_version,
    opus,
    utils,
    Guild,
    Emoji,
)
from discord.ext.commands import CommandError
from emoji import is_emoji

from config import config

# avoiding circular import
if TYPE_CHECKING:
    from musicbot.bot import Context


def check_dependencies():
    assert pycord_version == "2.5.2", (
        "you don't have necessary version of Pycord."
        " Please install the version specified in requirements.txt"
    )
    try:
        check_call("ffmpeg --help", stdout=DEVNULL, stderr=DEVNULL, shell=True)
    except Exception as e:
        if sys.platform == "win32":
            download_ffmpeg()
        else:
            raise RuntimeError("ffmpeg was not found") from e
    try:
        opus.Encoder.get_opus_version()
    except opus.OpusNotLoaded as e:
        raise RuntimeError("opus was not found") from e


def download_ffmpeg():
    from io import BytesIO
    from ssl import SSLContext
    from zipfile import ZipFile
    from urllib.request import urlopen

    print("Downloading ffmpeg automatically...")
    stream = urlopen(
        "https://github.com/Krutyi-4el/FFmpeg/"
        "releases/download/v5.1.git/ffmpeg.zip",
        context=SSLContext(),
    )
    total_size = int(stream.getheader("content-length") or 0)
    file = BytesIO()
    if total_size:
        BLOCK_SIZE = 1024 * 1024

        data = stream.read(BLOCK_SIZE)
        received_size = BLOCK_SIZE
        percentage = -1
        while data:
            file.write(data)
            data = stream.read(BLOCK_SIZE)
            received_size += len(data)
            new_percentage = int(received_size / total_size * 100)
            if new_percentage != percentage:
                print("\r", new_percentage, "%", sep="", end="")
                percentage = new_percentage
    else:
        file.write(stream.read())
    zipf = ZipFile(file)
    filename = [
        name for name in zipf.namelist() if name.endswith("ffmpeg.exe")
    ][0]
    with open("ffmpeg.exe", "wb") as f:
        f.write(zipf.read(filename))
    print("\nSuccess!")


class CheckError(CommandError):
    pass


async def dj_check(ctx: Context):
    "Check if the user has DJ permissions"
    if ctx.channel.permissions_for(ctx.author).administrator:
        return True

    if ctx.bot.is_owner(ctx.author):
        return True

    sett = ctx.bot.settings[ctx.guild]
    if sett.dj_role:
        if int(sett.dj_role) not in [r.id for r in ctx.author.roles]:
            raise CheckError(config.NOT_A_DJ)
        return True

    raise CheckError(config.USER_MISSING_PERMISSIONS)


async def voice_check(ctx: Context):
    "Check if the user can use the bot now"
    bot_vc = ctx.guild.voice_client
    if not bot_vc:
        # the bot is free
        return True

    author_voice = ctx.author.voice
    if author_voice:
        if author_voice.channel == bot_vc.channel:
            return True

        if all(m.bot for m in bot_vc.channel.members):
            # current channel doesn't have any user in it
            return await ctx.bot.audio_controllers[ctx.guild].uconnect(
                ctx, move=True
            )

    try:
        if await dj_check(ctx):
            # DJs and admins can always run commands
            return True
    except CheckError:
        pass

    raise CheckError(config.USER_NOT_IN_VC_MESSAGE)


async def play_check(ctx: Context):
    "Prepare for music commands"

    sett = ctx.bot.settings[ctx.guild]

    cm_channel = sett.command_channel
    vc_rule = sett.user_must_be_in_vc

    if cm_channel is not None:
        if int(cm_channel) != ctx.channel.id:
            raise CheckError(config.WRONG_CHANNEL_MESSAGE)

    if not ctx.guild.voice_client:
        return await ctx.bot.audio_controllers[ctx.guild].uconnect(ctx)

    if vc_rule:
        return await voice_check(ctx)

    return True


def get_emoji(guild: Guild, string: str) -> Optional[Union[str, Emoji]]:
    if is_emoji(string):
        return string
    ids = re.findall(r"\d{15,20}", string)
    if ids:
        emoji = utils.get(guild.emojis, id=int(ids[-1]))
        if emoji:
            return emoji
    return utils.get(guild.emojis, name=string)


# StrEnum doesn't exist in Python < 3.11
class StrEnum(str, Enum):
    def __str__(self):
        return self._value_


class Timer:
    def __init__(self, callback: Callable[[], Awaitable]):
        self._callback = callback
        self._task = None
        self.triggered = False

    async def _job(self):
        await asyncio.sleep(config.VC_TIMEOUT)
        self.triggered = True
        await self._callback()
        self.triggered = False
        self._task = None

    # we need event loop here
    async def start(self, restart=False):
        if self._task:
            if restart:
                self._task.cancel()
            else:
                return
        self._task = asyncio.create_task(self._job())

    def cancel(self):
        if self._task:
            self._task.cancel()
            self._task = None


class OutputWrapper:
    log_file = None

    def __init__(self, stream):
        self.using_log_file = False
        self.stream = stream

    def write(self, text, /):
        try:
            ret = self.stream.write(text)
            if not self.using_log_file:
                self.flush()
        except Exception:
            self.using_log_file = True
            self.stream = self.get_log_file()
            ret = self.stream.write(text)
        return ret

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            self.using_log_file = True
            self.stream = self.get_log_file()

    def __getattr__(self, key):
        return getattr(self.stream, key)

    @classmethod
    def get_log_file(cls):
        if cls.log_file:
            return cls.log_file
        cls.log_file = open("log.txt", "w", encoding="utf-8")
        return cls.log_file


class ShutdownReader(Thread):
    def __init__(self):
        super().__init__(name=type(self).__name__)

    def run(self):
        try:
            line = input()
        except EOFError:
            return
        if line == "shutdown":
            _thread.interrupt_main()
