"""d!setting <name>: each subcommand of the setting group, driven on a
fake context.

The subcommands are looked up by name from the cog's `setting` group,
the way Discord dispatches them. They update a real GuildSettings
whose database session is a no-op fake, so nothing here needs Discord
or a DB. A failure is never announced as a success, and leaves the
stored value unchanged.
"""

import asyncio
import types

import discord
import pytest
import sqlalchemy
from discord import AppCommandOptionType, ChannelType

from config import config
from musicbot.commands.general import General
from musicbot.settings import SETTINGS, GuildSettings

THUMBS_UP = "\N{THUMBS UP SIGN}"


def setting_group(cog):
    return next(c for c in cog.get_commands() if c.name == "setting")


def subcommand(cog, name):
    return setting_group(cog).get_command(name)


class FakeDbSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        pass

    async def commit(self):
        pass


class FakeMessage:
    def __init__(self, reaction_error=None):
        self.reaction_error = reaction_error

    async def add_reaction(self, emoji):
        if self.reaction_error is not None:
            raise self.reaction_error


def fake_ctx(sent, sett, message=None):
    async def send(content=None, **kwargs):
        sent.append(content)
        return message or FakeMessage()

    sessions = types.SimpleNamespace(settings=lambda guild: sett)
    return types.SimpleNamespace(
        bot=types.SimpleNamespace(
            sessions=sessions, DbSession=FakeDbSession, emojis=[]
        ),
        guild=types.SimpleNamespace(emojis=[]),
        send=send,
    )


def stored_settings():
    "A guild's settings, every one holding a known, non-default value"
    return GuildSettings(
        guild_id="1",
        command_channel="11",
        start_voice_channel="12",
        dj_role="13",
        user_must_be_in_vc=False,
        button_emote="\N{SPARKLES}",
        default_volume=40,
        vc_timeout=False,
        announce_songs=False,
    )


def stored_values(sett):
    return {name: getattr(sett, name) for name in SETTINGS}


def run(name, arg, sett, message=None):
    cog = General(None)
    sent = []
    ctx = fake_ctx(sent, sett, message)
    asyncio.run(subcommand(cog, name).callback(cog, ctx, arg))
    return sent


@pytest.fixture(autouse=True)
def editable(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_VC_TIMEOUT_EDIT", True)
    monkeypatch.setattr(config, "ENABLE_BUTTON_PLUGIN", True)


CHANNEL = types.SimpleNamespace(id=21, mention="<#21>", name="music")
ROLE = types.SimpleNamespace(id=22, mention="<@&22>", name="DJ")

ARGS = {
    "command_channel": CHANNEL,
    "start_voice_channel": CHANNEL,
    "dj_role": ROLE,
    "user_must_be_in_vc": True,
    "button_emote": THUMBS_UP,
    "default_volume": 50,
    "vc_timeout": True,
    "announce_songs": True,
}


def test_the_subcommands_are_exactly_the_described_settings():
    columns = set(sqlalchemy.inspect(GuildSettings).columns.keys())
    names = {c.name for c in setting_group(General(None)).commands}

    assert set(SETTINGS) == columns - {"guild_id"} == names


@pytest.mark.parametrize(
    "name, arg, confirmation, stored",
    [
        ("command_channel", CHANNEL, "<#21>", "21"),
        ("start_voice_channel", CHANNEL, "<#21>", "21"),
        ("dj_role", ROLE, "DJ", "22"),
        ("user_must_be_in_vc", True, "True", True),
        ("button_emote", THUMBS_UP, THUMBS_UP, THUMBS_UP),
        ("default_volume", 50, "50", 50),
        ("vc_timeout", True, "True", True),
        ("announce_songs", True, "True", True),
    ],
)
def test_a_setting_update_is_confirmed(name, arg, confirmation, stored):
    sett = stored_settings()

    sent = run(name, arg, sett)

    assert sent[-1] == f"Setting `{name}` updated to {confirmation}!"
    assert getattr(sett, name) == stored


def assert_failed(sent, sett, reply):
    assert sent[-1] == reply
    assert not any(m and "updated to" in m for m in sent)
    assert stored_values(sett) == stored_values(stored_settings())


@pytest.mark.parametrize("name", list(ARGS))
def test_an_unknown_setting_is_reported(name, monkeypatch):
    monkeypatch.delitem(SETTINGS, name)
    sett = stored_settings()

    sent = run(name, ARGS[name], sett)

    assert_failed(sent, sett, f"`Error: Setting {name} could not be updated.`")


def test_an_invalid_emoji_is_reported():
    unknown_emoji = discord.HTTPException(
        types.SimpleNamespace(status=400, reason="Bad Request"),
        {"code": 10014, "message": "Unknown Emoji"},
    )
    sett = stored_settings()

    sent = run(
        "button_emote",
        "not-an-emoji",
        sett,
        message=FakeMessage(reaction_error=unknown_emoji),
    )

    assert_failed(sent, sett, "`Error: Invalid emote`")


def test_an_out_of_range_volume_is_reported():
    sett = stored_settings()

    sent = run("default_volume", 101, sett)

    assert_failed(sent, sett, "`Error: Value must be a number in range 0-100`")


def test_vc_timeout_is_refused_while_its_edit_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_VC_TIMEOUT_EDIT", False)
    sett = stored_settings()

    sent = run("vc_timeout", True, sett)

    assert_failed(sent, sett, config.VC_TIMEOUT_EDIT_DISABLED)
    assert sent == [config.VC_TIMEOUT_EDIT_DISABLED]


THREADS = [
    ChannelType.news_thread,
    ChannelType.private_thread,
    ChannelType.public_thread,
]


@pytest.mark.parametrize(
    "name, param, option_type, channel_types",
    [
        (
            "command_channel",
            "channel",
            AppCommandOptionType.channel,
            THREADS + [ChannelType.voice, ChannelType.text, ChannelType.news],
        ),
        (
            "start_voice_channel",
            "channel",
            AppCommandOptionType.channel,
            [ChannelType.voice],
        ),
        ("dj_role", "role", AppCommandOptionType.role, []),
        ("user_must_be_in_vc", "value", AppCommandOptionType.boolean, []),
        ("button_emote", "emoji", AppCommandOptionType.string, []),
        ("default_volume", "value", AppCommandOptionType.integer, []),
        ("vc_timeout", "value", AppCommandOptionType.boolean, []),
        ("announce_songs", "value", AppCommandOptionType.boolean, []),
    ],
)
def test_the_slash_subcommand_takes_its_documented_parameter(
    name, param, option_type, channel_types
):
    group = setting_group(General(None)).app_command

    [parameter] = group.get_command(name).parameters

    assert parameter.name == param
    assert parameter.type == option_type
    assert sorted(parameter.channel_types) == sorted(channel_types)


@pytest.mark.parametrize(
    "name, param, arg",
    [
        ("command_channel", "channel", CHANNEL),
        ("dj_role", "role", ROLE),
        ("button_emote", "emoji", THUMBS_UP),
        ("default_volume", "value", 50),
    ],
)
def test_a_slash_subcommand_takes_its_value_by_parameter_name(
    name, param, arg
):
    "A slash command passes its options to the callback by keyword"
    cog = General(None)
    sett = stored_settings()
    sent = []

    asyncio.run(
        subcommand(cog, name).callback(
            cog, fake_ctx(sent, sett), **{param: arg}
        )
    )

    assert sent[-1].startswith(f"Setting `{name}` updated to")
