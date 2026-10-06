from __future__ import annotations
import os
import re
import sys
import asyncio
import discord
import subprocess
from enum import Enum
from traceback import print_exc
from subprocess import CalledProcessError, check_output
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Iterable,
    Optional,
    Union,
    List,
)

from discord import (
    opus,
    utils,
    Emoji,
    Embed,
)
from discord.ext.commands import CommandError, NotOwner

from config import config
from musicbot.song import Song
from musicbot.linkutils import SiteTypes, url_regex

# avoiding circular import
if TYPE_CHECKING:
    from musicbot.bot import Context, MusicBot
    from musicbot.settings import GuildSettings
    from musicbot.audiocontroller import AudioController


OLD_FFMPEG_CONF = """
ffmpeg version 5.1.git Copyright (c) 2000-2022 the FFmpeg developers
built with gcc 12.1.0 (Rev2, Built by MSYS2 project)
configuration: --disable-programs --enable-ffmpeg --disable-doc\
 --enable-w32threads --enable-openssl --extra-ldflags=-static\
 --pkg-config='pkg-config --static --with-path=/usr/local/lib/pkgconfig'
libavutil      57. 33.101 / 57. 33.101
libavcodec     59. 42.102 / 59. 42.102
libavformat    59. 30.100 / 59. 30.100
libavdevice    59.  8.101 / 59.  8.101
libavfilter     8. 46.103 /  8. 46.103
libswscale      6.  8.103 /  6.  8.103
libswresample   4.  8.100 /  4.  8.100
""".strip()
FFMPEG_ZIP_URL = (
    "https://github.com/solaluset/FFmpeg"
    "/releases/latest/download/ffmpeg.zip"
)
NEWEST_FFMPEG_TIMESTAMP = 1720195398

VC_CONNECT_TIMEOUT = 10


def extract_ffmpeg_timestamp(version: str) -> int:
    version = version.split()
    if len(version) > 2:
        mo = re.search(r"-(K4|SL)_(?P<timestamp>\d+)", version[2])
        if mo:
            return int(mo.group("timestamp"))
    return None


def check_dependencies():
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    ffmpeg_output = None
    try:
        ffmpeg_output = check_output(
            ("ffmpeg", "-version"), text=True, creationflags=flags
        ).strip()
    except (FileNotFoundError, CalledProcessError) as e:
        if sys.platform == "win32":
            print("Downloading FFmpeg...")
            download_ffmpeg()
        else:
            raise RuntimeError("ffmpeg was not found") from e
    if sys.platform == "win32" and ffmpeg_output:
        ffmpeg_timestamp = extract_ffmpeg_timestamp(ffmpeg_output)
        if ffmpeg_output == OLD_FFMPEG_CONF or (
            ffmpeg_timestamp and ffmpeg_timestamp < NEWEST_FFMPEG_TIMESTAMP
        ):
            print("Updating FFmpeg...")
            download_ffmpeg()

    try:
        opus.Encoder.get_opus_version()
    except opus.OpusNotLoaded as e:
        raise RuntimeError("opus was not found") from e


def download_ffmpeg():
    from io import BytesIO
    from ssl import SSLContext
    from zipfile import ZipFile
    from urllib.request import urlopen

    stream = urlopen(
        FFMPEG_ZIP_URL,
        context=SSLContext(),
    )
    total_size = int(stream.getheader("content-length") or 0)
    file = BytesIO()
    if total_size:
        BLOCK_SIZE = 1024 * 1024

        data = stream.read(BLOCK_SIZE)
        received_size = BLOCK_SIZE
        percentage = 0
        print("0%", end="")
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


ASSETS_PATH = os.path.join(
    getattr(
        sys,
        "_MEIPASS",
        os.path.dirname(os.path.abspath(sys.argv[0] or "dummy")),
    ),
    "assets",
)


def asset(name: str) -> str:
    return os.path.join(ASSETS_PATH, name)


class CheckError(CommandError):
    pass


def get_audiocontroller(ctx: Context) -> "AudioController":
    """This guild's AudioController, or a CheckError explaining that
    the bot is still starting.

    on_ready registers a controller for every guild, but that loop
    awaits a network call per guild, and only prefix commands wait for
    it to finish (process_commands awaits absolutely_ready).
    Application commands and component interactions are dispatched
    straight to the command, so they can land in that gap. Indexing
    the controllers directly turned that into a bare KeyError
    traceback with nothing sent back to the user."""
    controller = ctx.bot.sessions.controller(ctx.guild)
    if controller is None:
        raise CheckError(config.BOT_NOT_READY)
    return controller


