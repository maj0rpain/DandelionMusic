"""The guild settings descriptor table in musicbot.settings.

Every guild setting is described once, in SETTINGS; its defaults and
the d!setting show embed are driven by it. Nothing here needs Discord;
the server-default test builds an in-memory SQLite database.
"""

import types

import sqlalchemy

from config import config
from musicbot.settings import SETTINGS, GuildSettings, default_settings


def test_each_setting_defaults_to_its_documented_value():
    assert default_settings() == {
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


def test_settings_embed_shows_none_ids_as_not_set():
    sett = GuildSettings(
        guild_id="1",
        command_channel=None,
        start_voice_channel=None,
        dj_role=None,
        button_emote=None,
    )

    fields = shown(sett, fake_ctx(FakeGuild()))

    assert [
        fields[name]
        for name in (
            "command_channel",
            "start_voice_channel",
            "dj_role",
            "button_emote",
        )
    ] == ["Not Set"] * 4


def test_settings_embed_shows_false_and_zero_as_themselves():
    sett = GuildSettings(
        guild_id="1",
        user_must_be_in_vc=False,
        default_volume=0,
        vc_timeout=False,
        announce_songs=False,
    )

    fields = shown(sett, fake_ctx(FakeGuild()))

    assert [
        fields[name]
        for name in (
            "user_must_be_in_vc",
            "vc_timeout",
            "announce_songs",
            "default_volume",
        )
    ] == ["False", "False", "False", "0"]


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


def test_every_server_default_column_stores_its_descriptors_default():
    server_defaulted = [
        c.name
        for c in GuildSettings.__table__.columns
        if c.server_default is not None
    ]
    engine = sqlalchemy.create_engine("sqlite://")
    GuildSettings.metadata.create_all(engine)
    values = {"guild_id": "1"} | {
        name: value
        for name, value in default_settings().items()
        if name not in server_defaulted
    }
    with sqlalchemy.orm.Session(engine) as session:
        session.execute(
            sqlalchemy.insert(GuildSettings.__table__).values(values)
        )
        row = session.get(GuildSettings, "1")
        stored = {name: getattr(row, name) for name in server_defaulted}

    assert server_defaulted
    assert stored == {n: SETTINGS[n].default for n in server_defaulted}
