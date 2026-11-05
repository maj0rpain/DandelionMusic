"""Admission on a real MusicBot: on_ready's sweep and on_guild_join leave
every guild the whitelist does not admit and register the rest."""

import asyncio
import types

import discord

from musicbot import settings
from musicbot.bot import MusicBot
from musicbot.settings import WhitelistedGuild
from db_seam import in_memory_db
from helpers import run


class FakeGuild:
    def __init__(self, id, name=None, fail=False):
        self.id = id
        self.name = name or f"guild{id}"
        self.fail = fail
        self.left = False

    async def leave(self):
        if self.fail:
            raise discord.HTTPException(
                types.SimpleNamespace(status=500, reason="boom"), "boom"
            )
        self.left = True


def drive(monkeypatch, listed, scenario):
    """Build a MusicBot over an in-memory database holding `listed`, with
    register recorded, and run `scenario(bot, registered)`."""
    registered = []

    async def go():
        async with in_memory_db(settings) as factory:
            async with factory() as session:
                for guild_id in listed:
                    session.add(WhitelistedGuild(guild_id=str(guild_id)))
                await session.commit()
            bot = MusicBot(
                [], command_prefix="d!", intents=discord.Intents.default()
            )
            bot.DbSession = factory
            bot.absolutely_ready = asyncio.get_running_loop().create_future()

            async def register(guild):
                registered.append(guild.id)

            async def load_settings(guilds):
                pass

            monkeypatch.setattr(bot, "register", register)
            monkeypatch.setattr(bot.sessions, "load_settings", load_settings)
            monkeypatch.setattr(
                type(bot.update_views), "is_running", lambda self: True
            )
            await scenario(bot)

    run(go())
    return registered


def set_guilds(monkeypatch, guilds):
    monkeypatch.setattr(MusicBot, "guilds", property(lambda self: guilds))


def sweep(monkeypatch, listed, guilds):
    set_guilds(monkeypatch, guilds)
    return drive(monkeypatch, listed, lambda bot: bot.on_ready())


def test_sweep_leaves_unadmitted_and_registers_admitted(monkeypatch):
    guilds = [FakeGuild(1), FakeGuild(2), FakeGuild(3)]
    registered = sweep(monkeypatch, [1, 3], guilds)
    assert registered == [1, 3]
    assert [g.left for g in guilds] == [False, True, False]


def test_sweep_registers_every_guild_on_an_empty_whitelist(monkeypatch):
    guilds = [FakeGuild(1), FakeGuild(2)]
    registered = sweep(monkeypatch, [], guilds)
    assert registered == [1, 2]
    assert not any(g.left for g in guilds)


def test_sweep_logs_a_failed_leave_and_goes_on(monkeypatch, capsys):
    guilds = [FakeGuild(1, "bad", fail=True), FakeGuild(2), FakeGuild(3)]
    registered = sweep(monkeypatch, [3], guilds)
    err = capsys.readouterr().err
    assert registered == [3]
    assert guilds[1].left
    assert "bad" in err and "Traceback" in err


def test_sweep_reads_the_whitelist_once(monkeypatch):
    from musicbot import guild_whitelist

    calls = []
    real = guild_whitelist.load

    async def counting(factory):
        calls.append(True)
        return await real(factory)

    monkeypatch.setattr(guild_whitelist, "load", counting)
    sweep(monkeypatch, [1], [FakeGuild(1), FakeGuild(2), FakeGuild(3)])
    assert len(calls) == 1


def join(monkeypatch, listed, guild):
    return drive(monkeypatch, listed, lambda bot: bot.on_guild_join(guild))


def test_join_leaves_an_unadmitted_guild(monkeypatch):
    guild = FakeGuild(2)
    assert join(monkeypatch, [1], guild) == []
    assert guild.left


def test_join_registers_an_admitted_guild(monkeypatch):
    guild = FakeGuild(1)
    assert join(monkeypatch, [1], guild) == [1]
    assert not guild.left


def test_join_registers_any_guild_on_an_empty_whitelist(monkeypatch):
    assert join(monkeypatch, [], FakeGuild(5)) == [5]


def test_join_logs_a_failed_leave_and_does_not_register(monkeypatch, capsys):
    registered = join(monkeypatch, [1], FakeGuild(2, "bad", fail=True))
    err = capsys.readouterr().err
    assert registered == []
    assert "bad" in err and "Traceback" in err
