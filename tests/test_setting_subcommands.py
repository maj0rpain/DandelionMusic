"""d!setting <name>: each subcommand of the setting group, driven on a
fake context.

The subcommands are looked up by name from the cog's `setting` group,
the way Discord dispatches them. They update a real GuildSettings
whose database session is a no-op fake, so nothing here needs Discord
or a DB. A failure is never announced as a success, and leaves the
stored value unchanged.
"""

import types

import discord
import pytest
from discord import AppCommandOptionType, ChannelType
from discord.ext import commands
from discord.ext.commands.view import StringView

from config import config
from musicbot.commands.general import General
from musicbot.settings import SETTINGS
from setting_fakes import (
    FakeMessage,
    fake_author,
    fake_bot,
    fake_channel,
    setting_group,
    stored_settings,
    stored_values,
    FakeDbSession,
)
from helpers import run

THUMBS_UP = "\N{THUMBS UP SIGN}"


def subcommand(cog, name):
    return setting_group(cog).get_command(name)


def fake_ctx(sent, sett, message=None):
    async def send(content=None, **kwargs):
        sent.append(content)
        return message or FakeMessage()

    return types.SimpleNamespace(
        bot=fake_bot(sett),
        guild=types.SimpleNamespace(emojis=[]),
        send=send,
    )


NO_VALUE = object()


def invoke_setting(name, arg, sett, message=None):
    """Runs the subcommand; with `arg` left as `NO_VALUE` it is called
    without a value, which resets the setting."""
    cog = General(None)
    sent = []
    ctx = fake_ctx(sent, sett, message)
    args = () if arg is NO_VALUE else (arg,)
    run(subcommand(cog, name).callback(cog, ctx, *args))
    return sent


@pytest.fixture(autouse=True)
def every_setting_editable(monkeypatch):
    """Opens every setting's edit gate: `ALLOW_VC_TIMEOUT_EDIT` for
    `vc_timeout`, and `ENABLE_BUTTON_PLUGIN` for `button_emote`
    (`convert_emoji` refuses without it)."""
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
    names = {c.name for c in setting_group(General(None)).commands}

    assert set(SETTINGS) == names


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

    sent = invoke_setting(name, arg, sett)

    assert sent[-1] == f"Setting `{name}` updated to {confirmation}!"
    assert getattr(sett, name) == stored


def assert_failed(sent, sett, reply):
    assert sent[-1] == reply
    assert not any(m and "updated to" in m for m in sent)
    assert stored_values(sett) == stored_values(stored_settings())


@pytest.fixture
def missing_row(monkeypatch):
    "The settings row is gone: the update matches no row"

    async def execute(self, statement):
        return types.SimpleNamespace(rowcount=0)

    monkeypatch.setattr(FakeDbSession, "execute", execute)


FAILED = "`Error: Setting default_volume could not be updated.`"


def test_a_failed_write_is_reported_on_update(missing_row):
    sett = stored_settings()

    sent = invoke_setting("default_volume", 50, sett)

    assert_failed(sent, sett, FAILED)


def test_a_failed_write_is_reported_on_reset(missing_row):
    sett = stored_settings()

    sent = invoke_setting("default_volume", NO_VALUE, sett)

    assert_failed(sent, sett, FAILED)
    assert not any(m and "reset to" in m for m in sent)


def test_the_update_and_failure_replies_come_from_the_catalogue(monkeypatch):
    monkeypatch.setattr(config, "SETTING_UPDATED", "UPD[{}|{}]")
    monkeypatch.setattr(config, "SETTING_UPDATE_FAILED", "FAIL[{}]")

    updated = invoke_setting("default_volume", 50, stored_settings())
    with monkeypatch.context() as m:

        async def execute(self, statement):
            return types.SimpleNamespace(rowcount=0)

        m.setattr(FakeDbSession, "execute", execute)
        failed_update = invoke_setting("default_volume", 50, stored_settings())
        failed_reset = invoke_setting(
            "default_volume", NO_VALUE, stored_settings()
        )

    assert updated[-1] == "UPD[default_volume|50]"
    assert failed_update[-1] == "FAIL[default_volume]"
    assert failed_reset[-1] == "FAIL[default_volume]"


def test_an_invalid_emoji_is_reported():
    unknown_emoji = discord.HTTPException(
        types.SimpleNamespace(status=400, reason="Bad Request"),
        {"code": 10014, "message": "Unknown Emoji"},
    )
    sett = stored_settings()

    sent = invoke_setting(
        "button_emote",
        "not-an-emoji",
        sett,
        message=FakeMessage(reaction_error=unknown_emoji),
    )

    assert_failed(sent, sett, "`Error: Invalid emote`")


def test_an_out_of_range_volume_is_reported():
    sett = stored_settings()

    sent = invoke_setting("default_volume", 101, sett)

    assert_failed(sent, sett, "`Error: Value must be a number in range 0-100`")


def test_vc_timeout_is_refused_while_its_edit_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_VC_TIMEOUT_EDIT", False)
    sett = stored_settings()

    sent = invoke_setting("vc_timeout", True, sett)

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

    run(
        subcommand(cog, name).callback(
            cog, fake_ctx(sent, sett), **{param: arg}
        )
    )

    assert sent[-1].startswith(f"Setting `{name}` updated to")


TEXT_ID, VOICE_ID, ROLE_ID = (
    100000000000000021,
    100000000000000031,
    100000000000000022,
)


def guild_channel(cls, channel_id, name):
    "A real discord.py channel object, so the converters' type checks hold"
    channel = cls.__new__(cls)
    channel.id = channel_id
    channel.name = name
    return channel


