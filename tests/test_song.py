"""Song: per-track metadata, and how it refers to a saved playlist."""

import pickle
from datetime import datetime, timezone

import pytest

from config import config

from musicbot.linkutils import SiteTypes
from musicbot.playlists import PlaylistRef, SavedEntry
from musicbot.settings import SavedPlaylist
from musicbot.song import LoadResult, Song

URL = "https://example.invalid/track"


def test_update_with_a_yt_dlp_playlist_name_keeps_the_saved_entry():
    entry = SavedEntry(PlaylistRef("123", "mix"), URL)
    song = Song(SiteTypes.YT_DLP, URL, title="old", saved_entry=entry)

    song.update({"title": "new", "playlist": "Some YouTube playlist"})

    assert song.title == "new"
    assert song.saved_entry == SavedEntry(PlaylistRef("123", "mix"), URL)


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


def test_a_song_pickled_in_the_old_shape_loads_with_no_saved_entry():
    restored = pickle.loads(pickle.dumps(_old_shape_song()))

    assert restored.title == "old track"
    assert restored.saved_entry is None


def test_a_song_pickled_with_the_two_old_saved_fields_loads_with_no_entry():
    old = _old_shape_song()
    old.saved_playlist = PlaylistRef("123", "mix")
    old.saved_url = URL

    restored = pickle.loads(pickle.dumps(old))

    assert restored.saved_entry is None


def test_a_song_pickled_in_the_old_shape_loads_with_no_extraction_stamp():
    restored = pickle.loads(pickle.dumps(_old_shape_song()))

    assert restored.extracted_at is None


def test_update_copies_the_extraction_stamp_from_a_fresh_song():
    song = Song(SiteTypes.YT_DLP, URL)
    fresh = Song(SiteTypes.YT_DLP, URL, url="u")
    fresh.extracted_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    song.update(fresh)
    assert song.extracted_at == fresh.extracted_at


def test_display_name_is_the_title_else_the_webpage_url():
    assert Song(SiteTypes.YT_DLP, URL, title="named").display_name == "named"
    assert Song(SiteTypes.YT_DLP, URL).display_name == URL


def test_format_output_of_a_title_less_song_shows_its_webpage_url():
    for host in (SiteTypes.YT_DLP, SiteTypes.LOCAL_LIBRARY):
        embed = Song(host, URL).format_output("Now playing")

        assert "None" not in embed.description
        assert URL in embed.description


def test_of_song_is_a_single_untruncated_result():
    song = Song(SiteTypes.YT_DLP, "https://example.invalid/a")

    assert LoadResult.of_song(song) == LoadResult([song], True, None)


def _song(**kw):
    return Song(SiteTypes.YT_DLP, URL, **kw)


def _duration_field(song):
    (field,) = [
        f
        for f in song.format_output("t").fields
        if f.name == config.SONGINFO_DURATION
    ]
    return field.value


@pytest.mark.parametrize(
    "given, stored, shown",
    [
        (213.333, 213, "0:03:33"),
        (213.6, 214, "0:03:34"),
        (213, 213, "0:03:33"),
        (0.3, None, config.SONGINFO_UNKNOWN),
    ],
)
def test_update_stores_a_whole_second_duration_and_shows_it(
    given, stored, shown
):
    song = _song()
    song.update({"duration": given})
    assert song.duration == stored
    assert type(song.duration) is type(stored)
    assert _duration_field(song) == shown


def test_constructor_rounds_a_fractional_duration():
    song = _song(duration=213.6)
    assert song.duration == 214
    assert type(song.duration) is int
    assert _duration_field(song) == "0:03:34"


@pytest.mark.parametrize("update", [None, {"duration": None}])
def test_a_song_with_no_duration_shows_unknown(update):
    song = _song()
    if update:
        song.update(update)
    assert song.duration is None
    assert _duration_field(song) == config.SONGINFO_UNKNOWN


def _pickled_with_raw_duration(duration):
    """A song as an old backup holds it: the raw value is planted
    straight into the instance dict, bypassing the duration rule."""
    old = _old_shape_song()
    old.__dict__["duration"] = duration
    return pickle.loads(pickle.dumps(old))


@pytest.mark.parametrize(
    "given, stored, shown",
    [
        (213.333, 213, "0:03:33"),
        (None, None, config.SONGINFO_UNKNOWN),
        (0, None, config.SONGINFO_UNKNOWN),
    ],
)
def test_a_restored_song_reads_its_raw_duration_in_whole_seconds(
    given, stored, shown
):
    restored = _pickled_with_raw_duration(given)
    assert restored.duration == stored
    assert type(restored.duration) is type(stored)
    assert _duration_field(restored) == shown


def test_assigning_no_duration_directly_clears_a_known_one():
    song = _song(duration=100)
    song.duration = None
    assert song.duration is None
    assert _duration_field(song) == config.SONGINFO_UNKNOWN


def test_update_from_a_restored_song_applies_its_raw_fractional_duration():
    source = _pickled_with_raw_duration(213.333)
    song = _song()
    song.update(source)
    assert song.duration == 213
    assert type(song.duration) is int


def test_the_constructor_reads_a_zero_duration_as_unknown():
    song = _song(duration=0)
    assert song.duration is None
    assert _duration_field(song) == config.SONGINFO_UNKNOWN


@pytest.mark.parametrize("update", [{"duration": 0}, {"duration": None}])
def test_updating_with_a_missing_duration_keeps_the_stored_one(update):
    song = _song(duration=213)
    song.update(update)
    assert song.duration == 213
    assert _duration_field(song) == "0:03:33"
