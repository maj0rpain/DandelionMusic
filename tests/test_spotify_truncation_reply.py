"""What the user is told when a Spotify playlist was only partly loaded.

`d!play` and `d!playlist add` are driven on a fake context; the
controller's process_song and the loader's load_song are faked, so no
network or Discord is involved.
"""

import asyncio
import types

import pytest

from config import config
from musicbot import playlists
from musicbot.commands.music import Music
from musicbot.song import PartialPlaylist
from test_audiocontroller import _songs


def _ctx(controller=None, sent=None):
    sent = sent if sent is not None else []

    async def send(message=None, **kwargs):
        sent.append(message)

    async def defer(**kwargs):
        pass

    class Timer:
        async def start(self, *a):
            pass

    if controller is not None:
        controller.timer = Timer()
    ctx = types.SimpleNamespace(
        audiocontroller=controller,
        send=send,
        defer=defer,
        author=None,
        guild=types.SimpleNamespace(id=1),
        bot=types.SimpleNamespace(DbSession=None),
    )
    return ctx, sent


class ProcessController:
    def __init__(self, result):
        self.result = result

    async def process_song(self, track, user=None, playnext=False):
        return self.result


def test_truncated_playlist_reply_holds_numbers_and_reason_text():
    from musicbot.audiocontroller import QueuedPlaylist

    result = QueuedPlaylist(30, 50, "SPOTIFY_TRUNCATED_NO_API")
    ctx, sent = _ctx(ProcessController(result))

    asyncio.run(Music(None)._play_song(ctx, "t"))

    [text] = sent
    assert "30" in text and "50" in text
    assert config.SPOTIFY_TRUNCATED_NO_API in text
    assert "SPOTIFY_TRUNCATED_NO_API" not in text


@pytest.mark.parametrize("total", [None, 30])
def test_untruncated_playlist_reply_is_queued_playlist(total):
    from musicbot.audiocontroller import QueuedPlaylist

    ctx, sent = _ctx(ProcessController(QueuedPlaylist(30, total, None)))

    asyncio.run(Music(None)._play_song(ctx, "t"))

    assert sent == [config.SONGINFO_PLAYLIST_QUEUED]


def test_one_song_partial_playlist_is_reported_as_queued_playlist(
    controller, stub_loader
):
    from musicbot.audiocontroller import QueuedPlaylist

    songs = _songs(controller, "a")
    stub_loader.results["load_song"] = PartialPlaylist(
        songs, 50, "SPOTIFY_TRUNCATED_API_ERROR"
    )

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        return await controller.process_song("x")

    result = asyncio.run(run())

    assert isinstance(result, QueuedPlaylist)
    assert (result.queued, result.total, result.reason) == (
        1,
        50,
        "SPOTIFY_TRUNCATED_API_ERROR",
    )


def _add(monkeypatch, loaded, add_songs):
    from musicbot.commands import music

    async def load_song(track):
        return loaded

    monkeypatch.setattr(music.loader, "load_song", load_song)
    monkeypatch.setattr(playlists, "add_songs", add_songs)
    ctx, sent = _ctx()
    asyncio.run(Music._playlist_add_song.callback(Music(None), ctx, "p", "t"))
    return sent


def _partial():
    return PartialPlaylist(
        [_song()],
        50,
        "SPOTIFY_TRUNCATED_NO_API",
    )


def _song():
    from musicbot.song import Song
    from musicbot.linkutils import SiteTypes

    return Song(SiteTypes.YT_DLP, "https://example.invalid/a", title="a")


def test_playlist_add_reports_truncation_and_update(monkeypatch):
    async def add_songs(*a):
        pass

    sent = _add(monkeypatch, _partial(), add_songs)

    assert len(sent) == 2
    assert "1" in sent[0] and "50" in sent[0]
    assert config.SPOTIFY_TRUNCATED_NO_API in sent[0]
    assert sent[1] == config.PLAYLIST_UPDATED


def test_playlist_add_to_missing_playlist_sends_only_not_found(monkeypatch):
    async def add_songs(*a):
        raise playlists.PlaylistNotFound()

    sent = _add(monkeypatch, _partial(), add_songs)

    assert sent == [config.PLAYLIST_NOT_FOUND]


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


@pytest.mark.parametrize(
    "error, text",
    [
        (playlists.PlaylistNotFound(), config.PLAYLIST_NOT_FOUND),
        (playlists.PlaylistExists(), config.PLAYLIST_ALREADY_EXISTS),
        (
            playlists.InvalidPosition(4),
            "Invalid position. Playlist has 4 songs.",
        ),
        (
            playlists.OnlySongInPlaylist(),
            "Can't remove the only song from playlist.",
        ),
    ],
)
def test_saved_playlist_error_reply_texts(error, text):
    from musicbot.commands.music import _saved_playlist_error_reply

    assert _saved_playlist_error_reply(error) == text
