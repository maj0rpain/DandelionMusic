import pytest

from musicbot import linkutils
from helpers import imported_names


def test_linkutils_does_not_import_the_loader():
    assert "musicbot.loader" not in imported_names(linkutils)


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
