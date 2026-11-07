"""Small parsers and classifiers: audio tags, stream-url expiry, and
URL routing. Each one decides something the bot then acts on, and each
is a pure function of its input."""

import pytest

from musicbot.audiotags import AudioTags, _parse_year, nice_title
from musicbot.linkutils import (
    SiteTypes,
    get_site_type,
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
    @pytest.mark.parametrize(
        "text",
        [
            "re:zero",
            "nier:automata",
            "c418:sweden",
            "localhost:8000/a.mp3",
        ],
    )
    def test_colon_text_without_a_scheme_is_not_a_url(self, text):
        assert identify_url(text) is SiteTypes.NOT_URL

    @pytest.mark.parametrize(
        "text, ie_name",
        [
            ("ytsearch:foo", "youtube:search"),
            ("scsearch:foo", "soundcloud:search"),
        ],
    )
    def test_search_prefixes_resolve_to_their_extractor(self, text, ie_name):
        result = identify_url(text)
        assert not isinstance(result, SiteTypes)
        assert result.IE_NAME == ie_name

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

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.invalid/a.mp3",
            "HTTPS://example.invalid/a.mp3",
        ],
    )
    def test_http_and_https_links_in_any_case_are_custom(self, url):
        assert identify_url(url) is SiteTypes.CUSTOM

    @pytest.mark.parametrize(
        "url",
        [
            "file:/srv/private/x.mp3",
            "concat:/a|/b.mp3",
            "ftp://host/a.mp3",
            "subfile:/a.mp3",
            "http://127.0.0.1:8080/a.mp3",
            "http://localhost/a.mp3",
            "http://LOCALHOST./a.mp3",
            "http://[::1]/a.mp3",
            "http://10.0.0.1/a.mp3",
            "http://192.168.1.1/a.mp3",
            "http://169.254.169.254/a.mp3",
            "http://[fe80::1]/a.mp3",
            "http://0.0.0.0/a.mp3",
            "http://[::]/a.mp3",
            "http://[::ffff:127.0.0.1]/a.mp3",
            "http://2130706433/a.mp3",
            "http://127.1/a.mp3",
            "http://0x7f000001/a.mp3",
            "http://0177.0.0.1/a.mp3",
        ],
    )
    def test_a_refused_direct_media_link_is_unknown(self, url):
        assert identify_url(url) is SiteTypes.UNKNOWN

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
    def test_shortener_host_is_a_short_link(self, url):
        assert identify_url(url) is SiteTypes.SHORT_LINK
        assert get_site_type(url) is SiteTypes.SHORT_LINK

    @pytest.mark.parametrize(
        "url",
        [
            "https://soundcloud.com/artist/track",
            "https://notspotify.link/xyz",
            "https://on.soundcloud.com.evil.example/x",
        ],
    )
    def test_lookalike_host_is_not_a_short_link(self, url):
        assert get_site_type(url) is not SiteTypes.SHORT_LINK

    @pytest.mark.parametrize(
        "ext", [".flac", ".m4a", ".opus", ".aac", ".aiff", ".wma"]
    )
    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.discordapp.com/attachments/1/2/a{ext}"
            "?ex=65&is=1&hm=ab",
            "https://files.example.com/music/a{ext}",
        ],
    )
    def test_a_direct_link_in_an_audio_format_is_a_custom_link(self, url, ext):
        assert identify_url(url.format(ext=ext)) == SiteTypes.CUSTOM


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
        (
            f"spotify:track:{SPOTIFY_ID}.",
            f"https://open.spotify.com/track/{SPOTIFY_ID}",
        ),
        (
            f"SPOTIFY:track:{SPOTIFY_ID}",
            f"https://open.spotify.com/track/{SPOTIFY_ID}",
        ),
        (
            f"Spotify:album:{SPOTIFY_ID}.",
            f"https://open.spotify.com/album/{SPOTIFY_ID}",
        ),
        ("never gonna give you up.", "never gonna give you up."),
        ("  never gonna give you up  ", "never gonna give you up"),
        ("hello.", "hello."),
        ("<never gonna give you up>", "<never gonna give you up>"),
        ("<hello>", "<hello>"),
        ("Re:Zero!", "Re:Zero!"),
        ("ft:song.", "ft:song."),
        ("mix:tape?", "mix:tape?"),
        ("<Re:Zero>", "<Re:Zero>"),
        ("<https://youtu.be/xyz>.", "https://youtu.be/xyz"),
        ("<https://youtu.be/xyz.>", "https://youtu.be/xyz"),
        ("<https://youtu.be/xyz.>!", "https://youtu.be/xyz"),
        ("<Re:Zero>!", "<Re:Zero>!"),
        ("FILE:///lib/A/B/t.mp3", "file:///lib/A/B/t.mp3"),
        ("File:///lib/A/B/T.MP3", "file:///lib/A/B/T.MP3"),
        ("file:///lib/A/B/t.mp3", "file:///lib/A/B/t.mp3"),
        ("<FILE:///lib/A/B/t.mp3>", "file:///lib/A/B/t.mp3"),
        ("  File:///lib/A/B/t.mp3.", "file:///lib/A/B/t.mp3"),
        ("<File:///lib/A/B/t.mp3>!", "file:///lib/A/B/t.mp3"),
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


@pytest.mark.parametrize("kind", ["track", "album", "playlist"])
def test_spotify_link_of_every_type_is_spotify(kind):
    assert (
        identify_url(f"https://open.spotify.com/{kind}/{SPOTIFY_ID}")
        is SiteTypes.SPOTIFY
    )


def test_spotify_uri_with_an_upper_case_type_is_not_rewritten():
    uri = f"spotify:TRACK:{SPOTIFY_ID}"
    assert normalize_track(uri) == uri
