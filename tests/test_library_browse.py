"""The browse cursor's rules.

Every one of these covers a bug that reached master, because none of
them could be tested before: the rules lived on a discord.ui.View
whose every entry point took an Interaction, so the only way to
exercise them was a human clicking buttons in Discord.

Synchronous throughout - the cursor is, deliberately. The dev group
pins pytest with no asyncio_mode, so an async test here would be
collected, skipped with a warning, and silently pass.
"""

import ast
from pathlib import Path

import pytest

from musicbot import library, library_browse
from musicbot.library import LibrarySong


def make_song(title, **kw):
    return LibrarySong(
        filename=kw.get("filename", title + ".flac"),
        title=title,
        duration=kw.get("duration"),
        year=kw.get("year"),
        genre=kw.get("genre"),
        fmt=kw.get("fmt", "FLAC"),
        bitrate=kw.get("bitrate"),
        sample_rate=kw.get("sample_rate"),
        bit_depth=kw.get("bit_depth"),
    )


# Album contents are in the filename order build_index() produces, and
# "Kid A" is deliberately one whose filename order and title order
# disagree - see TestScreen.
INDEX = {
    "Radiohead": {
        "OK Computer": [
            make_song("Airbag", filename="01 - Airbag.flac"),
            make_song("Karma Police", filename="02 - Karma.flac"),
            make_song("Lucky", filename="03 - Lucky.flac"),
        ],
        "Kid A": [
            make_song("Zoo", filename="01 - Zoo.flac"),
            make_song("Apple", filename="02 - Apple.flac"),
        ],
    },
    "The Beatles": {"Abbey Road": [make_song("Come Together")]},
    "Aphex Twin": {"Drukqs": [make_song("Avril 14th")]},
}

# Small enough that three artists span two pages. The real one is a
# Discord fact (25 options per Select) and lives in the view.
PAGE_SIZE = 2


def cursor(index=INDEX, page_size=PAGE_SIZE):
    return library_browse.BrowseCursor(index, page_size)


def at_album():
    """A cursor sitting on Radiohead - OK Computer."""
    c = cursor()
    c.descend("Radiohead")
    c.descend("OK Computer")
    return c


class TestDescend:
    def test_descending_from_the_root_selects_an_artist(self):
        c = cursor()
        descent = c.descend("Radiohead")
        assert descent == library_browse.Descent(True, None)
        assert (c.artist, c.album) == ("Radiohead", None)
        assert not c.at_root
        assert not c.at_album_level
        assert c.screen().kind == "album"

    def test_descending_from_an_artist_selects_an_album(self):
        c = at_album()
        assert (c.artist, c.album) == ("Radiohead", "OK Computer")
        assert c.at_album_level
        assert c.screen().kind == "song"

    def test_a_level_change_resets_the_page_and_bumps_the_revision(self):
        c = cursor()
        c.page_by(1)
        revision = c.level_revision
        c.descend("Radiohead")
        assert c.page == 0
        assert c.level_revision == revision + 1

    def test_picking_a_track_reports_it_without_changing_level(self):
        c = at_album()
        descent = c.descend("03 - Lucky.flac")
        assert descent.changed_level is False
        assert descent.track == ("Radiohead", "OK Computer", "03 - Lucky.flac")
        assert (c.artist, c.album) == ("Radiohead", "OK Computer")

    def test_picking_a_track_neither_resets_the_page_nor_bumps(self):
        """Queueing a track doesn't re-render, so zeroing the page
        would leave the displayed page and the cursor disagreeing and
        the next "Next" click would jump back to page 1 - and bumping
        the revision would tell the in-flight enrichment that the
        album still on screen is stale, dropping its cover art."""
        c = at_album()
        c.page_by(1)
        revision = c.level_revision
        c.descend("03 - Lucky.flac")
        assert c.page == 1
        assert c.level_revision == revision


