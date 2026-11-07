"""`loader.load_song` takes a checked track, over a stubbed `_load`; the
loader holds no message-link pattern of its own."""

import pytest

from musicbot import loader
from musicbot.message_link import CheckedTrack
from helpers import imported_names, run
from message_link_fakes import LINK

TRACK = "https://example.invalid/track"


@pytest.mark.parametrize(
    "track, name, admitted",
    [
        (CheckedTrack(TRACK), TRACK, None),
        (CheckedTrack(LINK, LINK), LINK, LINK),
    ],
)
def test_load_song_loads_the_track_a_checked_track_names(
    track, name, admitted, monkeypatch
):
    loaded = []

    async def load(lane, name, admitted_link):
        loaded.append((name, admitted_link))

    monkeypatch.setattr(loader, "_load", load)
    run(loader.load_song(track))
    assert loaded == [(name, admitted)]


def test_the_loader_has_no_pattern_match_of_its_own_for_message_links():
    names = imported_names(loader)
    assert "re" not in names
    assert "musicbot.message_link.MESSAGE_LINK_PATTERN" not in names
