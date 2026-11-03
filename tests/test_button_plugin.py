"""Which links the reaction-button plugin offers a button for."""

import pytest

from musicbot.plugins.button import Button

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTU_BE = "https://youtu.be/dQw4w9WgXcQ"
SOUNDCLOUD = "https://soundcloud.com/some-artist/some-track"
SPOTIFY = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


@pytest.mark.parametrize("link", [YOUTUBE, YOUTU_BE, SOUNDCLOUD, SPOTIFY])
def test_supported_link_is_returned(link):
    assert Button.get_links(f"listen to {link} now") == [link]


def test_mixed_links_are_all_returned():
    text = f"{SPOTIFY} and {YOUTUBE} and {YOUTU_BE} and {SOUNDCLOUD}"
    assert Button.get_links(text) == [SPOTIFY, YOUTUBE, YOUTU_BE, SOUNDCLOUD]


@pytest.mark.parametrize(
    "text",
    [
        "https://example.invalid/page.html",
        "https://example.invalid/song.mp3",
        "no link here",
    ],
)
def test_unsupported_text_returns_nothing(text):
    assert Button.get_links(text) == []
