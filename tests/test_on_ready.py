"""on_ready on a real MusicBot: a failing step is logged and never keeps
the bot from becoming ready."""

from musicbot import guild_whitelist
from db_seam import run_musicbot
from developer_fakes import LeavingGuild
from helpers import assert_logged_with_traceback


def three_guilds():
    return [LeavingGuild(1, "one"), LeavingGuild(2, "two"), LeavingGuild(3)]


def test_failed_registration_is_isolated(monkeypatch, capsys):
    out = run_musicbot(monkeypatch, guilds=three_guilds(), fail_register={2})
    cap = capsys.readouterr()
    assert out.registered == [1, 3]
    assert_logged_with_traceback(cap.err, "Registration failed for guild two:")
    assert "Joined one" in cap.out and "Joined guild3" in cap.out
    assert "Joined two" not in cap.out
    assert out.ready and out.started


def test_failed_whitelist_load_fails_closed(monkeypatch, capsys):
    async def boom(factory):
        raise RuntimeError("db down")

    monkeypatch.setattr(guild_whitelist, "load", boom)
    guilds = three_guilds()
    out = run_musicbot(monkeypatch, guilds=guilds)
    err = capsys.readouterr().err
    assert out.registered == []
    assert not any(g.left for g in guilds)
    assert_logged_with_traceback(err, "Loading the whitelist failed:")
    assert out.ready and out.started


def test_failed_settings_load_still_registers_every_guild(monkeypatch, capsys):
    out = run_musicbot(monkeypatch, guilds=three_guilds(), fail_settings=True)
    err = capsys.readouterr().err
    assert out.registered == [1, 2, 3]
    assert_logged_with_traceback(err, "Loading settings failed:")
    assert out.ready and out.started
