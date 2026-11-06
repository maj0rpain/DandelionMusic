"""Fakes for the message-link gate's tests: a guild, channels, threads
and a `discord.Member` that need no Discord connection."""

import types

import discord

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
    **extra,
):
    return types.SimpleNamespace(
        view_channel=view_channel,
        read_message_history=read_message_history,
        manage_threads=manage_threads,
        manage_messages=False,
        administrator=False,
        **extra,
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


class LinkGuild:
    def __init__(self, channels=(), guild_id=GUILD_ID):
        self.id = guild_id
        self.name = f"guild {guild_id}"
        self.channels = {c.id: c for c in channels}

    def get_channel_or_thread(self, channel_id):
        return self.channels.get(channel_id)
