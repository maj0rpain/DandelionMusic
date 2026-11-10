"""A Discord message is fetched only for the checked track's admitted
link: `_load_song` and `preload` run in process over the real
`DiscordAttachmentsIE` and a fake `HTTPClient`, so a fetch is observable."""

import atexit
from datetime import timedelta

import pytest
from yt_dlp.extractor.common import InfoExtractor

from config import config
from musicbot import loader
from musicbot.loader import Preload, SongError
from musicbot.message_link import CheckedTrack
from musicbot.song import Song
from musicbot.yt_dlp_plugins.extractor import discord as extractor
from helpers import run
from message_link_fakes import LINK, FakeHTTPClient

OTHER = LINK[:-1] + "6"
SHORT = "https://short.test/abc"
HOP = "https://hop.test/x"


class _Hop(InfoExtractor):
    _VALID_URL = r"https://hop\.test/.*"
    IE_NAME = "fakehop"
    target = LINK
    kind = "url"

    def _real_extract(self, url):
        return {
            "_type": type(self).kind,
            "url": type(self).target,
            "ie_key": extractor.DiscordAttachmentsIE.ie_key(),
            "id": "h",
            "title": "hop",
        }


@pytest.fixture
def client(monkeypatch, worker_loop):
    FakeHTTPClient.instances = []
    FakeHTTPClient.login_error = None
    FakeHTTPClient.fetch_error = None
    monkeypatch.setattr(extractor, "HTTPClient", FakeHTTPClient)
    monkeypatch.setattr(extractor, "_client", None)
    monkeypatch.setattr(config, "BOT_TOKEN", "tok")
    monkeypatch.setattr(atexit, "register", lambda *a, **k: None)
    ydl = loader._new_downloader()
    ydl.add_info_extractor(extractor.DiscordAttachmentsIE())
    ydl.add_info_extractor(_Hop())
    monkeypatch.setattr(loader, "_downloader", ydl)
    monkeypatch.setattr(
        loader,
        "identify_url",
        lambda url: _Hop if url == HOP else extractor.DiscordAttachmentsIE,
    )
    _Hop.target = LINK
    _Hop.kind = "url"

    class Fetches:
        @property
        def made(self):
            return [f for c in FakeHTTPClient.instances for f in c.fetches]

    return Fetches()


def _code(info):
    return info.value.args[0]


def test_a_plain_message_link_is_refused_and_never_fetched(client):
    with pytest.raises(SongError) as info:
        loader._load_song(LINK)
    assert _code(info) == config.SONGINFO_UNSUPPORTED
    assert client.made == []


@pytest.mark.parametrize(
    "typed",
    [LINK, f"<{LINK}>", f"  {LINK}. ", LINK.replace("//", "//canary.")],
)
def test_a_checked_tracks_own_link_is_fetched(client, typed):
    from musicbot.linkutils import normalize_track

    result = loader._load_song(CheckedTrack(typed, normalize_track(typed)))
    assert len(result.songs) == 2
    assert len(client.made) == 1


def test_a_short_link_to_a_message_link_is_refused(client, monkeypatch):
    async def resolve(url):
        return LINK

    monkeypatch.setattr(loader, "resolve_short_link", resolve)
    monkeypatch.setattr(
        loader,
        "identify_url",
        lambda url: (
            loader.SiteTypes.SHORT_LINK
            if url == SHORT
            else (extractor.DiscordAttachmentsIE)
        ),
    )
    with pytest.raises(SongError) as info:
        loader._load_song(CheckedTrack(SHORT, LINK))
    assert _code(info) == config.SONGINFO_UNSUPPORTED
    assert client.made == []


@pytest.mark.parametrize("kind", ["url", "url_transparent"])
def test_a_hop_to_a_message_link_fails_without_a_fetch(client, kind):
    _Hop.kind = kind
    with pytest.raises(SongError) as info:
        loader._load_song(HOP)
    assert _code(info) == config.SONGINFO_ERROR
    assert client.made == []


def test_a_checked_tracks_hop_to_a_different_link_fails_without_a_fetch(
    client,
):
    _Hop.target = OTHER
    with pytest.raises(SongError) as info:
        loader._load_song(CheckedTrack(HOP, LINK))
    assert _code(info) == config.SONGINFO_ERROR
    assert client.made == []


def test_after_a_load_that_raised_a_plain_load_is_still_refused(
    client, monkeypatch
):
    FakeHTTPClient.fetch_error = RuntimeError("boom")
    with pytest.raises(SongError):
        loader._load_song(CheckedTrack(LINK, LINK))
    FakeHTTPClient.fetch_error = None
    fetched = len(client.made)

    with pytest.raises(SongError) as info:
        loader._load_song(LINK)
    assert _code(info) == config.SONGINFO_UNSUPPORTED
    assert len(client.made) == fetched


def test_after_a_checked_load_the_hop_check_still_refuses(client):
    loader._load_song(CheckedTrack(LINK, LINK))
    fetched = len(client.made)
    with pytest.raises(SongError):
        loader._load_song(HOP)
    assert len(client.made) == fetched


@pytest.fixture
def in_process(monkeypatch):
    async def load(lane, track):
        return loader._load_song(track)

    monkeypatch.setattr(loader, "_load", load)


def _song(url):
    song = Song(loader.SiteTypes.YT_DLP, webpage_url=LINK)
    song.url = url
    return song


def test_preload_of_an_expired_message_link_page_fails_without_a_fetch(
    client, in_process
):
    expired = loader._now() - timedelta(hours=1)
    url = f"https://cdn/a.mp3?expire={int(expired.timestamp())}"
    assert run(loader.preload(_song(url))) is Preload.FAILED
    assert client.made == []


def test_preload_of_a_message_link_with_a_current_stream_is_current(
    client, in_process
):
    soon = loader._now() + timedelta(hours=5)
    url = f"https://cdn/a.mp3?expire={int(soon.timestamp())}"
    assert run(loader.preload(_song(url))) is Preload.CURRENT
    assert client.made == []