class TestPaging:
    def test_paging_past_the_end_clamps_to_the_last_page(self):
        """Out of range the page slice comes back empty, and the view
        draws a screen with no Select on it and no way back."""
        c = cursor()
        assert c.last_page() == 1
        c.page_by(1)
        c.page_by(1)
        assert c.page == 1

    def test_paging_before_the_start_clamps_to_zero(self):
        """slice(-2, 0) selects nothing, so a negative page is just as
        destructive as one past the end."""
        c = cursor()
        c.page_by(-1)
        assert c.page == 0

    def test_has_prev_and_has_next_at_the_boundaries(self):
        c = cursor()
        assert not c.has_prev()
        assert c.has_next()
        c.page_by(1)
        assert c.has_prev()
        assert not c.has_next()

    def test_a_single_page_level_has_neither_neighbour(self):
        c = cursor()
        c.descend("Radiohead")
        assert c.last_page() == 0
        assert not c.has_prev()
        assert not c.has_next()

    def test_pages_slice_entries_and_labels_together(self):
        c = at_album()
        assert c.page_entries() == ["01 - Airbag.flac", "02 - Karma.flac"]
        assert c.page_labels() == ["Airbag", "Karma Police"]
        c.page_by(1)
        assert c.page_entries() == ["03 - Lucky.flac"]
        assert c.page_labels() == ["Lucky"]

    def test_a_page_turn_does_not_bump_the_level_revision(self):
        """A page turn stays on the level the pending enrichment
        describes; bumping here would make it drop itself."""
        c = cursor()
        revision = c.level_revision
        c.page_by(1)
        assert c.level_revision == revision


class TestScreen:
    def test_the_screen_is_built_once_per_level(self):
        c = cursor()
        assert c.screen() is c.screen()

    def test_a_page_turn_keeps_the_same_screen(self):
        c = cursor()
        screen = c.screen()
        c.page_by(1)
        assert c.screen() is screen

    def test_a_level_change_builds_a_new_screen(self):
        """The memo is one slot keyed on the scope it was built for -
        a stale screen here shows the previous level's entries."""
        c = cursor()
        root = c.screen()
        c.descend("Radiohead")
        assert c.screen() is not root
        assert c.screen().entries == ["Kid A", "OK Computer"]
        c.back()
        assert c.screen().entries == root.entries

    def test_counts_are_present_exactly_at_the_root(self):
        """_add_stat_fields() unpacks counts into three names without
        a guard, having tested the cursor's position instead."""
        c = cursor()
        assert c.screen().counts == (3, 4, 7)
        assert c.screen().stats is None
        c.descend("Radiohead")
        assert c.screen().counts is None
        assert c.screen().stats is not None
        c.descend("OK Computer")
        assert c.screen().counts is None
        assert c.screen().stats is not None

    def test_entries_and_labels_share_one_list_above_the_song_level(self):
        c = cursor()
        assert c.screen().entries is c.screen().labels
        c.descend("Radiohead")
        assert c.screen().entries is c.screen().labels

    def test_the_song_level_keeps_the_index_order(self):
        """build_index() sorts by filename so that track-numbered
        files keep their album order; re-sorting by the tag-derived
        title here would scramble it."""
        c = cursor()
        c.descend("Radiohead")
        c.descend("Kid A")
        assert c.screen().entries == ["01 - Zoo.flac", "02 - Apple.flac"]
        assert c.screen().labels == ["Zoo", "Apple"]

    def test_the_scope_describes_the_level_actually_built(self):
        c = cursor()
        assert c.screen().scope == (None, None)
        c.descend("Radiohead")
        assert c.screen().scope == ("Radiohead", None)
        c.descend("OK Computer")
        assert c.screen().scope == ("Radiohead", "OK Computer")

    def test_an_unknown_artist_gives_an_empty_screen(self):
        c = cursor()
        c.descend("Nobody")
        assert c.screen().entries == []