def get_settings(ctx: Context) -> "GuildSettings":
    """This guild's GuildSettings, or a CheckError explaining that the
    bot is still starting.

    The twin of get_audiocontroller(), and needed for the same reason:
    until on_ready has loaded a guild's settings, every path that
    could find its controller missing finds its settings missing
    too. Guarding only the controller just moved the
    KeyError one line down - play_check() reads settings immediately
    after Music.cog_check() has resolved the controller."""
    sett = ctx.bot.sessions.settings(ctx.guild)
    if sett is None:
        raise CheckError(config.BOT_NOT_READY)
    return sett


async def dj_check(ctx: Context):
    """Check if the user has DJ permissions"""
    if ctx.channel.permissions_for(ctx.author).administrator:
        return True
    owner = await ctx.bot.is_owner(ctx.author)
    if owner:
        return True

    sett = get_settings(ctx)
    if sett.dj_role:
        if int(sett.dj_role) not in [r.id for r in ctx.author.roles]:
            raise CheckError(config.NOT_A_DJ)
    return True


async def owner_check(ctx: Context):
    """Check if the user is the owner of the bot"""
    if ctx.author.id in config.EXTRA_OWNERS:
        return True
    owner = await ctx.bot.is_owner(ctx.author)
    if owner:
        return True
    raise NotOwner("You do not own this bot.")


async def voice_check(ctx: Context):
    """Check if the user can use the bot now. Only refuses: a bot
    sitting in a channel with only bots admits the user, and
    join_voice() moves it later."""
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
            return True

    try:
        if await dj_check(ctx):
            # DJs and admins can always run commands
            return True
    except CheckError:
        pass

    raise CheckError(config.USER_NOT_IN_VC_MESSAGE)


def joins_voice(func):
    """Marks a command as a joining command (GLOSSARY.md): one that
    starts playback, so it brings the bot into the invoker's voice
    channel, or moves it there, before it runs. Opt-in: a command
    without it never joins. Place it below the command decorator,
    like a check."""
    func.__joins_voice__ = True
    return func


def join_needed(ctx: Context) -> bool:
    """Whether the bot has to join the user's voice channel before a
    music command or a search pick runs: it has no voice client (a
    connect), or user_must_be_in_vc is on, its channel has only bots
    and the user is in another one (a move). Only a joining command
    ever needs to: when ctx.command is set but not marked with
    @joins_voice, this is False and the command answers without
    touching voice. A button's context has no command, so this reads
    only the voice state; a button decides in its own code whether to
    ask at all."""
    command = getattr(ctx, "command", None)
    if command is not None and not getattr(
        command.callback, "__joins_voice__", False
    ):
        return False
    bot_vc = ctx.guild.voice_client
    if not bot_vc:
        return True
    if not get_settings(ctx).user_must_be_in_vc:
        return False
    author_voice = ctx.author.voice
    return (
        author_voice is not None
        and author_voice.channel != bot_vc.channel
        and all(m.bot for m in bot_vc.channel.members)
    )


def check_voice_permissions(guild: discord.Guild, channel):
    perms = channel.permissions_for(guild.me)
    if not perms.connect or not perms.speak:
        raise CheckError(config.VOICE_PERMISSIONS_MISSING)


async def connect_to(guild: discord.Guild, channel):
    """Connects the bot to `channel`, or moves it there when it is
    already in voice."""
    check_voice_permissions(guild, channel)
    bot_vc = guild.voice_client
    if bot_vc:
        await bot_vc.move_to(channel)
        # to avoid ClientException: Not connected to voice
        await asyncio.sleep(1)
    else:
        await channel.connect(reconnect=True, timeout=VC_CONNECT_TIMEOUT)


def _check_command_channel(ctx: Context, sett):
    """Refuses a command or button used outside the guild's command
    channel, when one is set."""
    cm_channel = sett.command_channel
    if cm_channel is not None and int(cm_channel) != ctx.channel.id:
        raise CheckError(config.WRONG_CHANNEL_MESSAGE)


async def play_check(ctx: Context):
    """Refuses a music command or a search pick that may not run.
    Never touches voice: join_voice() does that once every check has
    passed. Player buttons use player_check() instead."""

    sett = get_settings(ctx)
    _check_command_channel(ctx, sett)

    if join_needed(ctx):
        if not ctx.author.voice:
            raise CheckError(config.USER_NOT_IN_VC_MESSAGE)
        check_voice_permissions(ctx.guild, ctx.author.voice.channel)

    if ctx.guild.voice_client and sett.user_must_be_in_vc:
        return await voice_check(ctx)

    return True


