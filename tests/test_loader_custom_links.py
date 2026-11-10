"""loader._load_song's custom-link branch over a faked follower."""

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import RefusedRedirect
from musicbot.loader import SongError

TYPED = "https://public.example/dir/song.mp3"
FINAL = "https://cdn.example/signed/abc123"


class Follower:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _follower(monkeypatch, result):
    follower = Follower(result)
    monkeypatch.setattr(loader, "resolve_redirects", follower)
    return follower


def test_redirecting_custom_link_streams_the_final_url(
    monkeypatch, worker_loop
):
    _follower(monkeypatch, FINAL)

    song = loader._load_song(TYPED).songs[0]

    assert (song.url, song.webpage_url, song.title) == (
        FINAL,
        TYPED,
        "song.mp3",
    )


def test_non_redirecting_custom_link_streams_the_typed_url(
    monkeypatch, worker_loop
):
    _follower(monkeypatch, TYPED)

    assert loader._load_song(TYPED).songs[0].url == TYPED


@pytest.mark.parametrize(
    "error",
    [RefusedRedirect("no"), OSError("boom"), TimeoutError()],
)
def test_failing_follower_is_a_songinfo_error(monkeypatch, worker_loop, error):
    _follower(monkeypatch, error)

    with pytest.raises(SongError) as e:
        loader._load_song(TYPED)

    assert str(e.value) == config.SONGINFO_ERROR


def test_reload_resolves_the_typed_link_not_the_stored_stream_url(
    monkeypatch, worker_loop
):
    follower = _follower(monkeypatch, FINAL)
    song = loader._load_song(TYPED).songs[0]
    assert song.url == FINAL

    loader._load_song(song.webpage_url)

    assert follower.calls == [TYPED, TYPED]
