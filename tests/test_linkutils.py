import pytest

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


@pytest.mark.parametrize(
    "url",
    [
        "https://on.soundcloud.com/AbC123",
        "https://ON.SoundCloud.com/AbC123",
        "https://on.soundcloud.com",
        "https://spotify.link/xyz",
        "https://Spotify.Link",
        "https://spotify.app.link/xyz",
        "http://SPOTIFY.APP.LINK/",
    ],
)
def test_shortener_host_is_a_short_link(url):
    assert linkutils.identify_url(url) is linkutils.SiteTypes.SHORT_LINK
    assert linkutils.get_site_type(url) is linkutils.SiteTypes.SHORT_LINK


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/artist/track",
        "https://notspotify.link/xyz",
        "https://on.soundcloud.com.evil.example/x",
    ],
)
def test_lookalike_host_is_not_a_short_link(url):
    assert linkutils.get_site_type(url) is not linkutils.SiteTypes.SHORT_LINK


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
