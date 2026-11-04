"""Small parsers and classifiers: audio tags, stream-url expiry, and
URL routing. Each one decides something the bot then acts on, and each
is a pure function of its input."""

import asyncio

import pytest

from musicbot.audiotags import AudioTags, _parse_year, nice_title
from musicbot import linkutils, loader
from musicbot.linkutils import (
    SiteTypes,
    get_urls,
    identify_url,
    normalize_track,
)
from musicbot.loader import _parse_expire


def tags(**kw):
    return AudioTags(
        **{
            **dict.fromkeys(AudioTags._fields),
            **kw,
        }
    )


class TestParseYear:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2007", 2007),
            ("2007-10-10", 2007),
            ("10/10/2007", 2007),
            ("Released 1995 remaster", 1995),
            (None, None),
            ("", None),
            ("no digits", None),
            ("12", None),
        ],
    )
    def test_takes_the_first_four_digit_run(self, value, expected):
        assert _parse_year(value) == expected


class TestNiceTitle:
    def test_artist_and_title(self):
        assert nice_title(tags(artist="A", title="T"), "fb") == "A - T"

    def test_title_only(self):
        assert nice_title(tags(title="T"), "fb") == "T"

    def test_falls_back_to_the_filename_stem(self):
        assert nice_title(tags(), "fb") == "fb"


class TestParseExpire:
    def test_reads_the_expire_parameter(self):
        assert _parse_expire("https://h/p?expire=1700000000&x=1") == 1700000000

    def test_reads_it_when_first(self):
        assert _parse_expire("https://h/p?expire=123") == 123

    @pytest.mark.parametrize(
        "url",
        [
            "https://h/p",
            "https://h/p?other=1",
            "https://h/p?expire=notanumber",
            "file:///music/a.flac",
        ],
    )
    def test_none_when_absent_or_unparseable(self, url):
        assert _parse_expire(url) is None

    def test_does_not_match_a_suffixed_parameter(self):
        """Partitioning on "&expire=" rather than "expire=" is what
        keeps a parameter like "noexpire=" from being read as one."""
        assert _parse_expire("https://h/p?noexpire=5") is None


class TestIdentifyUrl:
    def test_plain_text_is_not_a_url(self):
        assert identify_url("some song name") is SiteTypes.NOT_URL

    def test_spotify(self):
        assert (
            identify_url("https://open.spotify.com/track/abc123")
            is SiteTypes.SPOTIFY
        )

    def test_a_direct_media_link_is_custom(self):
        assert (
            identify_url("https://example.invalid/a.mp3") is SiteTypes.CUSTOM
        )

    def test_unknown_site(self):
        assert (
            identify_url("https://example.invalid/page.html")
            is SiteTypes.UNKNOWN
        )

    def test_youtube_resolves_to_an_extractor(self):
        result = identify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert not isinstance(result, SiteTypes)

    def test_file_uri_is_gated_on_the_library_being_enabled(self, monkeypatch):
        from config import config

        monkeypatch.setattr(config, "ENABLE_LOCAL_LIBRARY", False)
        assert identify_url("file:///music/a.flac") is SiteTypes.UNKNOWN

        monkeypatch.setattr(config, "ENABLE_LOCAL_LIBRARY", True)
        assert identify_url("file:///music/a.flac") is SiteTypes.LOCAL_LIBRARY


class TestGetUrls:
    def test_extracts_links_from_a_message(self):
        found = get_urls("see https://a.invalid/x and https://b.invalid/y")
        assert len(found) == 2

    def test_no_links(self):
        assert get_urls("nothing here") == []


SPOTIFY_ID = "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("  https://youtu.be/xyz  ", "https://youtu.be/xyz"),
        ("<https://youtu.be/xyz>", "https://youtu.be/xyz"),
        ("  <https://youtu.be/xyz> ", "https://youtu.be/xyz"),
        ("https://youtu.be/xyz.", "https://youtu.be/xyz"),
        ("https://youtu.be/xyz,", "https://youtu.be/xyz"),
        ("https://youtu.be/xyz!", "https://youtu.be/xyz"),
        ("https://youtu.be/xyz?", "https://youtu.be/xyz"),
        ("https://youtu.be/xyz;", "https://youtu.be/xyz"),
        ("https://youtu.be/xyz:", "https://youtu.be/xyz"),
        (
            f"spotify:track:{SPOTIFY_ID}",
            f"https://open.spotify.com/track/{SPOTIFY_ID}",
        ),
        (
            f"spotify:album:{SPOTIFY_ID}",
            f"https://open.spotify.com/album/{SPOTIFY_ID}",
        ),
        (
            f"spotify:playlist:{SPOTIFY_ID}",
            f"https://open.spotify.com/playlist/{SPOTIFY_ID}",
        ),
        (
            f"HTTPS://OPEN.SPOTIFY.COM/track/{SPOTIFY_ID}",
            f"HTTPS://OPEN.SPOTIFY.COM/track/{SPOTIFY_ID}",
        ),
        (
            f"https://open.spotify.com/TRACK/{SPOTIFY_ID}",
            f"https://open.spotify.com/TRACK/{SPOTIFY_ID}",
        ),
        ("never gonna give you up.", "never gonna give you up."),
        ("  never gonna give you up  ", "never gonna give you up"),
        ("hello.", "hello."),
        ("<never gonna give you up>", "<never gonna give you up>"),
    ],
)
def test_normalize_track(text, expected):
    assert normalize_track(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"https://open.spotify.com/track/{SPOTIFY_ID}",
        f"HTTPS://OPEN.SPOTIFY.COM/track/{SPOTIFY_ID}",
        f"Https://Open.Spotify.Com/album/{SPOTIFY_ID}",
    ],
)
def test_spotify_links_are_recognised_whatever_the_case_of_scheme_and_host(
    text,
):
    assert identify_url(text) is SiteTypes.SPOTIFY


def test_spotify_link_with_an_upper_case_type_segment_is_not_spotify():
    assert (
        identify_url(f"https://open.spotify.com/TRACK/{SPOTIFY_ID}")
        is not SiteTypes.SPOTIFY
    )


def test_fetch_spotify_with_credentials_looks_the_track_up_by_id(
    monkeypatch,
):
    calls = []

    class FakeApi:
        def track(self, arg):
            calls.append(arg)
            return {"name": "T", "artists": [{"name": "A"}]}

    monkeypatch.setattr(linkutils, "spotify_api", FakeApi())
    monkeypatch.setattr(
        loader, "_search_youtube", lambda query: [{"url": "u"}]
    )
    asyncio.run(
        linkutils.fetch_spotify(f"HTTPS://OPEN.SPOTIFY.COM/track/{SPOTIFY_ID}")
    )
    assert calls == [SPOTIFY_ID]


def test_load_song_classifies_and_loads_the_cleaned_text(monkeypatch):
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return {"url": "https://example.com/a", "title": "t"}

    extracted = []

    def fake_extract(url, ie=None):
        extracted.append(url)
        return {"url": url, "title": "t"}

    monkeypatch.setattr(loader, "fetch_spotify", fake_fetch)
    monkeypatch.setattr(loader, "extract_info", fake_extract)
    monkeypatch.setattr(loader, "_loop", asyncio.new_event_loop())
    loader._load_song("<https://open.spotify.com/track/x>")
    loader._load_song("https://youtu.be/dQw4w9WgXcQ.")
    assert fetched == ["https://open.spotify.com/track/x"]
    assert extracted[0] == "https://youtu.be/dQw4w9WgXcQ"
