"""MusicBot.update_views refreshes every guild's player panel even when
one guild's refresh fails, and names the failing guild in the log.
"""

import discord

from musicbot.bot import MusicBot
from helpers import run


class _Panel:
    def __init__(self, name, refreshed, error=None):
        self.guild = type("G", (), {"name": name})()
        self.error = error
        self.refreshed = refreshed

    async def refresh(self):
        if self.error:
            raise self.error
        self.refreshed.append(self.guild.name)


class _Sessions:
    def __init__(self, panels):
        self._panels = panels

    def panels(self):
        return iter(self._panels)


def test_failing_refresh_does_not_stop_other_guilds_and_names_the_guild(
    capsys,
):
    refreshed = []
    bot = MusicBot([], command_prefix="d!", intents=discord.Intents.default())
    bot.sessions = _Sessions(
        [
            _Panel("Broken Guild", refreshed, RuntimeError("boom")),
            _Panel("ok", refreshed),
        ]
    )
    run(MusicBot.update_views.coro(bot))
    err = capsys.readouterr().err
    assert (
        refreshed,
        "View update failed for guild Broken Guild:" in err,
        "Traceback" in err,
    ) == (["ok"], True, True)
