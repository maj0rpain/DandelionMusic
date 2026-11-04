"""Song: per-track metadata, and how it refers to a saved playlist."""

import pickle
from datetime import datetime, timezone

from musicbot.linkutils import SiteTypes
from musicbot.playlists import PlaylistRef
from musicbot.settings import SavedPlaylist
from musicbot.song import Song

URL = "https://example.invalid/track"


def test_update_with_a_yt_dlp_playlist_name_keeps_the_saved_playlist():
    ref = PlaylistRef("123", "mix")
    song = Song(SiteTypes.YT_DLP, URL, title="old", saved_playlist=ref)

    song.update({"title": "new", "playlist": "Some YouTube playlist"})

    assert song.title == "new"
    assert song.saved_playlist == PlaylistRef("123", "mix")


def _old_shape_song():
    # Before PlaylistRef, a Song held its saved playlist's ORM row as
    # `playlist` and had neither `saved_playlist` nor `extracted_at`.
    old = Song.__new__(Song)
    old.__dict__.update(
        host=SiteTypes.YT_DLP,
        webpage_url=URL,
        url=None,
        title="old track",
        uploader=None,
        duration=None,
        thumbnail=None,
        playlist=SavedPlaylist(guild_id="123", name="mix", songs_json="[]"),
    )
    return old


def test_a_song_pickled_in_the_old_shape_loads_with_no_saved_playlist():
    restored = pickle.loads(pickle.dumps(_old_shape_song()))

    assert restored.title == "old track"
    assert restored.saved_playlist is None


def test_a_song_pickled_in_the_old_shape_loads_with_no_extraction_stamp():
    restored = pickle.loads(pickle.dumps(_old_shape_song()))

    assert restored.extracted_at is None


def test_update_copies_the_extraction_stamp_from_a_fresh_song():
    song = Song(SiteTypes.YT_DLP, URL)
    fresh = Song(SiteTypes.YT_DLP, URL, url="u")
    fresh.extracted_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    song.update(fresh)
    assert song.extracted_at == fresh.extracted_at
