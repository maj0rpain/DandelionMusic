"""What the user is told when a saved-playlist command fails.

The `d!playlist` subcommands are driven on a fake context with the
`playlists` operation faked to raise, so no database or Discord is
involved.
"""

import types

import pytest

from config import config
from musicbot import playlists
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.song import LoadResult
from controller_helpers import command_ctx
from helpers import plain_song, run


def _fail_with(error):
    async def operation(*a):
        raise error

    return operation


def _run_playlist_command(monkeypatch, command, function, error, *args):
    async def load_song(track):
        return LoadResult.of_song(plain_song())

    monkeypatch.setattr(music.loader, "load_song", load_song)
    monkeypatch.setattr(playlists, function, _fail_with(error))
    ctx, sent = command_ctx()
    run(command.callback(Music(None), ctx, *args))
    return sent


@pytest.mark.parametrize(
    "command, function, args",
    [
        (Music._playlist_remove, "delete", ("p",)),
        (Music._playlist_remove_song, "remove_song", ("p", 1)),
        (Music._playlist_move_song, "move_song", ("p", 1, 2)),
        (Music._playlist_add_song, "add_songs", ("p", "t")),
    ],
    ids=["remove", "remove_song", "move_song", "add_song"],
)
def test_playlist_commands_on_a_missing_playlist_reply_not_found(
    monkeypatch, command, function, args
):
    sent = _run_playlist_command(
        monkeypatch, command, function, playlists.PlaylistNotFound(), *args
    )

    assert sent == [config.PLAYLIST_NOT_FOUND]


@pytest.mark.parametrize(
    "command, function, args",
    [
        (Music._playlist_remove_song, "remove_song", ("p", 9)),
        (Music._playlist_move_song, "move_song", ("p", 1, 9)),
    ],
    ids=["remove_song", "move_song"],
)
def test_playlist_commands_reject_an_out_of_range_position(
    monkeypatch, command, function, args
):
    sent = _run_playlist_command(
        monkeypatch, command, function, playlists.InvalidPosition(3), *args
    )

    assert sent == ["Invalid position. Playlist has 3 songs."]


def test_remove_song_refuses_the_only_song(monkeypatch):
    sent = _run_playlist_command(
        monkeypatch,
        Music._playlist_remove_song,
        "remove_song",
        playlists.OnlySongInPlaylist(),
        "p",
        1,
    )

    assert sent == ["Can't remove the only song from playlist."]


@pytest.mark.parametrize(
    "error, reply",
    [
        (playlists.PlaylistExists(), config.PLAYLIST_ALREADY_EXISTS),
        (
            playlists.PlaylistNameTooLong(),
            config.PLAYLIST_NAME_TOO_LONG.format(max_length=255),
        ),
    ],
    ids=["already_exists", "name_too_long"],
)
def test_save_that_fails_replies_with_the_reason(monkeypatch, error, reply):
    monkeypatch.setattr(playlists, "save", _fail_with(error))
    controller = types.SimpleNamespace(
        playlist=types.SimpleNamespace(playque=[plain_song()])
    )
    ctx, sent = command_ctx(controller)

    run(Music._playlist_save.callback(Music(None), ctx, "p"))

    assert sent == [reply]


@pytest.mark.parametrize(
    "command, function, args",
    [
        (Music._playlist_load, "get", ("p",)),
        (Music._playlist_show, "get", ("p",)),
        (Music._playlist_remove_song, "remove_song", ("p", 1)),
        (Music._playlist_move_song, "move_song", ("p", 1, 2)),
        (Music._playlist_add_song, "add_songs", ("p", "t")),
    ],
    ids=["load", "show", "remove_song", "move_song", "add_song"],
)
def test_playlist_commands_on_an_unreadable_playlist_reply_corrupt(
    monkeypatch, command, function, args
):
    sent = _run_playlist_command(
        monkeypatch, command, function, playlists.CorruptPlaylist("p"), *args
    )

    assert sent == [config.PLAYLIST_CORRUPT]


def test_show_lists_an_untitled_entry_by_its_url(monkeypatch):
    async def get(session_factory, ref):
        return playlists.PlaylistContents(
            ref, [playlists.PlaylistEntry("u", None)]
        )

    shown = []

    class Paginator:
        def __init__(self, ctx, pages):
            shown.extend(pages)

        async def send(self):
            pass

    monkeypatch.setattr(playlists, "get", get)
    monkeypatch.setattr(music, "SimplePaginator", Paginator)
    ctx, sent = command_ctx()

    run(Music._playlist_show.callback(Music(None), ctx, "p"))

    assert "[u](u)" in shown[0].fields[0].value
