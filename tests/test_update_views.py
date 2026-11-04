"""MusicBot.update_views runs every guild's view update even when one
guild's update fails, and names the failing guild in the log.
"""

import asyncio

import discord

from musicbot.bot import MusicBot


class _Controller:
    def __init__(self, name, updated, error=None):
        self.guild = type("G", (), {"name": name})()
        self.error = error
        self.updated = updated

    async def update_view(self):
        if self.error:
            raise self.error
        self.updated.append(self.guild.name)


def test_failing_view_update_does_not_stop_other_guilds_and_names_the_guild(
    capsys,
):
    updated = []
    bot = MusicBot([], command_prefix="d!", intents=discord.Intents.default())
    bot.sessions = [
        _Controller("Broken Guild", updated, RuntimeError("boom")),
        _Controller("ok", updated),
    ]
    asyncio.run(MusicBot.update_views.coro(bot))
    err = capsys.readouterr().err
    assert (
        updated,
        "View update failed for guild Broken Guild:" in err,
        "Traceback" in err,
    ) == (["ok"], True, True)
