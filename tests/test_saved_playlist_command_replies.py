"""What the user is told when a saved-playlist command fails.

The `d!playlist` subcommands are driven on a fake context with the
`playlists` operation faked to raise, so no database or Discord is
involved.
"""

import asyncio
import types

import pytest

from config import config
from musicbot import playlists
from musicbot.commands.music import Music
from musicbot.linkutils import SiteTypes
from musicbot.song import Song
from test_spotify_truncation_reply import _ctx


def _song():
    return Song(SiteTypes.YT_DLP, "https://example.invalid/a", title="a")


def _fail_with(error):
    async def operation(*a):
        raise error

    return operation


def _run_playlist_command(monkeypatch, command, function, error, *args):
    monkeypatch.setattr(playlists, function, _fail_with(error))
    ctx, sent = _ctx()
    asyncio.run(command.callback(Music(None), ctx, *args))
    return sent


@pytest.mark.parametrize(
    "command, function, args",
    [
        (Music._playlist_remove, "delete", ("p",)),
        (Music._playlist_remove_song, "remove_song", ("p", 1)),
        (Music._playlist_move_song, "move_song", ("p", 1, 2)),
    ],
    ids=["remove", "remove_song", "move_song"],
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


def test_save_with_an_existing_name_replies_already_exists(monkeypatch):
    monkeypatch.setattr(
        playlists, "save", _fail_with(playlists.PlaylistExists())
    )
    controller = types.SimpleNamespace(
        playlist=types.SimpleNamespace(playque=[_song()])
    )
    ctx, sent = _ctx(controller)

    asyncio.run(Music._playlist_save.callback(Music(None), ctx, "p"))

    assert sent == [config.PLAYLIST_ALREADY_EXISTS]
