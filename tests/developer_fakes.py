"""FakeBot and FakeCtx, shared by the developer-cog tests (d!execute and
the guild whitelist)."""

import types

import discord

from musicbot.bot import MusicBot


class FakeBot:
    def __init__(self, guilds, owner_id=1, DbSession=None):
        self.guilds = guilds
        self.owner_id = owner_id
        self.DbSession = DbSession

    def get_guild(self, id_):
        return next((g for g in self.guilds if g.id == id_), None)

    async def is_owner(self, user):
        return user.id == self.owner_id

    _leave = MusicBot._leave
    leave_guilds = MusicBot.leave_guilds


class FakeCtx:
    def __init__(self, bot, author_id=1, guild=None):
        self.bot = bot
        self.guild = guild
        self.author = types.SimpleNamespace(id=author_id)
        self.sent = []
        self.channel = types.SimpleNamespace(last_message=None)
        self.me = object()

    async def send(self, text):
        self.sent.append(text)


class LeavingGuild:
    """A guild recording that it was left; `fail` makes leave() raise.
    `log`, when set, receives ("leave", id) in call order."""

    def __init__(self, id, name=None, fail=False):
        self.id = id
        self.name = name or f"guild{id}"
        self.fail = fail
        self.left = False
        self.log = None

    async def leave(self):
        if self.log is not None:
            self.log.append(("leave", self.id))
        if self.fail:
            raise discord.HTTPException(
                types.SimpleNamespace(status=500, reason="boom"), "boom"
            )
        self.left = True
