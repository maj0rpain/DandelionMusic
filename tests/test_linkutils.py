from musicbot import linkutils
from helpers import imported_names


def test_linkutils_does_not_import_the_loader():
    assert "musicbot.loader" not in imported_names(linkutils)
