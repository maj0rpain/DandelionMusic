from __future__ import annotations
import os
import re
import sys
import asyncio
import discord
import subprocess
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
    KeyError one line down - admission.check reads settings immediately
    after resolving the controller."""
    sett = ctx.bot.sessions.settings(ctx.guild)
    if sett is None:
        raise CheckError(config.BOT_NOT_READY)
    return sett


async def _admin_or_owner(ctx: Context) -> bool:
    if ctx.channel.permissions_for(ctx.author).administrator:
        return True
    return bool(await ctx.bot.is_owner(ctx.author))


def _holds_role(ctx: Context, role_id) -> bool:
    """True when the author holds the role with this id. A missing id
    (no role set) is held by nobody."""
    if not role_id:
        return False
    return int(role_id) in [r.id for r in ctx.author.roles]


async def dj_check(ctx: Context):
    """Check if the user has DJ permissions"""
    if await _admin_or_owner(ctx):
        return True

    sett = get_settings(ctx)
    if sett.dj_role and not _holds_role(ctx, sett.dj_role):
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


def joining_command(func):
    """Marks a command as a joining command (GLOSSARY.md): one that
    may bring the bot into the invoker's voice channel, or move it
    there, when it runs or later, when a pick in a view it opened
    acts in its name. A command that opens a view whose picks may
    later join voice must carry the marker too: a pick joins through
    that command's context (a library search or browse pick) or
    through its own join() override (a search pick). Opt-in: a
    command without it never joins. Place it below the command
    decorator, like a check."""
    func._joining_command = True
    return func


def check_voice_permissions(
    guild: discord.Guild, channel: discord.VoiceChannel
):
    perms = channel.permissions_for(guild.me)
    if not perms.connect or not perms.speak:
        raise CheckError(config.VOICE_PERMISSIONS_MISSING)


async def connect_to(guild: discord.Guild, channel: discord.VoiceChannel):
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
