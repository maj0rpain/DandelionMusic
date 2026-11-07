from musicbot import linkutils, utils
from helpers import imported_names


def test_linkutils_does_not_import_the_loader():
    assert "musicbot.loader" not in imported_names(linkutils)


def test_bare_url_of_a_matching_url_is_its_bare_form():
    assert linkutils.bare_url("https://example.com/a?b=1") == (
        "example.com/a?b=1"
    )


def test_bare_url_of_a_non_url_is_unchanged():
    assert linkutils.bare_url("not a url") == "not a url"


def test_utils_takes_bare_url_from_linkutils():
    assert utils.bare_url.__module__ == "musicbot.linkutils"
    assert "musicbot.linkutils.bare_url" in imported_names(utils)
