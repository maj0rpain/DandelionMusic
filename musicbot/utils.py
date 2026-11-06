from __future__ import annotations
import os
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


VC_CONNECT_TIMEOUT = 10


def check_dependencies():
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        check_output(("ffmpeg", "-version"), text=True, creationflags=flags)
    except (FileNotFoundError, CalledProcessError) as e:
        raise RuntimeError(
            "ffmpeg was not found. Install ffmpeg and make sure it is"
            " on PATH."
        ) from e

    try:
        opus.Encoder.get_opus_version()
    except opus.OpusNotLoaded as e:
        raise RuntimeError("opus was not found") from e


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


async def admin_or_owner(ctx: Context) -> bool:
    if ctx.channel.permissions_for(ctx.author).administrator:
        return True
    return bool(await ctx.bot.is_owner(ctx.author))


def holds_role(ctx: Context, role_id) -> bool:
    """True when the author holds the role with this id. A missing id
    (no role set) is held by nobody."""
    if not role_id:
        return False
    return int(role_id) in [r.id for r in ctx.author.roles]


async def dj_check(ctx: Context):
    """Check if the user has DJ permissions"""
    if await admin_or_owner(ctx):
        return True

    sett = get_settings(ctx)
    if sett.dj_role and not holds_role(ctx, sett.dj_role):
        raise CheckError(config.NOT_A_DJ)
    return True


async def settings_admin_check(ctx: Context):
    """Check if the user may change a guild setting: a server
    administrator or the application owner, DJs and EXTRA_OWNERS
    excluded, whether or not a DJ role is set"""
    if await admin_or_owner(ctx):
        return True
    raise CheckError(config.USER_MISSING_PERMISSIONS)


async def is_bot_owner(bot, user) -> bool:
    """Whether `user` is a bot owner: listed in EXTRA_OWNERS, or the
    application owner. EXTRA_OWNERS is checked first, so an extra owner
    costs no application-info lookup."""
    if user.id in config.EXTRA_OWNERS:
        return True
    return bool(await bot.is_owner(user))


async def owner_check(ctx: Context):
    """Check if the user is the owner of the bot"""
    if await is_bot_owner(ctx.bot, ctx.author):
        return True
    raise NotOwner("You do not own this bot.")


def joining_command(func):
    """Marks a command as a joining command (GLOSSARY.md): one that
    may bring the bot into the invoker's voice channel, or move it
    there, once its own checks on its input have passed, or later,
    when a pick from a message it sent acts in its name. A marked
    Music command joins in its body, after its input checks, through
    Music._enter; the hooks never join. A command that opens a view
    whose picks may later join voice must carry the marker too: a
    pick joins through
    that command's context (a library search or browse pick) or
    is admitted as RequestKind.PICK (a search pick). Opt-in: a
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


def bare_url(url: str) -> str:
    """A url's bare form when ``url_regex`` matches it, else the url."""
    match = url_regex.fullmatch(url)
    return match.group("bare") if match else url


def songs_embed(title: str, songs: Iterable[Song]) -> Embed:
    embed = Embed(
        title=title,
        color=config.EMBED_COLOR,
    )

    for counter, song in enumerate(songs, start=1):
        song_title = song.title or bare_url(song.webpage_url)
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
        # loop, and skip() and add_task() are loop-only.
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
