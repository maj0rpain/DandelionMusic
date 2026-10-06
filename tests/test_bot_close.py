"""MusicBot.close() waits for the extraction worker to exit, off the
event loop: other tasks keep running while the worker is joined.
"""

import asyncio
import threading

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

    async def no_spotify_session():
        pass

    async def discord_close(self):
        # the Discord client closes only once the worker has exited
        closed.append(release.is_set())

    monkeypatch.setattr(bot_module.loader, "shutdown", blocking_shutdown)
    monkeypatch.setattr(bot_module.linkutils, "stop", no_spotify_session)
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
