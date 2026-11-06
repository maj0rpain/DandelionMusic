from helpers import imported_roots
from musicbot import library_display, library_session


class TestTrim:
    def test_trim_drops_whole_char_when_budget_ends_in_surrogate_pair(self):
        text = "x" + "\U0001f600" * 10
        assert library_display.trim(text, 4) == "x\U0001f600…"

    def test_trim_cuts_bmp_text_at_the_limit(self):
        assert library_display.trim("abcdef", 4) == "abcd…"

    def test_trim_returns_text_within_budget_unchanged(self):
        assert library_display.trim("abcd", 4) == "abcd"
        assert library_display.trim("\U0001f600\U0001f600", 4) == (
            "\U0001f600\U0001f600"
        )

    def test_trim_counts_a_lone_surrogate_as_one_unit(self):
        lone = "\udc80"  # an undecodable filename byte
        assert library_display.trim("ab" + lone, 3) == "ab" + lone
        assert library_display.trim("ab" + lone + "cd", 3) == (
            "ab" + lone + "…"
        )


class TestTheSeam:
    def test_imports_nothing_from_the_project(self):
        # "" is the root of a relative import
        assert not imported_roots(library_display) & {
            "musicbot",
            "config",
            "",
        }

    def test_session_re_exports_none_of_the_helpers(self):
        for name in ("KIND_EMOJI", "trim", "NAME_TITLE_LIMIT"):
            assert not hasattr(library_session, name)
