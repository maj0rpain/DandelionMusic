"""Admission on a real MusicBot: on_ready's sweep and on_guild_join leave
every guild the whitelist does not admit and register the rest."""

from db_seam import run_musicbot
from developer_fakes import LeavingGuild
from helpers import assert_logged_with_traceback


def drive(monkeypatch, listed, scenario, guilds=None):
    """Run `scenario(bot)` on a MusicBot whitelisting `listed`; returns the
    registered ids."""
    return run_musicbot(
        monkeypatch, listed, guilds=guilds, scenario=scenario
    ).registered


def sweep(monkeypatch, listed, guilds):
    return drive(
        monkeypatch, listed, lambda bot: bot.on_ready(), guilds=guilds
    )


def test_sweep_leaves_unadmitted_and_registers_admitted(monkeypatch):
    guilds = [LeavingGuild(1), LeavingGuild(2), LeavingGuild(3)]
    registered = sweep(monkeypatch, [1, 3], guilds)
    assert registered == [1, 3]
    assert [g.left for g in guilds] == [False, True, False]


def test_sweep_registers_every_guild_on_an_empty_whitelist(monkeypatch):
    guilds = [LeavingGuild(1), LeavingGuild(2)]
    registered = sweep(monkeypatch, [], guilds)
    assert registered == [1, 2]
    assert not any(g.left for g in guilds)


def test_sweep_logs_a_failed_leave_and_goes_on(monkeypatch, capsys):
    guilds = [
        LeavingGuild(1, "bad", fail=True),
        LeavingGuild(2),
        LeavingGuild(3),
    ]
    registered = sweep(monkeypatch, [3], guilds)
    err = capsys.readouterr().err
    assert registered == [3]
    assert guilds[1].left
    assert_logged_with_traceback(err, "Leaving guild bad failed:")


def test_sweep_reads_the_whitelist_once(monkeypatch):
    from musicbot import guild_whitelist

    calls = []
    real = guild_whitelist.load

    async def counting(factory):
        calls.append(True)
        return await real(factory)

    monkeypatch.setattr(guild_whitelist, "load", counting)
    sweep(
        monkeypatch, [1], [LeavingGuild(1), LeavingGuild(2), LeavingGuild(3)]
    )
    assert len(calls) == 1


def join(monkeypatch, listed, guild):
    return drive(monkeypatch, listed, lambda bot: bot.on_guild_join(guild))


def test_join_leaves_an_unadmitted_guild(monkeypatch):
    guild = LeavingGuild(2)
    assert join(monkeypatch, [1], guild) == []
    assert guild.left


def test_join_registers_an_admitted_guild(monkeypatch):
    guild = LeavingGuild(1)
    assert join(monkeypatch, [1], guild) == [1]
    assert not guild.left


def test_join_registers_any_guild_on_an_empty_whitelist(monkeypatch):
    assert join(monkeypatch, [], LeavingGuild(5)) == [5]


def test_join_logs_a_failed_leave_and_does_not_register(monkeypatch, capsys):
    registered = join(monkeypatch, [1], LeavingGuild(2, "bad", fail=True))
    err = capsys.readouterr().err
    assert registered == []
    assert_logged_with_traceback(err, "Leaving guild bad failed:")
