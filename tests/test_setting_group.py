"""d!setting through discord.py's real Group.invoke on a real Context.

The prefix form replies only with the subcommand's own result; a bare
d!setting and d!setting show give the settings embed; any other word is
an unknown setting. Through the group, sent messages and raised errors are all
that is checked.

`_unmatched_word` is also tested on its own, against a `StringView`,
for leaving the view where it was.
"""

import types

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from config import config
from musicbot import bot as bot_module
from musicbot.bot import MusicBot
from musicbot.commands.general import General, _unmatched_word
from musicbot.settings import SETTINGS
from musicbot.utils import CheckError
from discord_fakes import RecordingCtx
from setting_fakes import (
    FakeDbSession,
    FakeMessage,
    fake_author,
    fake_bot,
    fake_channel,
    setting_group,
    stored_settings,
    stored_values,
)
from helpers import run


class RecordingContext(commands.Context):
    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs.get("embed")))
        return FakeMessage()


TEXT_ID, VOICE_ID, ROLE_ID = (
    100000000000000021,
    100000000000000031,
    100000000000000022,
)
DJ_ROLE_ID = 13


def guild_channel(cls, channel_id, name):
    "A real discord.py channel object, so the converters' type checks hold"
    channel = cls.__new__(cls)
    channel.id = channel_id
    channel.name = name
    return channel


def fake_group_guild():
    "A guild resolving the channel and role ids the settings take"
    channels = {
        TEXT_ID: guild_channel(discord.TextChannel, TEXT_ID, "music"),
        VOICE_ID: guild_channel(discord.VoiceChannel, VOICE_ID, "Lounge"),
    }
    roles = {ROLE_ID: types.SimpleNamespace(id=ROLE_ID, name="DJ")}
    return types.SimpleNamespace(
        emojis=[],
        name="Guild",
        icon=None,
        id=1,
        get_channel=channels.get,
        get_channel_or_thread=channels.get,
        get_thread=lambda i: None,
        get_role=roles.get,
        voice_channels=[channels[VOICE_ID]],
    )


# a value valid for each generated subcommand
VALID_VALUES = {
    "command_channel": f"<#{TEXT_ID}>",
    "start_voice_channel": f"<#{VOICE_ID}>",
    "dj_role": f"<@&{ROLE_ID}>",
    "user_must_be_in_vc": "on",
    "button_emote": "\N{SPARKLES}",
    "default_volume": "50",
    "vc_timeout": "on",
    "announce_songs": "on",
}


def build_ctx(text, sett, is_admin=True, is_owner=False, roles=()):
    bot = fake_bot(sett)

    async def is_owner_answer(user):
        return is_owner

    bot.is_owner = is_owner_answer
    author = fake_author()
    author.roles = list(roles)
    guild = fake_group_guild()
    message = types.SimpleNamespace(
        _state=None,
        attachments=[],
        guild=guild,
        channel=fake_channel(is_admin),
        author=author,
    )
    ctx = RecordingContext(
        message=message, bot=bot, view=StringView(text), prefix="d!"
    )
    ctx.sent = []
    return ctx


def invoke(text, is_admin=True, sett=None, is_owner=False, roles=()):
    """Runs `d!setting <text>`; returns (sent, error, sett)"""
    cog = General(None)
    group = setting_group(cog)
    group.cog = cog
    for sub in group.commands:
        sub.cog = cog
    sett = sett or stored_settings()
    sett.guild_id = "1"
    ctx = build_ctx(text, sett, is_admin, is_owner, roles)
    ctx.invoked_with = "setting"
    ctx.command = group
    ctx.invoked_parents = []
    error = None
    try:
        run(group.invoke(ctx))
    except commands.CommandError as e:
        error = e
    return ctx.sent, error, sett


@pytest.fixture(autouse=True)
def reset_commit_count():
    FakeDbSession.commits = 0


