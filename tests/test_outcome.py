import ast
import types
from pathlib import Path

import pytest
from helpers import run

from config import config
from musicbot import outcome as outcome_module
from musicbot.linkutils import SiteTypes
from musicbot.outcome import EnqueueOutcome, OutcomeKind, render, send_outcome
from musicbot.song import Song
from musicbot.truncation import Truncation, TruncationReason


def _song():
    return Song(SiteTypes.YT_DLP, "https://example.com/a", title="Tune")


def test_started_with_announcements_on_says_nothing():
    outcome = EnqueueOutcome(OutcomeKind.STARTED, song=_song())
    assert render(outcome, True) is None


def test_started_with_announcements_off_says_now_playing():
    outcome = EnqueueOutcome(OutcomeKind.STARTED, song=_song())
    reply = render(outcome, False)
    assert reply.embed.title == "Now Playing"


def test_start_failed_says_nothing():
    outcome = EnqueueOutcome(OutcomeKind.START_FAILED, song=_song())
    assert render(outcome, False) is None
    assert render(outcome, True) is None


def test_queued_shows_position():
    outcome = EnqueueOutcome(OutcomeKind.QUEUED, song=_song(), position=3)
    reply = render(outcome, True)
    assert reply.embed.title == "Added to queue - position 3"


def test_playlist_says_queued_playlist():
    outcome = EnqueueOutcome(OutcomeKind.PLAYLIST, count=4)
    assert render(outcome, True).text == config.SONGINFO_PLAYLIST_QUEUED


def test_truncated_playlist_says_how_many_of_how_many():
    truncation = Truncation(50, TruncationReason.API_ERROR)
    outcome = EnqueueOutcome(
        OutcomeKind.PLAYLIST, count=10, truncation=truncation
    )
    assert render(outcome, True).text == (
        "Only 10 of 50 tracks could be loaded: "
        "Spotify stopped responding partway through."
    )


def _library(count, skipped):
    outcome = EnqueueOutcome(
        OutcomeKind.LIBRARY_PICK, count=count, skipped=tuple(skipped)
    )
    return render(outcome, True).text


def test_library_pick_with_nothing_skipped():
    assert _library(3, []) == "Queued 3 song(s)."


def test_library_pick_names_a_few_skipped_files():
    assert _library(1, ["a.mp3", "b.mp3"]) == (
        "Queued 1 song(s). 2 skipped (file not found): a.mp3, b.mp3. "
        "The index may be stale - ask the bot owner to run `d!lib refresh`."
    )


def test_library_pick_elides_skipped_files_past_five():
    names = [f"{i}.mp3" for i in range(7)]
    assert _library(0, names) == (
        "Queued 0 song(s). 7 skipped (file not found): "
        "0.mp3, 1.mp3, 2.mp3, 3.mp3, 4.mp3, and 2 more. "
        "The index may be stale - ask the bot owner to run `d!lib refresh`."
    )


def _send(settings):
    sent = []

    async def send(message=None, **kwargs):
        sent.append((message, kwargs.get("embed")))

    ctx = types.SimpleNamespace(
        send=send,
        guild=object(),
        bot=types.SimpleNamespace(
            sessions=types.SimpleNamespace(settings=lambda _guild: settings)
        ),
    )
    outcome = EnqueueOutcome(
        OutcomeKind.STARTED,
        song=Song(SiteTypes.YT_DLP, "https://example.invalid/a", title="a"),
    )
    run(send_outcome(ctx, outcome))
    return sent


@pytest.mark.parametrize("announce", [True, False])
def test_send_outcome_follows_the_guilds_announce_songs(announce):
    settings = types.SimpleNamespace(announce_songs=announce)
    sent = _send(settings)
    assert bool(sent) is (not announce)


def test_send_outcome_with_missing_settings_is_treated_as_announcements_off():
    sent = _send(None)
    assert len(sent) == 1


def test_outcome_module_imports_neither_utils_nor_the_controller():
    names = set()
    tree = ast.parse(Path(outcome_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    assert "musicbot.utils" not in names
    assert "musicbot.audiocontroller" not in names
