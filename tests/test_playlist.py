"""Queue arithmetic in Playlist.

playque[0] is the *currently playing* track, not the next one, which
is what makes next()/prev()/clear() read oddly. The loop modes each
move through the deque differently, and nothing else in the codebase
re-derives that, so these are the tests that pin it down.
"""

import pytest

from musicbot.linkutils import SiteTypes
from musicbot.playlist import LoopMode, Playlist, PlaylistError


def song(title):
    from musicbot.song import Song

    return Song(
        SiteTypes.YT_DLP, f"https://example.invalid/{title}", title=title
    )


def filled(*titles):
    playlist = Playlist()
    for title in titles:
        playlist.add(song(title))
    return playlist


class TestShuffle:
    def test_empty_queue_is_a_no_op(self):
        """The callers gate on is_active(), which stays true for a
        moment after stop_player() has emptied the queue - so d!stop
        followed straight away by d!shuffle reached popleft() with
        nothing there."""
        Playlist().shuffle()  # must not raise

    def test_current_track_stays_first(self):
        playlist = filled(*(str(i) for i in range(25)))
        playlist.shuffle()
        assert playlist[0].title == "0"
        assert len(playlist) == 25


class TestNext:
    def test_advances_and_records_history(self):
        playlist = filled("a", "b")
        assert playlist.next().title == "b"
        assert [s.title for s in playlist.playhistory] == ["a"]

    def test_returns_none_at_the_end(self):
        assert filled("a").next() is None

    def test_empty_queue_returns_none(self):
        assert Playlist().next() is None

    def test_single_loop_repeats_the_same_track(self):
        playlist = filled("a", "b")
        playlist.loop = LoopMode.SINGLE
        assert playlist.next().title == "a"
        assert not playlist.playhistory

    def test_single_loop_can_be_overridden(self):
        """What d!skip does - ignore_single_loop moves on anyway."""
        playlist = filled("a", "b")
        playlist.loop = LoopMode.SINGLE
        assert playlist.next(ignore_single_loop=True).title == "b"

    def test_loop_all_rotates_instead_of_consuming(self):
        playlist = filled("a", "b", "c")
        playlist.loop = LoopMode.ALL
        assert [playlist.next().title for _ in range(3)] == ["b", "c", "a"]
        # nothing is consumed and nothing lands in history
        assert len(playlist) == 3
        assert not playlist.playhistory


class TestPrev:
    def test_pulls_back_from_history(self):
        playlist = filled("a", "b")
        playlist.next()
        assert playlist.prev().title == "a"
        assert playlist[0].title == "a"

    def test_returns_none_with_no_history(self):
        assert filled("a").prev() is None

    def test_loop_all_rotates_backwards(self):
        playlist = filled("a", "b")
        playlist.loop = LoopMode.ALL
        assert playlist.prev().title == "b"


class TestClear:
    def test_keeps_the_current_track(self):
        playlist = filled("a", "b", "c")
        playlist.clear()
        assert [s.title for s in playlist.playque] == ["a"]

    def test_empty_queue_is_a_no_op(self):
        Playlist().clear()


class TestMoveAndRemove:
    def test_remove_returns_the_song(self):
        playlist = filled("a", "b")
        assert playlist.remove(1).title == "b"
        assert len(playlist) == 1

    def test_move_reorders(self):
        playlist = filled("a", "b", "c")
        playlist.move(2, 1)
        assert [s.title for s in playlist.playque] == ["a", "c", "b"]

    @pytest.mark.parametrize("index", [-1, 0, 99])
    def test_rejects_unusable_indexes(self, index):
        """0 is the playing track and negatives would wrap - both are
        refused rather than silently doing the wrong thing."""
        with pytest.raises(PlaylistError):
            filled("a", "b").remove(index)


class TestHistoryBounds:
    def test_trackname_history_is_capped(self):
        from config import config

        playlist = Playlist()
        for i in range(config.MAX_TRACKNAME_HISTORY_LENGTH + 5):
            playlist.add_name(str(i))
        assert (
            len(playlist.trackname_history)
            == config.MAX_TRACKNAME_HISTORY_LENGTH
        )
