"""Fakes the setting test modules share (not a test module: pytest does
not collect it). Import them from here, never from another test module.
"""

import types

from musicbot.settings import SETTINGS, GuildSettings


def setting_group(cog):
    return next(c for c in cog.get_commands() if c.name == "setting")


class FakeDbSession:
    commits = 0  # across every session; a test that counts resets it

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        pass

    async def execute(self, statement):
        return types.SimpleNamespace(rowcount=1)

    async def commit(self):
        FakeDbSession.commits += 1


class FakeMessage:
    def __init__(self, reaction_error=None):
        self.reaction_error = reaction_error

    async def add_reaction(self, emoji):
        if self.reaction_error is not None:
            raise self.reaction_error


async def _allowed(ctx):
    return True


async def _not_owner(user):
    return False


def fake_bot(sett):
    "A bot whose checks pass and whose guild settings are `sett`"
    return types.SimpleNamespace(
        sessions=types.SimpleNamespace(settings=lambda guild: sett),
        DbSession=FakeDbSession,
        emojis=[],
        can_run=_allowed,
        is_owner=_not_owner,
        _before_invoke=None,
        _after_invoke=None,
    )


def fake_author():
    return types.SimpleNamespace(id=1, roles=[])


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


def fake_channel(is_admin):
    "A channel whose permissions report `administrator` as `is_admin`"
    return types.SimpleNamespace(
        permissions_for=lambda member: types.SimpleNamespace(
            administrator=is_admin
        )
    )
