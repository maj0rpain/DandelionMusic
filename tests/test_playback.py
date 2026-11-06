"""What `musicbot/playback.py` may import."""

import musicbot.playback as playback_module

from helpers import imported_names


def test_playback_imports_no_song_bot_or_playlist_class_at_runtime():
    imported = imported_names(playback_module, skip_type_checking=True)

    # PauseState, a value Playback returns, is the one thing it takes
    # from musicbot.playlist; the Playlist class itself stays out
    forbidden = {"musicbot.song", "musicbot.bot", "musicbot.playlist.Playlist"}
    assert not forbidden & imported
