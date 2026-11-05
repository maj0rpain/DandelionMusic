"""The replies of d!play, d!playnext, search picks and d!playlist load
are exactly what `render` returns for the controller's outcome."""

import pytest

from config import config
from musicbot import playlists
from musicbot.commands.music import Music
from musicbot.linkutils import SiteTypes
from musicbot.outcome import EnqueueOutcome, OutcomeKind, render
from musicbot.song import Song
from controller_helpers import (
    ProcessController,
    _fake_ffmpeg,
    _run,
    command_ctx,
)
from helpers import run


def _song():
    return Song(SiteTypes.YT_DLP, "https://example.invalid/a", title="a")


OUTCOMES = [
    EnqueueOutcome(OutcomeKind.STARTED, song=_song()),
    EnqueueOutcome(OutcomeKind.QUEUED, song=_song(), position=3),
    EnqueueOutcome(OutcomeKind.START_FAILED, song=_song()),
]


@pytest.mark.parametrize("playnext", [False, True])
@pytest.mark.parametrize("announce", [True, False])
@pytest.mark.parametrize("outcome", OUTCOMES, ids=lambda o: o.kind.name)
def test_play_sends_exactly_what_render_returns(outcome, announce, playnext):
    ctx, sent = command_ctx(
        ProcessController(outcome), announce_songs=announce
    )

    run(Music(None)._play_song(ctx, "t", playnext=playnext))

    expected = render(outcome, announce)
    assert sent == ([] if expected is None else [expected.embed.title])


def test_loading_a_one_entry_saved_playlist_replies_queued_playlist(
    controller, monkeypatch
):
    _fake_ffmpeg(monkeypatch)
    ref = playlists.PlaylistRef("1", "mix")
    contents = playlists.PlaylistContents(
        ref,
        [playlists.PlaylistEntry("https://example.invalid/a", "a")],
    )

    async def get(*args):
        return contents

    monkeypatch.setattr(playlists, "get", get)
    ctx, sent = command_ctx(controller)

    _run(
        controller,
        lambda: Music._playlist_load.callback(Music(None), ctx, "mix"),
    )

    assert sent == [config.SONGINFO_PLAYLIST_QUEUED]
