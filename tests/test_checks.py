"""Permission and readiness checks.

These decide who may run owner-only commands (d!execute runs arbitrary
Python) and what happens to a command that arrives before the bot has
finished registering its guilds.
"""

import types

import pytest
from discord.ext.commands import NotOwner

from config import config
from musicbot.utils import (
    CheckError,
    chunks,
    get_audiocontroller,
    get_settings,
    owner_check,
)


def make_ctx(bot, guild=None, author_id=1):
    return types.SimpleNamespace(
        bot=bot, guild=guild, author=types.SimpleNamespace(id=author_id)
    )


def make_bot(controllers=None, is_owner=False, settings=None):
    async def _is_owner(_user):
        return is_owner

    return types.SimpleNamespace(
        audio_controllers=controllers or {},
        settings=settings or {},
        is_owner=_is_owner,
    )


class Guild:
    pass


class TestGetAudiocontroller:
    def test_returns_the_registered_controller(self):
        guild = Guild()
        bot = make_bot({guild: "controller"})
        assert get_audiocontroller(make_ctx(bot, guild)) == "controller"

    def test_unregistered_guild_raises_check_error(self):
        """on_ready registers guilds in a loop that awaits a network
        call apiece, and application commands are not gated behind it
        - so this used to be a bare KeyError with nothing sent back to
        the user."""
        bot = make_bot({Guild(): "controller"})
        with pytest.raises(CheckError) as exc:
            get_audiocontroller(make_ctx(bot, Guild()))
        assert str(exc.value) == config.BOT_NOT_READY

    def test_no_guild_raises_check_error(self):
        """Context.send can be reached with guild None outside a guild
        entirely."""
        with pytest.raises(CheckError):
            get_audiocontroller(make_ctx(make_bot(), None))


class TestGetSettings:
    """The twin of get_audiocontroller. on_ready fills bot.settings and
    bot.audio_controllers in the same pass, so guarding only the
    controller just moved the KeyError one line down into
    play_check()."""

    def test_returns_the_registered_settings(self):
        guild = Guild()
        bot = make_bot(settings={guild: "sett"})
        assert get_settings(make_ctx(bot, guild)) == "sett"

    def test_unregistered_guild_raises_check_error(self):
        bot = make_bot(settings={Guild(): "sett"})
        with pytest.raises(CheckError) as exc:
            get_settings(make_ctx(bot, Guild()))
        assert str(exc.value) == config.BOT_NOT_READY

    def test_no_guild_raises_check_error(self):
        with pytest.raises(CheckError):
            get_settings(make_ctx(make_bot(), None))


@pytest.mark.parametrize(
    "extra_owners, author_id, is_owner, allowed",
    [
        ([], 1, True, True),  # the application owner
        ([], 1, False, False),  # nobody
        ([42], 42, False, True),  # configured extra owner
        ([42], 43, False, False),  # a different user
        # the id that used to be hardcoded in owner_check
        ([], 150861087976194048, False, False),
    ],
)
def test_owner_check(monkeypatch, extra_owners, author_id, is_owner, allowed):
    import asyncio

    monkeypatch.setattr(config, "EXTRA_OWNERS", extra_owners)
    ctx = make_ctx(make_bot(is_owner=is_owner), author_id=author_id)

    if allowed:
        assert asyncio.run(owner_check(ctx)) is True
    else:
        with pytest.raises(NotOwner):
            asyncio.run(owner_check(ctx))


class TestChunks:
    def test_splits_evenly(self):
        assert list(chunks([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]

    def test_keeps_a_short_final_chunk(self):
        assert list(chunks([1, 2, 3], 2)) == [[1, 2], [3]]

    def test_empty(self):
        assert list(chunks([], 5)) == []