def prefix_ctx(sent, sett, argument):
    """A context as a prefix command sees it: the text after the
    subcommand's name, waiting to be converted"""
    ctx = fake_ctx(sent, sett)
    channels = {
        TEXT_ID: guild_channel(discord.TextChannel, TEXT_ID, "music"),
        VOICE_ID: guild_channel(discord.VoiceChannel, VOICE_ID, "Lounge"),
    }
    roles = {ROLE_ID: types.SimpleNamespace(id=ROLE_ID, name="DJ")}

    ctx.guild.get_channel = channels.get
    ctx.guild.get_thread = lambda thread_id: None
    ctx.guild.get_role = roles.get
    ctx.guild.voice_channels = [channels[VOICE_ID]]
    ctx.channel = fake_channel(True)
    ctx.author = fake_author()
    ctx.message = types.SimpleNamespace(attachments=[])
    ctx.view = StringView(argument)
    ctx.command = None
    ctx.interaction = None
    return ctx


@pytest.mark.parametrize(
    "name, argument, confirmation, stored",
    [
        (
            "command_channel",
            f"<#{TEXT_ID}>",
            f"<#{TEXT_ID}>",
            str(TEXT_ID),
        ),
        (
            "command_channel",
            f"<#{VOICE_ID}>",
            f"<#{VOICE_ID}>",
            str(VOICE_ID),
        ),
        (
            "start_voice_channel",
            "Lounge",
            f"<#{VOICE_ID}>",
            str(VOICE_ID),
        ),
        ("dj_role", f"<@&{ROLE_ID}>", "DJ", str(ROLE_ID)),
        ("user_must_be_in_vc", "yes", "True", True),
        ("announce_songs", "off", "False", False),
        ("button_emote", THUMBS_UP, THUMBS_UP, THUMBS_UP),
        ("default_volume", "55", "55", 55),
    ],
)
def test_a_prefix_subcommand_converts_its_argument(
    name, argument, confirmation, stored
):
    """d!setting <name> <arg>: discord.py converts the text by the
    subcommand's parameter type before the setting is updated"""
    command = bound_subcommand(name)
    sett = stored_settings()
    sent = []

    run(command.invoke(prefix_ctx(sent, sett, argument)))

    assert sent[-1] == f"Setting `{name}` updated to {confirmation}!"
    assert getattr(sett, name) == stored


def test_every_generated_subcommand_takes_only_its_value_parameter():
    """discord.py skips `self` only when it reads the callback as a
    method, so a subcommand left unqualified would also take `self`"""
    for command in setting_group(General(None)).commands:
        assert list(command.clean_params) == [
            SETTINGS[command.name].param_name
        ]


def bound_subcommand(name):
    cog = General(None)
    command = subcommand(cog, name)
    command.cog = cog  # as bot.add_cog() binds it
    return command


def reset_text(name):
    shown = SETTINGS[name].show(None, SETTINGS[name].default)
    return f"Setting `{name}` reset to {shown}!"


@pytest.mark.parametrize("name", list(ARGS))
def test_a_subcommand_without_a_value_resets_the_setting(name):
    sett = stored_settings()
    if getattr(sett, name) == SETTINGS[name].default:
        setattr(sett, name, True)  # announce_songs defaults to False
    assert getattr(sett, name) != SETTINGS[name].default

    sent = invoke_setting(name, NO_VALUE, sett)

    assert sent[-1] == reset_text(name)
    assert getattr(sett, name) == SETTINGS[name].default


def test_a_reset_is_confirmed_with_the_value_the_embed_shows():
    sent = invoke_setting("command_channel", NO_VALUE, stored_settings())

    assert sent[-1] == "Setting `command_channel` reset to Not Set!"


def test_a_prefix_subcommand_without_a_value_resets_the_setting():
    command = bound_subcommand("dj_role")
    sett = stored_settings()
    sent = []

    run(command.invoke(prefix_ctx(sent, sett, "")))

    assert sent[-1] == reset_text("dj_role")
    assert sett.dj_role is None


def test_a_prefix_value_that_does_not_convert_is_not_a_reset():
    command = bound_subcommand("dj_role")
    sett = stored_settings()
    sent = []

    ctx = prefix_ctx(sent, sett, "Typo")
    ctx.guild._roles = {}  # no role by that name
    with pytest.raises(commands.BadArgument):
        run(command.invoke(ctx))

    assert stored_values(sett) == stored_values(stored_settings())


@pytest.mark.parametrize("name", list(ARGS))
def test_a_slash_subcommand_value_is_optional(name):
    group = setting_group(General(None)).app_command

    [parameter] = group.get_command(name).parameters

    assert not parameter.required


def test_the_generated_annotation_is_not_optional():
    for command in setting_group(General(None)).commands:
        annotation = command.callback.__annotations__[
            SETTINGS[command.name].param_name
        ]
        assert annotation is SETTINGS[command.name].param_type


def test_a_vc_timeout_reset_is_refused_while_its_edit_is_disabled(
    monkeypatch,
):
    monkeypatch.setattr(config, "ALLOW_VC_TIMEOUT_EDIT", False)
    sett = stored_settings()
    sett.vc_timeout = False
    assert SETTINGS["vc_timeout"].default is not False

    sent = invoke_setting("vc_timeout", NO_VALUE, sett)

    assert sent == [config.VC_TIMEOUT_EDIT_DISABLED]
    assert sett.vc_timeout is False


def test_a_button_emote_reset_succeeds_with_the_plugin_disabled(
    monkeypatch,
):
    monkeypatch.setattr(config, "ENABLE_BUTTON_PLUGIN", False)
    sett = stored_settings()

    sent = invoke_setting("button_emote", NO_VALUE, sett)

    assert sent[-1] == reset_text("button_emote")
    assert sett.button_emote is None