async def player_check(ctx: Context):
    """Refuses a player button that may not run. A player button
    never joins voice, so it is refused while the bot has no voice
    client (a stale player view), and never by play_check()'s join
    refusals; the command channel and voice_check() still apply."""
    if not ctx.guild.voice_client:
        raise CheckError(config.NOT_CONNECTED_MESSAGE)

    sett = get_settings(ctx)
    _check_command_channel(ctx, sett)

    if sett.user_must_be_in_vc:
        return await voice_check(ctx)

    return True


async def join_voice(ctx: Context):
    """Connects or moves the bot to the user's voice channel when
    join_needed() says so, and does nothing otherwise. A failed
    connect or move is logged and becomes a CheckError, so it reaches
    on_command_error (or a button's refusal) like any other."""
    if not join_needed(ctx):
        return
    author_voice = ctx.author.voice
    if not author_voice:
        # the user left voice since the check ran
        raise CheckError(config.USER_NOT_IN_VC_MESSAGE)
    try:
        await connect_to(ctx.guild, author_voice.channel)
    except (asyncio.TimeoutError, discord.ClientException):
        print_exc(file=sys.stderr)
        raise CheckError(config.VOICE_CONNECT_FAILED)


def get_emoji(bot: MusicBot, string: str) -> Optional[Union[str, Emoji]]:
    if string.isdecimal():
        return utils.get(bot.emojis, id=int(string))
    return string


def songs_embed(title: str, songs: Iterable[Song]) -> Embed:
    embed = Embed(
        title=title,
        color=config.EMBED_COLOR,
    )

    for counter, song in enumerate(songs, start=1):
        song_title = song.title or url_regex.fullmatch(song.webpage_url).group(
            "bare"
        )
        # file:// isn't a scheme Discord renders as a clickable link -
        # show plain title text instead of a dead-looking markdown
        # link (matches Song.format_output()'s handling of this).
        value = (
            song_title
            if song.host == SiteTypes.LOCAL_LIBRARY
            else "[{}]({})".format(song_title, song.webpage_url)
        )
        embed.add_field(
            name=f"{counter}.",
            value=value,
            inline=False,
        )

    return embed


def chunks(lst: list, n: int) -> List[list]:
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


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
        task = asyncio.current_task()
        await asyncio.sleep(config.VC_TIMEOUT)
        self.triggered = True
        try:
            await self._callback()
        finally:
            self.triggered = False
            # start(restart=True) may have already replaced self._task
            # with a new job while this one was being cancelled; only
            # clear it if it's still ours to clear
            if self._task is task:
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
        """Drop the pending timeout.

        Never cancels the task it is running inside. The inactivity
        path is _job -> timeout_handler() -> udisconnect(), and
        udisconnect() cancels this timer partway through its teardown -
        so self._task is the *current* task there. Cancelling it raised
        CancelledError at udisconnect()'s next await, which is inside
        the disconnect announcement, and `except Exception` does not
        catch it (CancelledError is a BaseException since 3.8) - so
        voice_client.disconnect() never ran and the bot stayed sitting
        in the channel after every inactivity timeout. By that point
        the timer has already fired and there is nothing pending to
        cancel; only clearing the reference matters."""
        # current_task() before dropping the reference, and tolerant
        # of there being no running loop: it raises RuntimeError off
        # the loop, which would otherwise clear self._task while
        # leaving the real task alive and still due to fire. No caller
        # does that today: discord.py's audio thread only hops to the
        # loop, and next_song() and add_task() are loop-only.
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        task, self._task = self._task, None
        if task is not None and task is not current:
            task.cancel()


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


def wrap_stdio():
    """Wraps sys.stdout/sys.stderr in OutputWrapper. A second call does
    nothing."""
    if not isinstance(sys.stdout, OutputWrapper):
        sys.stdout = OutputWrapper(sys.stdout)
    if not isinstance(sys.stderr, OutputWrapper):
        sys.stderr = OutputWrapper(sys.stderr)


class SimplePaginator(discord.ui.View):
    def __init__(self, pages: List[Embed], timeout: int = 60):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current_page = 0

    async def send(self, ctx: Context):
        if not self.pages:
            return
        self.message = await ctx.send(embed=self.pages[0], view=self)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.grey)
    async def prev_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(
                embed=self.pages[self.current_page]
            )

    @discord.ui.button(label="Next", style=discord.ButtonStyle.grey)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            await interaction.response.edit_message(
                embed=self.pages[self.current_page]
            )
