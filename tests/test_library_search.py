"""Library search ranking.

library._score() carries a lot of deliberate, non-obvious behaviour -
a word-match tier above a containment tier above the raw ratio, an
asymmetric SequenceMatcher orientation, and a screening cutoff that
returns 0.0 rather than the true score. All of it was documented only
in comments, and a comment cannot fail.
"""

import difflib

import pytest

from musicbot import library
from musicbot.library import LibrarySong, SearchResult


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


INDEX = {
    "Radiohead": {
        "OK Computer": [make_song("Airbag"), make_song("Karma Police")],
        "In Rainbows": [make_song("Nude")],
    },
    "The Beatles": {"Abbey Road": [make_song("Come Together")]},
    "Theo": {"Short": [make_song("Track")]},
}


def score(query, candidate, cutoff=0.0):
    query = query.casefold()
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(query)
    return library._score(
        matcher, library._word_pattern(query), query, candidate, cutoff
    )


class TestScore:
    def test_exact_match_scores_one(self):
        assert score("airbag", "Airbag") == pytest.approx(1.0)

    def test_whole_word_outranks_a_fragment(self):
        """Coverage alone reads a short query as a better hit the
        shorter the candidate is, so without the word tier a search for
        "the" buries The Beatles under any short name containing those
        letters."""
        assert score("the", "The Beatles") > score("the", "Theo")

    def test_containment_beats_the_bare_ratio(self):
        # difflib alone rates this 0.63; the containment floor lifts it
        assert score("computer", "OK Computer") > 0.63

    def test_unrelated_candidate_falls_below_the_floor(self):
        assert score("zzzzz", "OK Computer") < library._SCORE_FLOOR

    def test_empty_candidate_scores_zero(self):
        assert score("anything", "") == 0.0

    def test_cutoff_screens_without_changing_what_survives(self):
        """_score returns 0.0 for anything that cannot reach the
        cutoff. Callers only ever compare against the floor, so a
        screened candidate must be one that would have failed anyway."""
        for candidate in ("Airbag", "OK Computer", "Theo", "zzz"):
            exact = score("computer", candidate)
            screened = score(
                "computer", candidate, cutoff=library._SCORE_FLOOR
            )
            if exact >= library._SCORE_FLOOR:
                assert screened == pytest.approx(exact)
            else:
                assert screened < library._SCORE_FLOOR


class TestWordPattern:
    def test_none_when_the_query_is_not_word_bounded(self):
        r"""\b sits between a word and a non-word character, so "!!!"
        (a real band name) could never match one."""
        assert library._word_pattern("!!!") is None

    def test_pattern_for_an_ordinary_query(self):
        assert library._word_pattern("the") is not None

    def test_regex_metacharacters_are_escaped(self):
        # word-bounded at both ends, so a pattern is built, but the "."
        # must not be left as a regex wildcard
        pattern = library._word_pattern("a.b")
        assert pattern.search("a.b")
        assert not pattern.search("axb")


class TestSearch:
    def test_finds_an_artist_album_and_song(self):
        assert library.search(INDEX, "Radiohead")[0].kind == "artist"
        assert library.search(INDEX, "OK Computer")[0].kind == "album"
        assert library.search(INDEX, "Karma Police")[0].kind == "song"

    def test_results_are_ordered_best_first(self):
        results = library.search(INDEX, "Radiohead", limit=5)
        assert [r.score for r in results] == sorted(
            (r.score for r in results), reverse=True
        )

    def test_limit_is_honoured(self):
        assert len(library.search(INDEX, "a", limit=2)) <= 2

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_limit_returns_nothing(self, limit):
        """keep() indexes a heap of the best `limit` scores, which a
        non-positive limit would leave permanently empty."""
        assert library.search(INDEX, "Radiohead", limit=limit) == []

    def test_blank_query_returns_nothing(self):
        assert library.search(INDEX, "   ") == []

    def test_nonsense_query_returns_nothing(self):
        assert library.search(INDEX, "qqqqzzzzxxxx") == []

    def test_long_query_is_capped_not_rejected(self):
        assert library.search(INDEX, "a" * 5000) == []

    def test_a_song_hit_carries_its_whole_path(self):
        hit = library.search(INDEX, "Karma Police")[0]
        assert hit.artist == "Radiohead"
        assert hit.album == "OK Computer"
        assert hit.filename in INDEX[hit.artist][hit.album][1].filename

    def test_screening_does_not_change_results(self):
        """The rising cutoff is an optimisation. Whatever it prunes
        must not have made the list, so a large limit (which keeps the
        cutoff at the floor) has to agree with a small one."""
        broad = library.search(INDEX, "the", limit=50)
        narrow = library.search(INDEX, "the", limit=2)
        assert [(r.kind, r.label) for r in broad[:2]] == [
            (r.kind, r.label) for r in narrow
        ]

    def test_album_wins_a_tie_against_its_own_track(self):
        index = {"A": {"Same Name": [make_song("Same Name")]}}
        assert library.search(index, "Same Name")[0].kind == "album"


class TestStats:
    def test_counts(self):
        assert library.counts(INDEX) == (3, 4, 5)

    def test_artist_stats_aggregates_albums(self):
        stats = library.artist_stats(INDEX, "Radiohead")
        assert stats.albums == 2
        assert stats.tracks == 3

    def test_album_stats(self):
        assert (
            library.album_stats(INDEX, "Radiohead", "In Rainbows").tracks == 1
        )

    def test_missing_artist_is_empty_not_an_error(self):
        assert library.artist_stats(INDEX, "Nobody").tracks == 0

    def test_quality_prefers_the_common_value(self):
        """A single oddly-encoded bonus track must not decide the
        label."""
        songs = [
            make_song("a", bit_depth=16, sample_rate=44100),
            make_song("b", bit_depth=16, sample_rate=44100),
            make_song("c", bit_depth=24, sample_rate=96000),
        ]
        assert library._quality(songs) == "16-bit / 44.1 kHz"

    def test_quality_falls_back_to_bitrate(self):
        songs = [make_song("a", bitrate=320000, fmt="MP3")]
        assert library._quality(songs) == "320 kbps"

    def test_quality_is_none_without_stream_info(self):
        assert library._quality([make_song("a")]) is None


def test_search_result_shape():
    assert SearchResult("artist", "A", None, None, "A", 1.0).album is None
