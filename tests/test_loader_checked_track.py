"""`loader.load_song` takes a plain or a checked track, over a stubbed
`_load`; the loader holds no message-link pattern of its own."""

import pytest

from musicbot import loader
from musicbot.message_link import CheckedTrack
from helpers import run
from message_link_fakes import LINK

TRACK = "https://example.invalid/track"


@pytest.mark.parametrize(
    "track", [TRACK, CheckedTrack(TRACK), CheckedTrack(LINK, LINK)]
)
def test_load_song_loads_the_track_a_plain_or_checked_track_names(
    track, monkeypatch
):
    loaded = []

    async def load(lane, name):
        loaded.append(name)

    monkeypatch.setattr(loader, "_load", load)
    run(loader.load_song(track))
    assert loaded == [getattr(track, "track", track)]


def test_the_loader_has_no_pattern_match_of_its_own_for_message_links():
    assert not hasattr(loader, "re")
    assert not hasattr(loader, "MESSAGE_LINK_PATTERN")
