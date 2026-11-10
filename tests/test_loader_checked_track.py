"""`loader.load_song` takes a plain or a checked track, over a stubbed
`_load`; the loader holds no message-link pattern of its own."""

import pytest

from musicbot import loader
from musicbot.message_link import CheckedTrack, check_message_link
from helpers import imported_names, run
from message_link_fakes import LINK, LinkChannel, LinkGuild, LinkMember

TRACK = "https://example.invalid/track"


def gated(track):
    guild = LinkGuild([LinkChannel()])
    return run(check_message_link(track, guild, LinkMember()))


@pytest.mark.parametrize(
    "make, name, admitted",
    [
        (lambda: TRACK, TRACK, None),
        (lambda: CheckedTrack(TRACK), TRACK, None),
        (lambda: gated(LINK), LINK, LINK),
    ],
)
def test_load_song_loads_the_track_a_plain_or_checked_track_names(
    make, name, admitted, monkeypatch
):
    loaded = []

    async def load(lane, name, admitted_link):
        loaded.append((name, admitted_link))

    monkeypatch.setattr(loader, "_load", load)
    run(loader.load_song(make()))
    assert loaded == [(name, admitted)]


def test_the_loader_has_no_pattern_match_of_its_own_for_message_links():
    names = imported_names(loader)
    assert "re" not in names
    assert "musicbot.message_link.MESSAGE_LINK_PATTERN" not in names
