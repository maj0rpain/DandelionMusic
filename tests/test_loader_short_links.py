"""loader._load_song over a faked redirect follower."""

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SpotifyTrack
from musicbot.loader import SongError

from helpers import fake_redirect_follower

SHORT = "https://on.soundcloud.com/AbC123"
SC_TRACK = "https://soundcloud.com/artist/track?si=abc"
SC_SET = "https://soundcloud.com/artist/sets/album"


@pytest.fixture
def extracted(monkeypatch):
    calls = []
    out = {"data": None}

    def extract_info(url, ie=None):
        calls.append(url)
        return out["data"]

    monkeypatch.setattr(loader, "extract_info", extract_info)
    return calls, out


def test_short_link_to_a_track_loads_the_resolved_url(
    monkeypatch, worker_loop, extracted
):
    calls, out = extracted
    out["data"] = {"url": "stream", "title": "T"}
    fake_redirect_follower(monkeypatch, SC_TRACK)

    result = loader._load_song(SHORT)

    assert calls == [SC_TRACK]
    assert result.songs[0].webpage_url == SC_TRACK


def test_extractor_webpage_url_replaces_the_resolved_url(
    monkeypatch, worker_loop, extracted
):
    calls, out = extracted
    out["data"] = {"url": "s", "webpage_url": "https://soundcloud.com/a/t"}
    fake_redirect_follower(monkeypatch, SC_TRACK)

    result = loader._load_song(SHORT)

    assert result.songs[0].webpage_url == "https://soundcloud.com/a/t"


def test_short_link_to_a_set_queues_every_entry(
    monkeypatch, worker_loop, extracted
):
    calls, out = extracted
    out["data"] = {"entries": [{"url": f"https://x/{i}"} for i in range(3)]}
    fake_redirect_follower(monkeypatch, SC_SET)

    result = loader._load_song(SHORT)

    assert calls == [SC_SET]
    assert len(result.songs) == 3
    assert SHORT not in [s.webpage_url for s in result.songs]


def test_short_link_to_a_spotify_track(monkeypatch, worker_loop, extracted):
    resolved = "https://open.spotify.com/track/t"
    fetched = []

    async def fetch_spotify(url):
        fetched.append(url)
        return SpotifyTrack("Song Artist")

    monkeypatch.setattr(loader, "fetch_spotify", fetch_spotify)
    monkeypatch.setattr(
        loader,
        "_search_youtube",
        lambda q: [{"url": "https://yt/v", "webpage_url": "https://yt/v"}],
    )
    fake_redirect_follower(monkeypatch, resolved)
    extracted[1]["data"] = {"webpage_url": "https://yt/v"}

    result = loader._load_song("https://spotify.link/xyz")

    assert fetched == [resolved]
    assert [s.webpage_url for s in result.songs] == ["https://yt/v"]


def test_raising_follower_is_a_songinfo_error(
    monkeypatch, worker_loop, extracted
):
    fake_redirect_follower(monkeypatch, OSError("boom"))

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_ERROR


def test_resolved_unknown_url_is_unsupported(
    monkeypatch, worker_loop, extracted
):
    fake_redirect_follower(monkeypatch, "https://example.com/page")

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_UNSUPPORTED


def test_resolved_non_url_is_unsupported_not_searched(
    monkeypatch, worker_loop, extracted
):
    fake_redirect_follower(monkeypatch, "just some words")
    searched = []
    monkeypatch.setattr(
        loader, "_search_youtube", lambda q: searched.append(q) or [{}]
    )

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_UNSUPPORTED
    assert searched == []


def test_resolved_short_link_is_unsupported_after_one_resolution(
    monkeypatch, worker_loop, extracted
):
    follower = fake_redirect_follower(
        monkeypatch, "https://spotify.link/other"
    )

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_UNSUPPORTED
    assert len(follower.calls) == 1


def test_unknown_url_off_the_list_never_calls_the_follower(
    monkeypatch, worker_loop, extracted
):
    follower = fake_redirect_follower(monkeypatch, SC_TRACK)

    with pytest.raises(SongError) as e:
        loader._load_song("https://example.com/")

    assert str(e.value) == config.SONGINFO_UNSUPPORTED
    assert follower.calls == []


def test_resolved_discord_message_link_is_unsupported_not_extracted(
    monkeypatch, worker_loop, extracted
):
    calls, _ = extracted
    fake_redirect_follower(monkeypatch, "https://discord.com/channels/1/2/3")

    with pytest.raises(SongError) as e:
        loader._load_song(SHORT)

    assert str(e.value) == config.SONGINFO_UNSUPPORTED
    assert calls == []