def test_an_admin_setting_a_value_gets_only_the_confirmation():
    sent, error, sett = invoke("announce_songs on")

    assert error is None
    assert sent == [("Setting `announce_songs` updated to True!", None)]


def settings_with_dj_role(dj_role):
    sett = stored_settings()
    sett.dj_role = dj_role
    return sett, stored_values(sett)


NOT_ADMIN_INVOKERS = {
    "plain member": (None, [], []),
    "plain member, DJ role set": (str(DJ_ROLE_ID), [], []),
    "DJ-role holder": (
        str(DJ_ROLE_ID),
        [types.SimpleNamespace(id=DJ_ROLE_ID)],
        [],
    ),
    "extra owner": (None, [], [1]),
}


@pytest.mark.parametrize("who", list(NOT_ADMIN_INVOKERS))
@pytest.mark.parametrize("name", list(SETTINGS))
def test_a_non_admin_is_refused_without_a_reply_or_a_change(
    monkeypatch, name, who
):
    dj_role, roles, extra_owners = NOT_ADMIN_INVOKERS[who]
    monkeypatch.setattr(config, "EXTRA_OWNERS", extra_owners)
    sett, before = settings_with_dj_role(dj_role)

    sent, error, sett = invoke(
        f"{name} {VALID_VALUES[name]}",
        is_admin=False,
        sett=sett,
        roles=roles,
    )

    assert isinstance(error, CheckError)
    assert str(error) == config.USER_MISSING_PERMISSIONS
    assert sent == []
    assert stored_values(sett) == before
    assert FakeDbSession.commits == 0


@pytest.mark.parametrize("is_owner", [False, True], ids=["admin", "owner"])
@pytest.mark.parametrize("name", list(SETTINGS))
def test_an_admin_or_the_owner_gets_the_subcommands_own_reply(name, is_owner):
    sent, error, sett = invoke(
        f"{name} {VALID_VALUES[name]}",
        is_admin=not is_owner,
        is_owner=is_owner,
    )

    assert error is None
    # button_emote first sends its reaction check; the last reply is the
    # subcommand's own
    content, embed = sent[-1]
    assert embed is None
    assert content == config.VC_TIMEOUT_EDIT_DISABLED or content.startswith(
        f"Setting `{name}` updated to"
    )


@pytest.mark.parametrize("text", ["", "show"])
def test_a_plain_member_still_gets_the_settings_embed(text):
    sent, error, sett = invoke(text, is_admin=False)

    assert error is None
    [(content, embed)] = sent
    assert isinstance(embed, discord.Embed)


def test_the_slash_setting_group_has_no_default_permissions():
    app_group = setting_group(General(None)).app_command

    assert app_group.default_permissions is None


def test_the_command_error_handler_replies_with_the_refusal(monkeypatch):
    printed = []
    monkeypatch.setattr(
        bot_module, "print_exception", lambda e: printed.append(e)
    )
    ctx = RecordingCtx(types.SimpleNamespace(DbSession=object()))
    bot = MusicBot([], command_prefix="d!", intents=discord.Intents.default())

    run(bot.on_command_error(ctx, CheckError(config.USER_MISSING_PERMISSIONS)))

    assert [str(m) for m in ctx.sent] == [config.USER_MISSING_PERMISSIONS]
    assert printed == []


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
    assert FakeDbSession.commits == 0


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


def test_extra_whitespace_before_an_unknown_word_is_still_unknown():
    sent, error, sett = invoke("  foo")

    assert isinstance(error, CheckError)
    assert str(error) == "Unknown setting `foo`"
    assert sent == []


@pytest.mark.parametrize("text", ["", "  ", "foo", "  foo bar", " show"])
def test_the_unmatched_word_leaves_the_view_index_unchanged(text):
    view = StringView(text)

    word = _unmatched_word(view)

    assert view.index == 0
    assert word == (text.split(None, 1) or [None])[0]
