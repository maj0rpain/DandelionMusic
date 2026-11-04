"""The playlist group and its subcommands refuse while playlists are
disabled."""

import asyncio
import inspect
import types

import pytest

from config import config
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.utils import CheckError

SUBCOMMANDS = [
    "load",
    "remove",
    "add_song",
    "remove_song",
    "move_song",
    "show",
    "list",
    "save",
]


def commands_under_gate():
    group = Music._playlist
    subs = {c.name: c for c in group.commands}
    assert set(SUBCOMMANDS) <= set(subs)
    return [group] + [subs[n] for n in SUBCOMMANDS]


async def run_checks(cmd, ctx):
    for check in cmd.checks:
        result = check(ctx)
        if inspect.isawaitable(result):
            await result


@pytest.mark.parametrize("cmd", commands_under_gate(), ids=lambda c: c.name)
def test_refuses_while_disabled(cmd, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", False)
    ctx = types.SimpleNamespace()
    with pytest.raises(CheckError) as exc:
        asyncio.run(run_checks(cmd, ctx))
    assert str(exc.value) == config.PLAYLISTS_ARE_DISABLED


@pytest.mark.parametrize("cmd", commands_under_gate(), ids=lambda c: c.name)
def test_gate_admits_while_enabled(cmd, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", True)
    gate = next(c for c in cmd.checks if c.__name__ == "playlists_enabled")
    assert cmd.checks[0] is gate
    assert gate(types.SimpleNamespace()) is True


def test_autocomplete_offers_nothing_while_disabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", False)
    cog = types.SimpleNamespace(bot=None)
    interaction = types.SimpleNamespace(guild=None)
    result = asyncio.run(Music._playlist_autocomplete(cog, interaction, ""))
    assert result == []


def test_save_has_no_inline_gate():
    src = inspect.getsource(Music._playlist_save.callback)
    assert "ENABLE_PLAYLISTS" not in src
