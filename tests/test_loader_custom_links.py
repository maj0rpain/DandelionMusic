"""loader._load_song's custom-link branch over a faked follower."""

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import RefusedRedirect
from musicbot.loader import SongError

from helpers import fake_redirect_follower

TYPED = "https://public.example/dir/song.mp3"
FINAL = "https://cdn.example/signed/abc123"


def test_redirecting_custom_link_streams_the_final_url(
    monkeypatch, worker_loop
):
    fake_redirect_follower(monkeypatch, FINAL)

    song = loader._load_song(TYPED).songs[0]

    assert (song.url, song.webpage_url, song.title) == (
        FINAL,
        TYPED,
        "song.mp3",
    )


def test_non_redirecting_custom_link_streams_the_typed_url(
    monkeypatch, worker_loop
):
    fake_redirect_follower(monkeypatch, TYPED)

    assert loader._load_song(TYPED).songs[0].url == TYPED


@pytest.mark.parametrize(
    "error",
    [RefusedRedirect("no"), OSError("boom"), TimeoutError()],
)
def test_failing_follower_is_a_songinfo_error(monkeypatch, worker_loop, error):
    fake_redirect_follower(monkeypatch, error)

    with pytest.raises(SongError) as e:
        loader._load_song(TYPED)

    assert str(e.value) == config.SONGINFO_ERROR


def test_reload_resolves_the_typed_link_not_the_stored_stream_url(
    monkeypatch, worker_loop
):
    follower = fake_redirect_follower(monkeypatch, FINAL)
    song = loader._load_song(TYPED).songs[0]
    assert song.url == FINAL

    loader._load_song(song.webpage_url)

    assert follower.calls == [TYPED, TYPED]


def test_short_link_leading_to_a_custom_link_is_followed_once(
    monkeypatch, worker_loop
):
    follower = fake_redirect_follower(monkeypatch, TYPED)

    song = loader._load_song("https://on.soundcloud.com/AbC123").songs[0]

    assert follower.calls == ["https://on.soundcloud.com/AbC123"]
    assert song.url == TYPED
