import enum

import pytest

from musicbot import utils
from musicbot.linkutils import SpotifyPlaylistTypes
from musicbot.playlist import (
    LoopMode,
    LoopState,
    PauseState,
    PlaylistErrorText,
)
from musicbot.settings import ConversionErrorText, SettingsEmbed

ENUMS = [
    LoopMode,
    LoopState,
    PauseState,
    PlaylistErrorText,
    ConversionErrorText,
    SettingsEmbed,
    SpotifyPlaylistTypes,
]


@pytest.mark.parametrize("enum_cls", ENUMS, ids=lambda e: e.__name__)
def test_is_stdlib_strenum(enum_cls):
    assert issubclass(enum_cls, enum.StrEnum)


@pytest.mark.parametrize("enum_cls", ENUMS, ids=lambda e: e.__name__)
def test_members_render_as_their_value(enum_cls):
    for member in enum_cls:
        assert str(member) == member.value
        assert f"{member}" == member.value


def test_utils_has_no_strenum_shim():
    assert not hasattr(utils, "StrEnum")
