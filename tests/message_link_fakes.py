"""Fakes for the message-link gate's tests: a guild, channels, threads
and a `discord.Member` that need no Discord connection."""

import atexit
import types

import discord

from config import config
from musicbot.yt_dlp_plugins.extractor import discord as extractor

GUILD_ID = 1234
CHANNEL_ID = 77
OTHER_GUILD_ID = 99
LINK = f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/5"
FOREIGN_LINK = f"https://discord.com/channels/{OTHER_GUILD_ID}/{CHANNEL_ID}/5"


class LinkMember(discord.Member):
    """A real `discord.Member` by type, built without a connection."""

    # class attributes shadow Member's slots and properties
    id = 5
    bot = False
    roles = ()
    voice = None
    guild = None

    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    __hash__ = object.__hash__

    def __str__(self):
        return "member"

    __repr__ = __str__

    def __eq__(self, other):
        return self is other


def perms(
    view_channel=True,
    read_message_history=True,
    manage_threads=False,
):
    return types.SimpleNamespace(
        view_channel=view_channel,
        read_message_history=read_message_history,
        manage_threads=manage_threads,
        manage_messages=False,
    )


class LinkChannel:
    def __init__(self, channel_id=CHANNEL_ID, permissions=None):
        self.id = channel_id
        self.permissions = permissions or perms()

    def permissions_for(self, _member):
        return self.permissions


class LinkThread(LinkChannel):
    """A thread: `private`, and `fetch_member` raising `joined_error`
    (None: the member is in it)."""

    def __init__(self, private=False, joined_error=None, **kwargs):
        super().__init__(**kwargs)
        self.private = private
        self.joined_error = joined_error

    def is_private(self):
        return self.private

    async def fetch_member(self, _member_id):
        if self.joined_error is not None:
            raise self.joined_error
        return object()


class CountingThread(LinkThread):
    """A `LinkThread` appending each `fetch_member` id to `lookups`."""

    def __init__(self, lookups, **kwargs):
        super().__init__(**kwargs)
        self.lookups = lookups

    async def fetch_member(self, member_id):
        self.lookups.append(member_id)
        return await super().fetch_member(member_id)


class LinkGuild:
    def __init__(self, channels=(), guild_id=GUILD_ID):
        self.id = guild_id
        self.name = f"guild {guild_id}"
        self.channels = {c.id: c for c in channels}

    def get_channel_or_thread(self, channel_id):
        return self.channels.get(channel_id)


class FakeHTTPClient:
    instances = []
    login_error = None
    fetch_error = None

    def __init__(self, loop, *args, **kwargs):
        self.loop = loop
        self.logins = []
        self.fetches = []
        self.closed = False
        FakeHTTPClient.instances.append(self)

    async def static_login(self, token):
        self.logins.append(token)
        if FakeHTTPClient.login_error:
            raise FakeHTTPClient.login_error

    async def get_message(self, channel_id, message_id):
        self.fetches.append((channel_id, message_id))
        if FakeHTTPClient.fetch_error:
            raise FakeHTTPClient.fetch_error
        return {
            "author": {"username": "alice"},
            "attachments": [
                {"id": "9", "url": "https://cdn/a.mp3", "filename": "a.mp3"},
                {"id": "10", "url": "https://cdn/b.mp3", "filename": "b.mp3"},
            ],
        }

    async def close(self):
        self.closed = True


def install_fake_discord_client(monkeypatch, register=lambda *a, **k: None):
    """Reset `FakeHTTPClient` and install it as the extractor's client,
    with a bot token and `register` standing in for `atexit.register`."""
    FakeHTTPClient.instances = []
    FakeHTTPClient.login_error = None
    FakeHTTPClient.fetch_error = None
    monkeypatch.setattr(extractor, "HTTPClient", FakeHTTPClient)
    monkeypatch.setattr(extractor, "_client", None)
    monkeypatch.setattr(config, "BOT_TOKEN", "tok")
    monkeypatch.setattr(atexit, "register", register)


def gated_track(track, guild=None, member=None):
    """`track` through the gate, as the entry points make a checked
    track: over a guild with the one `CHANNEL_ID` channel by default,
    for a member who can read it."""
    from helpers import run
    from musicbot.message_link import check_message_link

    guild = guild or LinkGuild([LinkChannel()])
    return run(check_message_link(track, guild, member or LinkMember()))