class TestTracks:
    def test_the_album_level_stands_for_its_own_tracks(self):
        c = at_album()
        assert c.tracks() == [
            ("Radiohead", "OK Computer", "01 - Airbag.flac"),
            ("Radiohead", "OK Computer", "02 - Karma.flac"),
            ("Radiohead", "OK Computer", "03 - Lucky.flac"),
        ]

    def test_the_artist_level_stands_for_the_whole_discography(self):
        c = cursor()
        c.descend("Radiohead")
        assert len(c.tracks()) == 5
        assert {album for _, album, _ in c.tracks()} == {
            "OK Computer",
            "Kid A",
        }

    def test_the_root_stands_for_nothing(self):
        assert cursor().tracks() == []

    def test_a_song_is_returned_unvalidated(self):
        """Load-bearing: a file deleted since the last index build has
        to reach queue_songs()'s "skipped (file not found)" report,
        which names it and points at `d!lib refresh`. Dropping it here
        would give "Queued 0 song(s)." and no reason why."""
        assert library.tracks_for(
            INDEX, "Radiohead", "OK Computer", "deleted.flac"
        ) == [("Radiohead", "OK Computer", "deleted.flac")]
        assert library.tracks_for(INDEX, "Nobody", "Nothing", "gone.flac") == [
            ("Nobody", "Nothing", "gone.flac")
        ]

    def test_a_filename_without_an_album_is_an_error(self):
        """The triple it would build has no album component, so it
        could not name a file."""
        with pytest.raises(ValueError):
            library.tracks_for(INDEX, "Radiohead", filename="01 - Zoo.flac")


class TestSampleTrack:
    def test_the_root_has_no_sample(self):
        assert cursor().sample_track() is None

    def test_the_artist_level_samples_the_first_album(self):
        c = cursor()
        c.descend("Radiohead")
        assert c.sample_track() == ("Radiohead", "Kid A", "01 - Zoo.flac")

    def test_the_album_level_samples_its_first_track(self):
        c = at_album()
        assert c.sample_track() == (
            "Radiohead",
            "OK Computer",
            "01 - Airbag.flac",
        )

    def test_an_empty_scope_has_no_sample(self):
        c = cursor({"Empty": {}})
        c.descend("Empty")
        assert c.sample_track() is None


class TestBack:
    def test_back_from_the_album_level_returns_to_the_artist(self):
        c = at_album()
        assert c.back() is True
        assert (c.artist, c.album) == ("Radiohead", None)

    def test_back_from_the_artist_level_returns_to_the_root(self):
        c = cursor()
        c.descend("Radiohead")
        assert c.back() is True
        assert (c.artist, c.album) == (None, None)
        assert c.at_root

    def test_back_resets_the_page_and_bumps_the_revision(self):
        c = at_album()
        c.page_by(1)
        revision = c.level_revision
        c.back()
        assert c.page == 0
        assert c.level_revision == revision + 1

    def test_back_at_the_root_returns_false_and_mutates_nothing(self):
        """There is nowhere to go, so the view has nothing to redraw
        and no reason to restart an enrichment."""
        c = cursor()
        c.page_by(1)
        state = (c.artist, c.album, c.page, c.level_revision)
        assert c.back() is False
        assert (c.artist, c.album, c.page, c.level_revision) == state


class TestTheSeam:
    """What stops the cursor drifting back into the view.

    The plan's check for this was `import musicbot.library_browse`
    followed by `assert "discord" not in sys.modules`, which cannot
    pass for any module in the package: importing any of them runs
    musicbot/__init__.py, which imports loader and with it discord,
    yt_dlp and the bot (#20 - make importing musicbot.loader inert).
    So the seam is checked where it actually lives - in what this
    module's source is allowed to import - which is the stronger
    check anyway, since it cannot be satisfied by something else
    having already imported discord first.
    """

    @staticmethod
    def _imports(module) -> set:
        source = Path(module.__file__).read_text(encoding="utf-8")
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # `from musicbot import library` names a module and
                # `from typing import List` names an object inside
                # one; recording both shapes covers either
                names.add(node.module)
                names.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
        return names

    def test_the_cursor_imports_nothing_but_typing_and_the_index(self):
        imports = self._imports(library_browse)
        assert {name.split(".")[0] for name in imports} == {
            "typing",
            "musicbot",
        }
        assert {n for n in imports if n.startswith("musicbot")} == {
            "musicbot",
            "musicbot.library",
        }
