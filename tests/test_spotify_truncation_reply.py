"""What the user is told when a Spotify playlist was only partly loaded.

`d!play` and `d!playlist add` are driven on a fake context; the
controller's process_song and the loader's load_song are faked, so no
network or Discord is involved.
"""

import asyncio
import sys


from config import config
from musicbot import playlists
from musicbot.audiocontroller import QueuedPlaylist
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.linkutils import SiteTypes
from musicbot.song import PartialPlaylist, Song
from musicbot.truncation import Truncation, TruncationReason
from controller_helpers import ProcessController, _songs, play_ctx


def _queued_playlist_class(controller):
    # the controller fixture loads its own copy of the module
    return sys.modules[type(controller).__module__].QueuedPlaylist


def test_truncated_playlist_reply_holds_numbers_and_reason_text():
    cut = Truncation(50, TruncationReason.NO_API)
    ctx, sent = play_ctx(ProcessController(QueuedPlaylist(30, cut)))

    asyncio.run(Music(None)._play_song(ctx, "t"))

    assert sent == [cut.notice(30)]


def test_untruncated_playlist_reply_is_queued_playlist():
    ctx, sent = play_ctx(ProcessController(QueuedPlaylist(30)))

    asyncio.run(Music(None)._play_song(ctx, "t"))

    assert sent == [config.SONGINFO_PLAYLIST_QUEUED]


def _process(controller):
    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        return await controller.process_song("x")

    return asyncio.run(run())


def test_one_song_partial_playlist_is_reported_as_queued_playlist(
    controller, stub_loader
):
    songs = _songs(controller, "a")
    cut = Truncation(50, TruncationReason.API_ERROR)
    stub_loader.results["load_song"] = PartialPlaylist(songs, cut)

    result = _process(controller)

    assert isinstance(result, _queued_playlist_class(controller))
    assert result.queued == 1
    assert result.truncation == cut


def test_multi_song_partial_playlist_keeps_its_truncation(
    controller, stub_loader
):
    songs = _songs(controller, "a", "b", "c")
    cut = Truncation(50, TruncationReason.NO_API)
    stub_loader.results["load_song"] = PartialPlaylist(songs, cut)

    result = _process(controller)

    assert isinstance(result, _queued_playlist_class(controller))
    assert result.queued == 3
    assert result.truncation == cut


def test_plain_list_becomes_queued_playlist_without_truncation(
    controller, stub_loader
):
    stub_loader.results["load_song"] = _songs(controller, "a", "b")

    result = _process(controller)

    assert isinstance(result, _queued_playlist_class(controller))
    assert result.truncation is None and not result.truncated


def _add(monkeypatch, loaded, add_songs):
    async def load_song(track):
        return loaded

    monkeypatch.setattr(music.loader, "load_song", load_song)
    monkeypatch.setattr(playlists, "add_songs", add_songs)
    ctx, sent = play_ctx()
    asyncio.run(Music._playlist_add_song.callback(Music(None), ctx, "p", "t"))
    return sent


def _partial():
    return PartialPlaylist([_song()], Truncation(50, TruncationReason.NO_API))


def _song():
    return Song(SiteTypes.YT_DLP, "https://example.invalid/a", title="a")


def test_playlist_add_reports_truncation_and_update(monkeypatch):
    async def add_songs(*a):
        pass

    sent = _add(monkeypatch, _partial(), add_songs)

    assert len(sent) == 2
    assert sent[0] == _partial().truncation.notice(1)
    assert sent[1] == config.PLAYLIST_UPDATED


def test_playlist_add_untruncated_sends_only_update(monkeypatch):
    async def add_songs(*a):
        pass

    sent = _add(monkeypatch, [_song(), _song()], add_songs)

    assert sent == [config.PLAYLIST_UPDATED]


def test_playlist_add_to_missing_playlist_sends_only_not_found(monkeypatch):
    async def add_songs(*a):
        raise playlists.PlaylistNotFound()

    sent = _add(monkeypatch, _partial(), add_songs)

    assert sent == [config.PLAYLIST_NOT_FOUND]
