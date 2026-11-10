"""MusicBot.refresh_panels refreshes every guild's player panel even when
one guild's refresh fails, and names the failing guild in the log.
"""

from discord_fakes import real_music_bot
from helpers import run
from musicbot.bot import MusicBot


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
    bot = real_music_bot()
    bot.sessions = _Sessions(
        [
            _Panel("Broken Guild", refreshed, RuntimeError("boom")),
            _Panel("ok", refreshed),
        ]
    )
    run(MusicBot.refresh_panels.coro(bot))
    err = capsys.readouterr().err
    assert (
        refreshed,
        0
        <= err.find("Panel refresh failed for guild Broken Guild:")
        < err.find("Traceback"),
    ) == (["ok"], True)
