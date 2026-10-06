"""Song: per-track metadata, and how it refers to a saved playlist."""

import pickle

from musicbot.linkutils import SiteTypes
from musicbot.playlists import PlaylistRef
from musicbot.song import Song

URL = "https://example.invalid/track"


def test_update_with_a_yt_dlp_playlist_name_keeps_the_saved_playlist():
    ref = PlaylistRef("123", "mix")
    song = Song(SiteTypes.YT_DLP, URL, title="old", saved_playlist=ref)

    song.update({"title": "new", "playlist": "Some YouTube playlist"})

    assert song.title == "new"
    assert song.saved_playlist == PlaylistRef("123", "mix")


def test_a_song_pickled_in_the_old_shape_loads_with_no_saved_playlist():
    # Before PlaylistRef, a Song held its saved playlist's ORM row as
    # `playlist` and had no `saved_playlist` attribute at all.
    old = Song.__new__(Song)
    old.__dict__.update(
        host=SiteTypes.YT_DLP,
        webpage_url=URL,
        url=None,
        title="old track",
        uploader=None,
        duration=None,
        thumbnail=None,
        playlist=None,
    )

    restored = pickle.loads(pickle.dumps(old))

    assert restored.title == "old track"
    assert restored.saved_playlist is None
