"""MusicBot.close() waits for both extraction lanes' workers to exit, off
the event loop: other tasks keep running while the workers are joined.
"""

import asyncio
import threading

import pytest

import discord
from discord.ext import commands

from musicbot import bot as bot_module
from musicbot.bot import MusicBot


def test_close_keeps_the_loop_running_while_the_loader_shuts_down(
    monkeypatch,
):
    release = threading.Event()
    shutdown_calls = []
    closed = []

    def blocking_shutdown():
        shutdown_calls.append(True)
        release.wait(timeout=2)

    async def no_http_session():
        pass

    async def discord_close(self):
        # the Discord client closes only once both lanes' workers have exited
        closed.append(release.is_set())

    monkeypatch.setattr(bot_module.loader, "shutdown", blocking_shutdown)
    monkeypatch.setattr(bot_module.linkutils, "stop", no_http_session)
    monkeypatch.setattr(commands.Bot, "close", discord_close)

    async def scenario():
        bot = MusicBot(
            [], command_prefix="d!", intents=discord.Intents.default()
        )
        closing = asyncio.create_task(bot.close())
        while not shutdown_calls:
            await asyncio.sleep(0.01)

        # shutdown is still blocked, yet this coroutine gets to run
        ran_while_blocked = await asyncio.wait_for(
            asyncio.sleep(0, result=True), timeout=1
        )
        still_closing = not closing.done()

        release.set()
        await asyncio.wait_for(closing, timeout=5)
        return ran_while_blocked, still_closing

    try:
        ran_while_blocked, still_closing = asyncio.run(
            asyncio.wait_for(scenario(), timeout=10)
        )
    finally:
        release.set()

    assert (ran_while_blocked, still_closing, shutdown_calls, closed) == (
        True,
        True,
        [True],
        [True],
    )


class _Controller:
    def __init__(self, name, error=None):
        self.guild = type("G", (), {"name": name})()
        self.error = error

    async def udisconnect(self, reason):
        if self.error:
            raise self.error


def _close(monkeypatch, controllers=(), stop_error=None, shutdown_error=None):
    calls = []

    async def stop():
        calls.append("stop")
        if stop_error:
            raise stop_error

    def shutdown():
        calls.append("shutdown")
        if shutdown_error:
            raise shutdown_error

    async def discord_close(self):
        calls.append("discord")

    monkeypatch.setattr(bot_module.loader, "shutdown", shutdown)
    monkeypatch.setattr(bot_module.linkutils, "stop", stop)
    monkeypatch.setattr(commands.Bot, "close", discord_close)

    async def scenario():
        bot = MusicBot(
            [], command_prefix="d!", intents=discord.Intents.default()
        )
        bot.sessions = list(controllers)
        await bot.close()

    return calls, scenario


def test_failing_disconnect_does_not_stop_close_and_names_the_guild(
    monkeypatch, capsys
):
    calls, scenario = _close(
        monkeypatch,
        [_Controller("Broken Guild", RuntimeError("boom")), _Controller("ok")],
    )
    asyncio.run(scenario())
    err = capsys.readouterr().err
    assert (
        calls,
        "Broken Guild" in err,
        "boom" in err,
        "Disconnect failed for guild Broken Guild:" in err,
    ) == (["stop", "shutdown", "discord"], True, True, True)


def test_failing_link_stop_runs_the_rest_then_raises(monkeypatch):
    calls, scenario = _close(monkeypatch, stop_error=ValueError("stop"))
    with pytest.raises(ValueError, match="stop"):
        asyncio.run(scenario())
    assert calls == ["stop", "shutdown", "discord"]


def test_failing_loader_shutdown_still_closes_discord_then_raises(
    monkeypatch,
):
    calls, scenario = _close(monkeypatch, shutdown_error=OSError("join"))
    with pytest.raises(OSError, match="join"):
        asyncio.run(scenario())
    assert calls == ["stop", "shutdown", "discord"]


def test_two_failing_steps_raise_the_later_chained_onto_the_earlier(
    monkeypatch,
):
    calls, scenario = _close(
        monkeypatch,
        stop_error=ValueError("stop"),
        shutdown_error=OSError("join"),
    )
    with pytest.raises(OSError, match="join") as raised:
        asyncio.run(scenario())
    assert calls == ["stop", "shutdown", "discord"]
    assert isinstance(raised.value.__context__, ValueError)
