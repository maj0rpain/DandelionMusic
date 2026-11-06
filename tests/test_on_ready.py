"""on_ready on a real MusicBot: a failing step is logged and never keeps
the bot from becoming ready."""

import asyncio

import discord

from musicbot import guild_whitelist, settings
from musicbot.bot import MusicBot
from db_seam import in_memory_db, seed_whitelist
from developer_fakes import LeavingGuild
from helpers import run


class Outcome:
    def __init__(self):
        self.registered = []
        self.started = []
        self.ready = False


def drive(monkeypatch, guilds, fail_register=(), fail_settings=False):
    monkeypatch.setattr(MusicBot, "guilds", property(lambda self: guilds))
    out = Outcome()

    async def go():
        async with in_memory_db(settings) as factory:
            await seed_whitelist(factory)
            bot = MusicBot(
                [], command_prefix="d!", intents=discord.Intents.default()
            )
            bot.DbSession = factory
            bot.absolutely_ready = asyncio.get_running_loop().create_future()

            async def register(guild):
                if guild.id in fail_register:
                    raise RuntimeError("db down")
                out.registered.append(guild.id)

            async def load_settings(guilds):
                if fail_settings:
                    raise RuntimeError("settings down")

            monkeypatch.setattr(bot, "register", register)
            monkeypatch.setattr(bot.sessions, "load_settings", load_settings)
            panels = type(bot.refresh_panels)
            monkeypatch.setattr(panels, "is_running", lambda self: False)
            monkeypatch.setattr(
                panels, "start", lambda self: out.started.append(True)
            )
            await bot.on_ready()
            out.ready = bot.absolutely_ready.done()

    run(go())
    return out


def three():
    return [LeavingGuild(1, "one"), LeavingGuild(2, "two"), LeavingGuild(3)]


def test_failed_registration_is_isolated(monkeypatch, capsys):
    out = drive(monkeypatch, three(), fail_register={2})
    cap = capsys.readouterr()
    assert out.registered == [1, 3]
    assert "Registration failed for guild two:" in cap.err
    assert "Traceback" in cap.err
    assert "Joined one" in cap.out and "Joined two" not in cap.out
    assert out.ready and out.started


def test_failed_whitelist_load_fails_closed(monkeypatch, capsys):
    async def boom(factory):
        raise RuntimeError("db down")

    monkeypatch.setattr(guild_whitelist, "load", boom)
    guilds = three()
    out = drive(monkeypatch, guilds)
    err = capsys.readouterr().err
    assert out.registered == []
    assert not any(g.left for g in guilds)
    assert "Loading the whitelist failed:" in err and "Traceback" in err
    assert out.ready and out.started


def test_failed_settings_load_still_registers_every_guild(monkeypatch, capsys):
    out = drive(monkeypatch, three(), fail_settings=True)
    err = capsys.readouterr().err
    assert out.registered == [1, 2, 3]
    assert "Loading settings failed:" in err and "Traceback" in err
    assert out.ready and out.started
