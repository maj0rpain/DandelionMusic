"""The guild settings descriptor table in musicbot.settings.

Every guild setting is described once, in SETTINGS; its defaults and
the d!setting show embed are driven by it. Nothing here needs Discord
or a database.
"""

import types

import sqlalchemy

from config import config
from musicbot.settings import SETTINGS, GuildSettings


def test_each_setting_defaults_to_its_documented_value():
    defaults = {name: d.default for name, d in SETTINGS.items()}

    assert defaults == {
        "command_channel": None,
        "start_voice_channel": None,
        "dj_role": None,
        "user_must_be_in_vc": True,
        "button_emote": None,
        "default_volume": 100,
        "vc_timeout": config.VC_TIMEOUT_DEFAULT,
        "announce_songs": False,
    }


def test_the_table_describes_exactly_the_setting_columns():
    columns = set(sqlalchemy.inspect(GuildSettings).columns.keys())

    assert set(SETTINGS) == columns - {"guild_id"}


class FakeEmoji:
    def __init__(self, emoji_id):
        self.id = emoji_id

    def __str__(self):
        return f"<:dandelion:{self.id}>"


class FakeGuild:
    """Resolves only the ids it was built with, like a guild whose
    other channels, roles and emojis have been deleted."""

    name = "guild"
    icon = None

    def __init__(self, channels=(), roles=()):
        self.channels = {c.id: c for c in channels}
        self.roles = {r.id: r for r in roles}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_channel_or_thread(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return self.roles.get(role_id)


def named(obj_id, name):
    return types.SimpleNamespace(id=obj_id, name=name)


def fake_ctx(guild, emojis=()):
    return types.SimpleNamespace(
        guild=guild, bot=types.SimpleNamespace(emojis=list(emojis))
    )


def shown(sett, ctx):
    embed = sett.format(ctx)
    return {field.name: field.value for field in embed.fields}


def test_settings_embed_shows_what_each_stored_value_resolves_to():
    ctx = fake_ctx(
        FakeGuild(
            channels=[named(11, "bot-commands"), named(12, "Lounge")],
            roles=[named(13, "DJ")],
        ),
        emojis=[FakeEmoji(14)],
    )
    sett = GuildSettings(
        guild_id="1",
        command_channel="11",
        start_voice_channel="12",
        dj_role="13",
        user_must_be_in_vc=True,
        button_emote="14",
        default_volume=55,
        vc_timeout=True,
        announce_songs=True,
    )

    assert shown(sett, ctx) == {
        "command_channel": "bot-commands",
        "start_voice_channel": "Lounge",
        "dj_role": "DJ",
        "user_must_be_in_vc": "True",
        "button_emote": "<:dandelion:14>",
        "default_volume": "55",
        "vc_timeout": "True",
        "announce_songs": "True",
    }


def test_settings_embed_shows_a_unicode_button_emote_as_itself():
    sett = GuildSettings(guild_id="1", button_emote="\N{THUMBS UP SIGN}")

    assert (
        shown(sett, fake_ctx(FakeGuild()))["button_emote"]
        == "\N{THUMBS UP SIGN}"
    )


def test_settings_embed_shows_unset_values_as_not_set():
    sett = GuildSettings(
        guild_id="1",
        command_channel=None,
        start_voice_channel=None,
        dj_role=None,
        user_must_be_in_vc=False,
        button_emote=None,
        default_volume=0,
        vc_timeout=False,
        announce_songs=False,
    )

    assert set(shown(sett, fake_ctx(FakeGuild())).values()) == {"Not Set"}


def test_settings_embed_flags_ids_that_no_longer_resolve():
    sett = GuildSettings(
        guild_id="1",
        command_channel="21",
        start_voice_channel="22",
        dj_role="23",
        button_emote="24",
    )

    values = shown(sett, fake_ctx(FakeGuild()))

    assert {
        key: values[key]
        for key in (
            "command_channel",
            "start_voice_channel",
            "dj_role",
            "button_emote",
        )
    } == {
        "command_channel": "Invalid Channel",
        "start_voice_channel": "Invalid VChannel",
        "dj_role": "Invalid Role",
        "button_emote": "Invalid Emoji",
    }
