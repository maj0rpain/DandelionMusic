"""Shared Discord stand-ins for the tests: the click FakeInteraction,
make_bot, the voice fakes, RecordingCtx, http_error and the
player view's button ids. Import them rather than writing a per-file
copy.
"""

import asyncio
import types

import discord

from musicbot.sessions import GuildSessions


def make_bot(controllers=None, is_owner=False, settings=None):
    """A bot whose registry is seeded through its own interface:
    `settings` alone are bulk-loaded, and each of `controllers` is
    built by get_or_create() over stand-in settings."""
    controllers = controllers or {}
    settings = settings or {}

    async def _is_owner(_user):
        return is_owner

    async def load(guilds):
        # a vc_timeout means get_or_create() does not autojoin
        return {
            g: settings.get(g, types.SimpleNamespace(vc_timeout=True))
            for g in guilds
        }

    async def seed():
        await sessions.load_settings(settings)
        for guild in controllers:
            await sessions.get_or_create(guild)

    sessions = GuildSessions(controllers.__getitem__, load)
    asyncio.run(seed())
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

    async def defer(self):
        self.deferred = True

    async def send(self, message=None, **kwargs):
        self.sent.append(message)


def http_error():
    return discord.HTTPException(
        types.SimpleNamespace(status=500, reason="Server Error"), "boom"
    )


# The player view's button ids, in its order. Hand-written because
# parametrize reads it at collection; test_audiocontroller checks it
# against the real view.
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
