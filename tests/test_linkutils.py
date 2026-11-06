import pytest

from musicbot import linkutils
from helpers import imported_names


def test_linkutils_does_not_import_the_loader():
    assert "musicbot.loader" not in imported_names(linkutils)


@pytest.mark.parametrize(
    "ext", [".flac", ".m4a", ".opus", ".aac", ".aiff", ".wma"]
)
@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.discordapp.com/attachments/1/2/a{ext}?ex=65&is=1&hm=ab",
        "https://files.example.com/music/a{ext}",
    ],
)
def test_a_direct_link_in_an_audio_format_is_a_custom_link(url, ext):
    assert linkutils.identify_url(url.format(ext=ext)) == (
        linkutils.SiteTypes.CUSTOM
    )
