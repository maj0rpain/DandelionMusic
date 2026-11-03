"""d!setting through discord.py's real Group.invoke on a real Context.

The prefix form replies only with the subcommand's own result; a bare
d!setting and d!setting show give the settings embed; any other word is
an unknown setting. Sent messages and raised errors are all that is
checked.
"""

import asyncio
import types

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from config import config
from musicbot.commands.general import General
from musicbot.utils import CheckError
from test_settings_update_failure import (
    FakeMessage,
    setting_group,
    stored_settings,
    stored_values,
)


class RecordingDbSession:
    commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        pass

    async def commit(self):
        RecordingDbSession.commits += 1


class RecordingContext(commands.Context):
    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs.get("embed")))
        return FakeMessage()


def build_ctx(text, sett, is_dj=True):
    sessions = types.SimpleNamespace(settings=lambda guild: sett)

    async def allowed(ctx):
        return True

    async def is_owner(user):
        return False

    bot = types.SimpleNamespace(
        sessions=sessions,
        DbSession=RecordingDbSession,
        emojis=[],
        can_run=allowed,
        is_owner=is_owner,
        _before_invoke=None,
        _after_invoke=None,
    )
    guild = types.SimpleNamespace(
        emojis=[],
        name="Guild",
        icon=None,
        id=1,
        get_channel=lambda i: None,
        get_channel_or_thread=lambda i: None,
        get_role=lambda i: None,
    )
    message = types.SimpleNamespace(
        _state=None,
        attachments=[],
        guild=guild,
        channel=types.SimpleNamespace(
            permissions_for=lambda member: types.SimpleNamespace(
                administrator=is_dj
            )
        ),
        author=types.SimpleNamespace(id=1, roles=[]),
    )
    ctx = RecordingContext(
        message=message, bot=bot, view=StringView(text), prefix="d!"
    )
    ctx.sent = []
    return ctx


def invoke(text, is_dj=True, sett=None):
    """Runs `d!setting <text>`; returns (sent, error, sett)"""
    cog = General(None)
    group = setting_group(cog)
    group.cog = cog
    for sub in group.commands:
        sub.cog = cog
    sett = sett or stored_settings()
    sett.guild_id = "1"
    ctx = build_ctx(text, sett, is_dj)
    ctx.view.skip_ws()
    ctx.invoked_with = "setting"
    ctx.command = group
    ctx.invoked_parents = []
    ctx.prefix = "d!"
    error = None
    try:
        asyncio.run(group.invoke(ctx))
    except commands.CommandError as e:
        error = e
    return ctx.sent, error, sett


@pytest.fixture(autouse=True)
def fresh_db():
    RecordingDbSession.commits = 0


def test_a_dj_setting_a_value_gets_only_the_confirmation():
    sent, error, sett = invoke("announce_songs on")

    assert error is None
    assert sent == [("Setting `announce_songs` updated to True!", None)]


def test_a_non_dj_is_refused_without_the_embed_or_a_change():
    sett = stored_settings()
    before = stored_values(sett)

    sent, error, sett = invoke("announce_songs on", is_dj=False, sett=sett)

    assert isinstance(error, CheckError)
    assert str(error) == config.NOT_A_DJ
    assert sent == []
    assert stored_values(sett) == before
    assert RecordingDbSession.commits == 0


def test_a_bad_value_gives_only_the_conversion_error():
    sent, error, sett = invoke("announce_songs maybe")

    assert isinstance(error, commands.BadArgument)
    assert sent == []


def test_an_unknown_word_is_an_error_with_no_embed_and_no_change():
    sett = stored_settings()
    before = stored_values(sett)

    sent, error, sett = invoke("typo", sett=sett)

    assert isinstance(error, CheckError)
    assert str(error) == "Unknown setting `typo`"
    assert sent == []
    assert stored_values(sett) == before
    assert RecordingDbSession.commits == 0


@pytest.mark.parametrize("text", ["", "show"])
def test_bare_and_show_send_exactly_the_settings_embed(text):
    sent, error, sett = invoke(text)

    assert error is None
    [(content, embed)] = sent
    assert content is None
    assert isinstance(embed, discord.Embed)


@pytest.mark.parametrize("alias", ["settings", "set"])
def test_the_aliases_resolve_to_the_setting_group(alias):
    holder = commands.GroupMixin()
    group = setting_group(General(None))
    holder.add_command(group)

    assert holder.get_command(alias) is holder.get_command("setting")
    assert holder.get_command(alias) is group


def test_the_slash_show_fallback_has_no_parameters():
    app_group = setting_group(General(None)).app_command

    assert app_group.get_command("show").parameters == []
