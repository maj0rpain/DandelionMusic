"""What the user is told when a Spotify playlist was only partly loaded.

`d!play` and `d!playlist add` are driven on a fake context; the
controller's process_song and the loader's load_song are faked, so no
network or Discord is involved.
"""

import asyncio

from config import config
from musicbot.message_link import CheckedTrack
from musicbot import playlists
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.outcome import EnqueueOutcome, OutcomeKind
from musicbot.song import LoadResult
from musicbot.truncation import Truncation, TruncationReason
from controller_helpers import ProcessController, _songs, command_ctx
from helpers import plain_song, run


def test_truncated_playlist_reply_holds_numbers_and_reason_text():
    cut = Truncation(50, TruncationReason.NO_API)
    ctx, sent = command_ctx(
        ProcessController(
            EnqueueOutcome(OutcomeKind.PLAYLIST, count=30, truncation=cut)
        )
    )

    run(Music(None)._play_song(ctx, CheckedTrack("t")))

    assert sent == [cut.notice(30)]


def test_untruncated_playlist_reply_is_queued_playlist():
    ctx, sent = command_ctx(
        ProcessController(EnqueueOutcome(OutcomeKind.PLAYLIST, count=30))
    )

    run(Music(None)._play_song(ctx, CheckedTrack("t")))

    assert sent == [config.SONGINFO_PLAYLIST_QUEUED]


def _process(controller):
    async def main():
        controller.bot.loop = asyncio.get_running_loop()
        return await controller.process_song(CheckedTrack("x"))

    return run(main())


def test_one_song_partial_playlist_is_reported_as_a_playlist_outcome(
    controller, stub_loader
):
    songs = _songs(controller, "a")
    cut = Truncation(50, TruncationReason.API_ERROR)
    stub_loader.results["load_song"] = LoadResult(songs, False, cut)

    result = _process(controller)

    assert result.kind is OutcomeKind.PLAYLIST
    assert result.count == 1
    assert result.truncation == cut


def test_multi_song_partial_playlist_keeps_its_truncation(
    controller, stub_loader
):
    songs = _songs(controller, "a", "b", "c")
    cut = Truncation(50, TruncationReason.NO_API)
    stub_loader.results["load_song"] = LoadResult(songs, False, cut)

    result = _process(controller)

    assert result.kind is OutcomeKind.PLAYLIST
    assert result.count == 3
    assert result.truncation == cut


def test_plain_list_becomes_a_playlist_outcome_without_truncation(
    controller, stub_loader
):
    stub_loader.results["load_song"] = LoadResult(
        _songs(controller, "a", "b"), False
    )

    result = _process(controller)

    assert result.kind is OutcomeKind.PLAYLIST
    assert result.truncation is None


def _add(monkeypatch, loaded, add_songs):
    async def load_song(track):
        return loaded

    monkeypatch.setattr(music.loader, "load_song", load_song)
    monkeypatch.setattr(playlists, "add_songs", add_songs)
    ctx, sent = command_ctx()
    run(Music._playlist_add_song.callback(Music(None), ctx, "p", "t"))
    return sent


def _partial():
    return LoadResult(
        [plain_song()], False, Truncation(50, TruncationReason.NO_API)
    )


def test_playlist_add_reports_truncation_and_update(monkeypatch):
    saved = []

    async def add_songs(db, ref, entries):
        saved.extend(entries)

    sent = _add(monkeypatch, _partial(), add_songs)

    assert len(sent) == 2
    assert sent[0] == _partial().truncation.notice(1)
    assert sent[1] == config.PLAYLIST_UPDATED
    assert [e.title for e in saved] == ["a"]


def test_playlist_add_untruncated_sends_only_update(monkeypatch):
    async def add_songs(*a):
        pass

    sent = _add(
        monkeypatch, LoadResult([plain_song(), plain_song()], False), add_songs
    )

    assert sent == [config.PLAYLIST_UPDATED]


def test_playlist_add_to_missing_playlist_sends_only_not_found(monkeypatch):
    async def add_songs(*a):
        raise playlists.PlaylistNotFound()

    sent = _add(monkeypatch, _partial(), add_songs)

    assert sent == [config.PLAYLIST_NOT_FOUND]
