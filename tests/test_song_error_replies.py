"""A SongError reaches the user as its own message from d!playlist
add_song."""

import types


from config import config
from musicbot.commands.music import Music
from musicbot.loader import SongError
from musicbot.commands import music as music_module
from discord_fakes import RecordingCtx
from helpers import run


def db_ctx():
    return RecordingCtx(types.SimpleNamespace(DbSession=object()))


def test_add_song_replies_with_the_song_error_and_saves_nothing(monkeypatch):
    add_calls = []

    async def load_song(track):
        raise SongError("unsupported link")

    async def add_songs(*args):
        add_calls.append(args)

    monkeypatch.setattr(music_module.loader, "load_song", load_song)
    monkeypatch.setattr(music_module.playlists, "add_songs", add_songs)
    group = next(c for c in Music(None).get_commands() if c.name == "playlist")
    command = group.get_command("add_song")
    ctx = db_ctx()
    ctx.guild = types.SimpleNamespace(id=1)
    ctx.author = None
    run(command.callback(Music(None), ctx, "mine", "bad link"))
    assert [str(m) for m in ctx.sent] == ["unsupported link"]
    assert add_calls == []
    assert config.PLAYLIST_UPDATED not in ctx.sent
