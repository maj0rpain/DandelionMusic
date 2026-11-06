"""Shared Discord stand-ins for the tests: the click FakeInteraction,
make_bot, the voice fakes, RecordingCtx, http_error and the
player panel's button ids. Import them rather than writing a per-file
copy.
"""

import asyncio
import types
from concurrent.futures import ThreadPoolExecutor

import discord

from musicbot import admission
from musicbot.admission import RequestKind
from musicbot.player_panel import PlayerPanel
from musicbot.sessions import GuildSessions
from musicbot.utils import joining_command
from helpers import run


def admission_without_join(controller, on_enter=None):
    """A fake admission needing no join: `enter()` returns `controller()`
    (called each time), after calling `on_enter()` when given."""

    async def enter():
        if on_enter is not None:
            on_enter()
        return controller()

    return types.SimpleNamespace(join_needed=False, enter=enter)


class FakePanel(PlayerPanel):
    """A real player panel over no controller, recording `refresh`,
    `direct_to` and `attach`; `carries_buttons` is the real one.
    `attach` runs the sender with no view, as a panel over an inactive
    controller does. The panel is over no guild."""

    def __init__(self):
        super().__init__(None, lambda: None, lambda: None)
        self.refreshes = 0
        self.directed = []
        self.attached = 0

    async def refresh(self):
        self.refreshes += 1

    def direct_to(self, channel, *, replace=True):
        self.directed.append((channel, replace))
        super().direct_to(channel, replace=replace)

    async def attach(self, send):
        self.attached += 1
        return await send(None)


