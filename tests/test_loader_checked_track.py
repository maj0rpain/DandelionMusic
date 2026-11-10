"""`loader.load_song` takes a plain or a checked track, over a stubbed
`_load`; the loader holds no message-link pattern of its own."""

import pickle

import pytest

from musicbot import loader
from musicbot.message_link import CheckedTrack
from helpers import attribute_names, imported_names, run
from message_link_fakes import LINK, gated_track

TRACK = "https://example.invalid/track"


@pytest.mark.parametrize(
    "make",
    [lambda: TRACK, lambda: CheckedTrack(TRACK), lambda: gated_track(LINK)],
)
def test_load_song_hands_load_the_very_track_it_got(make, monkeypatch):
    track = make()
    loaded = []

    async def load(lane, given):
        loaded.append(given)

    monkeypatch.setattr(loader, "_load", load)
    run(loader.load_song(track))
    assert len(loaded) == 1
    assert loaded[0] is track


def test_a_checked_track_equals_itself_after_a_pickle_round_trip():
    checked = gated_track(f"<{LINK}>")
    assert pickle.loads(pickle.dumps(checked)) == checked


@pytest.mark.parametrize(
    "held, url, admitted",
    [
        (LINK, LINK, True),
        (LINK, f"<{LINK}>", True),
        (LINK, f"  {LINK}. ", True),
        (LINK, "https://example.invalid/a.mp3", True),
        (LINK, LINK[:-1] + "6", False),
        (LINK, LINK.replace("//", "//canary."), False),
        (None, LINK, False),
        (None, "https://example.invalid/a.mp3", True),
    ],
)
def test_admits_only_the_held_message_link(monkeypatch, held, url, admitted):
    monkeypatch.setattr(loader, "_admitted_link", held)
    assert loader.admits_link(url) is admitted


def test_the_extractor_decides_through_the_loaders_predicate():
    from musicbot.yt_dlp_plugins.extractor import discord

    assert not [n for n in attribute_names(discord) if "_admitted_link" in n]
    assert "musicbot.loader.admits_link" in imported_names(discord)


def test_the_loader_has_no_pattern_match_of_its_own_for_message_links():
    names = imported_names(loader)
    assert "re" not in names
    assert "musicbot.message_link.MESSAGE_LINK_PATTERN" not in names
