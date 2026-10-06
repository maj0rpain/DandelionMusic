"""d!settings: a failed update_setting is reported, not claimed as success.

update_setting() returns False for a setting name it does not know.
The commands drive their callbacks on a fake context whose guild
settings report that failure, so nothing here needs Discord or a DB.
"""

import asyncio
import types

import pytest

from musicbot.commands.general import General
from musicbot.settings import GuildSettings


def test_update_setting_rejects_an_unknown_setting():
    sett = GuildSettings(guild_id="1")

    updated = asyncio.run(sett.update_setting("no_such_setting", 5, None))

    assert updated is False
    assert not hasattr(sett, "no_such_setting")


class FailingSettings:
    async def update_setting(self, setting, value, ctx):
        return False


def fake_ctx(sent):
    async def send(message):
        sent.append(message)

    sessions = types.SimpleNamespace(settings=lambda guild: FailingSettings())
    return types.SimpleNamespace(
        bot=types.SimpleNamespace(sessions=sessions),
        guild=object(),
        send=send,
    )


TARGET = types.SimpleNamespace(mention="<#1>", name="DJ")


@pytest.mark.parametrize(
    "command, name, arg",
    [
        (General._set_command_channel, "command_channel", TARGET),
        (General._set_start_voice_channel, "start_voice_channel", TARGET),
        (General._set_dj_role, "dj_role", TARGET),
        (General._set_user_must_be_in_vc, "user_must_be_in_vc", True),
        (General._set_button_emote, "button_emote", "\N{THUMBS UP SIGN}"),
        (General._set_default_volume, "default_volume", 50),
        (General._set_vc_timeout, "vc_timeout", True),
        (General._set_announce_songs, "announce_songs", True),
    ],
)
def test_settings_command_reports_a_failed_update(
    command, name, arg, monkeypatch
):
    monkeypatch.setattr(
        "musicbot.commands.general.config.ALLOW_VC_TIMEOUT_EDIT", True
    )
    sent = []

    asyncio.run(command.callback(General(None), fake_ctx(sent), arg))

    assert sent == [f"`Error: Setting {name} could not be updated.`"]