def make_bot(controllers=None, is_owner=False, settings=None):
    """A bot whose registry is seeded through its own interface:
    `settings` alone are bulk-loaded, and each of `controllers` is
    built by get_or_create() over stand-in settings."""
    controllers = controllers or {}
    settings = settings or {}

    async def _is_owner(_user):
        return is_owner

    async def load(guilds):
        # the default is admission's defaults; their vc_timeout means
        # get_or_create() does not autojoin
        return {g: settings.get(g, admission_settings()) for g in guilds}

    async def seed():
        await sessions.load_settings(settings)
        for guild in controllers:
            await sessions.get_or_create(guild)

    sessions = GuildSessions(
        lambda guild: FakePanel(),
        lambda guild, panel: controllers[guild],
        load,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        run(seed())
    else:
        # built from inside a running test coroutine: seed on a loop of
        # its own, off this thread
        with ThreadPoolExecutor(1) as pool:
            pool.submit(run, seed()).result()
    return types.SimpleNamespace(sessions=sessions, is_owner=_is_owner)


class FakeVoiceClient:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel

    async def move_to(self, channel):
        self.guild.events.append(("move", channel.name))
        if self.guild.fail is not None:
            raise self.guild.fail
        self.channel = channel


class FakeVoiceChannel:
    def __init__(self, guild, name, members=(), connect=True, speak=True):
        self.guild = guild
        self.name = name
        self.members = list(members)
        self.perms = types.SimpleNamespace(connect=connect, speak=speak)

    def permissions_for(self, member):
        assert member is self.guild.me
        return self.perms

    async def connect(self, **kwargs):
        self.guild.events.append(("connect", self.name))
        if self.guild.fail is not None:
            raise self.guild.fail
        self.guild.voice_client = FakeVoiceClient(self.guild, self)


class VoiceGuild:
    """A guild whose bot is out of voice until `put_bot_in(channel)` or
    a connect sets `voice_client`. `fail` is raised by the next connect
    or move."""

    def __init__(self):
        self.id = 1
        self.me = types.SimpleNamespace(bot=True)
        self.voice_client = None
        self.events = []
        self.fail = None

    def channel(self, name, humans=0, bots=0, **perms):
        members = [types.SimpleNamespace(bot=False)] * humans
        members += [types.SimpleNamespace(bot=True)] * bots
        return FakeVoiceChannel(self, name, members, **perms)

    def put_bot_in(self, channel):
        channel.members.append(self.me)
        self.voice_client = FakeVoiceClient(self, channel)


class FakeInteraction:
    """A component click. Records `deferred`, `messages` (direct
    responses) and `followups`. Its client answers get_context with
    the context and has no controller for any guild, so a click ends
    after its action."""

    def __init__(self, ctx, custom_id="x", user_id=1):
        self.guild = ctx.guild
        self.user = types.SimpleNamespace(id=user_id)
        self.data = {"custom_id": custom_id}
        self.deferred = False
        self.messages = []
        self.followups = []

        async def send_message(*args, **kwargs):
            self.messages.append((args, kwargs))

        async def defer(**kwargs):
            self.deferred = True

        self.response = types.SimpleNamespace(
            send_message=send_message,
            defer=defer,
            is_done=lambda: self.deferred or bool(self.messages),
        )

        async def followup_send(*args, **kwargs):
            self.followups.append((args, kwargs))

        self.followup = types.SimpleNamespace(send=followup_send)

        async def get_context(inter):
            return ctx

        self.client = types.SimpleNamespace(
            get_context=get_context,
            sessions=types.SimpleNamespace(controller=lambda guild: None),
        )


class RecordingCtx:
    """A context recording each `send` in `sent` and whether it was
    deferred. `bot` is whatever the command reads off `ctx.bot`."""

    def __init__(self, bot=None):
        self.sent = []
        self.deferred = False
        self.interaction = None
        self.bot = bot
        self.message = types.SimpleNamespace(attachments=[], reference=None)
        self.audiocontroller = None
        self.joined = []

        self.admitted = admission_without_join(
            lambda: self.audiocontroller,
            lambda: self.joined.append("enter"),
        )

    async def defer(self):
        self.deferred = True

    async def send(self, message=None, **kwargs):
        self.sent.append(message)


def http_error():
    return coded_error(discord.HTTPException, 500, 0)


def coded_error(cls, status, code):
    class Response:
        def __init__(self):
            self.status = status
            self.reason = "x"

    return cls(Response(), {"code": code, "message": "x"})


EXPIRED = coded_error(discord.HTTPException, 401, 50027)


# The player panel's expected layout, its button ids in order. Written
# out by hand so test_player_panel can check the real view against it;
# deriving it from the view would compare the view with itself.
PLAYER_BUTTONS = [
    "prev",
    "pause",
    "next",
    "loop",
    "current_song",
    "shuffle",
    "queue",
    "stop",
    "volume_down",
    "volume_up",
]


ADMISSION_CHANNEL = 7
HERE = "here"  # the bot's channel when the author is with it
BOTS_ONLY = "bots"
PEOPLE = "people"
ADMISSION_DJ_ROLE = 99


@joining_command
async def _joining_callback(ctx):
    pass


async def _plain_callback(ctx):
    pass


def _command(joining):
    callback = _joining_callback if joining else _plain_callback
    return types.SimpleNamespace(callback=callback)


def admission_settings(**overrides):
    """The settings admission reads, at their defaults; `overrides`
    change those or add fields outside them."""
    fields = {
        "command_channel": None,
        "user_must_be_in_vc": True,
        "dj_role": None,
        "vc_timeout": True,
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def fake_controller():
    async def dispose(reason):
        pass

    return types.SimpleNamespace(
        dispose=dispose, is_disconnecting=lambda: False
    )


def situation(
    kind,
    bot=None,  # None (out of voice), HERE, BOTS_ONLY or PEOPLE
    author="b",  # None (not in voice), "a" (bot's channel) or "b"
    rule=True,
    wrong_channel=False,
    perms=None,
    role=None,  # None, "admin", "owner" or "dj"
    dj_role=None,
    custom_id="queue",
    registered=True,
    disconnecting=False,
):
    """Builds (guild, ctx) for one row.

    HERE differs from PEOPLE only when used with author="a" (the author
    is then the one human with the bot); both build the same channel.
    """
    guild = VoiceGuild()
    members = {
        PEOPLE: {"humans": 1},
        HERE: {"humans": 1},
        BOTS_ONLY: {"bots": 1},
    }
    a = guild.channel("a", **members.get(bot, {}))
    if bot is not None:
        guild.put_bot_in(a)
    b = guild.channel("b", humans=1, **(perms or {}))
    voice = {"a": a, "b": b}.get(author)
    sett = admission_settings(
        command_channel=ADMISSION_CHANNEL + 1 if wrong_channel else None,
        user_must_be_in_vc=rule,
        dj_role=dj_role,
    )
    controller = fake_controller()
    controller.is_disconnecting = lambda: disconnecting
    controllers = {guild: controller} if registered else {}
    bot_obj = make_bot(
        controllers,
        is_owner=role == "owner",
        settings={guild: sett},
    )
    roles = (
        [types.SimpleNamespace(id=ADMISSION_DJ_ROLE)] if role == "dj" else []
    )
    ctx = types.SimpleNamespace(
        bot=bot_obj,
        guild=guild,
        author=types.SimpleNamespace(
            id=1,
            roles=roles,
            voice=types.SimpleNamespace(channel=voice) if voice else None,
        ),
        channel=types.SimpleNamespace(
            id=ADMISSION_CHANNEL,
            permissions_for=lambda m: types.SimpleNamespace(
                administrator=role == "admin"
            ),
        ),
        command=(
            _command(kind is RequestKind.JOINING_COMMAND)
            if kind in admission.COMMAND_KINDS
            else None
        ),
        interaction=types.SimpleNamespace(data={"custom_id": custom_id}),
        controllers=controllers,
    )
    return guild, ctx


async def invoke_music_hooks(cog, ctx):
    """The Music cog's hooks in discord.py's order: cog_check, the
    command's own checks (none for a command-less ctx), then
    cog_before_invoke."""
    await cog.cog_check(ctx)
    command = getattr(ctx, "command", None)
    for command_check in command.checks if command else ():
        await command_check(ctx)
    await cog.cog_before_invoke(ctx)
