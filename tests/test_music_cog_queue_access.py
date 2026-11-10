"""The Music cog reads the queue through the controller's methods.

Commands run on `command_ctx` over a controller built through the
`controller` fixture, with songs queued through its helpers.
"""

import inspect

from config import config
from musicbot import playlists
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.playlist import PlaylistErrorText
from controller_helpers import _queue, _run_while_playing, _titles, command_ctx
from helpers import run


def test_queue_sends_the_controller_queue_embed(controller):
    _queue(controller, "current", "a")
    ctx, sent = command_ctx(controller)

    run(Music._queue.callback(Music(None), ctx))

    assert sent == [controller.queue_embed().title]


def test_move_with_no_source_moves_the_last_song_to_position_two(controller):
    _queue(controller, "current", "a", "b", "c")
    ctx, sent = command_ctx(controller)

    _run_while_playing(
        controller, lambda: Music._move.callback(Music(None), ctx)
    )

    assert _titles(controller) == ["current", "c", "a", "b"]


def test_remove_with_no_position_removes_and_names_the_last_song(controller):
    _queue(controller, "current", "a", "b")
    ctx, sent = command_ctx(controller)

    _run_while_playing(
        controller, lambda: Music._remove.callback(Music(None), ctx)
    )

    assert sent == ["Removed #3: b"]
    assert _titles(controller) == ["current", "a"]


def test_remove_with_no_position_on_a_one_song_queue_removes_nothing(
    controller,
):
    _queue(controller, "only")
    ctx, sent = command_ctx(controller)

    _run_while_playing(
        controller, lambda: Music._remove.callback(Music(None), ctx)
    )

    assert sent == [PlaylistErrorText.ZERO_INDEX]
    assert _titles(controller) == ["only"]


def test_playlist_save_saves_the_queued_songs_in_order(
    controller, monkeypatch
):
    _queue(controller, "current", "a", "b")
    saved = []

    async def save(db_session, ref, entries):
        saved.extend(entries)

    monkeypatch.setattr(playlists, "save", save)
    ctx, sent = command_ctx(controller)

    run(Music._playlist_save.callback(Music(None), ctx, "p"))

    assert [(e.url, e.title) for e in saved] == [
        ("https://example.invalid/current", "current"),
        ("https://example.invalid/a", "a"),
        ("https://example.invalid/b", "b"),
    ]


def test_playlist_save_on_an_empty_queue_replies_queue_empty(controller):
    ctx, sent = command_ctx(controller)

    run(Music._playlist_save.callback(Music(None), ctx, "p"))

    assert sent == [config.QUEUE_EMPTY]


def test_queued_songs_is_an_immutable_snapshot_in_queue_order(controller):
    _queue(controller, "current", "a")

    snapshot = controller.queued_songs()
    _queue(controller, "late")

    assert isinstance(snapshot, tuple)
    assert [s.title for s in snapshot] == ["current", "a"]
    assert controller.queue_length() == 3


def test_the_music_cog_does_not_reach_into_the_controller_playlist():
    assert "audiocontroller.playlist" not in inspect.getsource(music)
