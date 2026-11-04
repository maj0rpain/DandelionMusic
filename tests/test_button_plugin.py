"""Which links the reaction-button plugin offers a button for."""

import asyncio
from types import SimpleNamespace

import pytest

from musicbot import loader, utils
from musicbot.audiocontroller import QueuedPlaylist
from musicbot.plugins.button import Button
from musicbot.truncation import Truncation, TruncationReason

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


class FakeChannel:
    def __init__(self, message):
        self.message = message

    def permissions_for(self, _me):
        return SimpleNamespace(manage_messages=False)

    async def fetch_message(self, _id):
        return self.message


class FakeContext:
    def __init__(self, sent):
        self.sent = sent
        self.author = None

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class FakeController:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.processed = []
        self.command_channel = object()

    async def register_voice_channel(self, _channel):
        pass

    async def process_song(self, url, user=None):
        self.processed.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def react(monkeypatch, outcomes, *, resolves=True, thread=False, content=None):
    """Drive one reaction over links whose load gives `outcomes`;
    returns what the controller processed and what was sent."""
    sent = []
    message = SimpleNamespace(content=content or " ".join(outcomes))
    channel = FakeChannel(message)
    controller = FakeController(outcomes)
    sett = SimpleNamespace(button_emote="x", command_channel=None)

    class Guild:
        me = object()

        def get_channel(self, _id):
            # a plain channel lookup does not see threads
            return None if thread or not resolves else channel

        def get_channel_or_thread(self, _id):
            return channel if resolves else None

    guild = Guild()

    async def get_context(_message):
        return FakeContext(sent)

    bot = SimpleNamespace(
        get_guild=lambda _id: guild,
        get_context=get_context,
        sessions=SimpleNamespace(
            settings=lambda _g: sett, controller=lambda _g: controller
        ),
    )

    async def ok(_ctx):
        return True

    monkeypatch.setattr(utils, "voice_check", ok)
    member = SimpleNamespace(bot=False, voice=SimpleNamespace(channel=1))
    event = SimpleNamespace(
        guild_id=1,
        channel_id=2,
        message_id=3,
        member=member,
        emoji=SimpleNamespace(name="x", id=None),
    )
    asyncio.run(Button(bot).on_raw_reaction_add(event))
    return controller.processed, sent


A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def test_failing_link_replies_and_next_link_is_processed(monkeypatch):
    processed, sent = react(
        monkeypatch, {A: loader.SongError("bad link"), B: object()}
    )
    assert processed == [A, B]
    assert sent == ["bad link"]


def test_reaction_in_a_thread_is_processed(monkeypatch):
    processed, _ = react(monkeypatch, {A: object()}, thread=True)
    assert processed == [A]


def test_unresolvable_channel_does_nothing(monkeypatch):
    processed, sent = react(monkeypatch, {A: object()}, resolves=False)
    assert processed == []
    assert sent == []


@pytest.mark.parametrize("kind", ["track", "album", "playlist"])
def test_spotify_uri_returns_the_open_link(kind):
    uri = f"spotify:{kind}:4uLU6hMCjMI75M1A2tKUQC"
    assert Button.get_links(f"play {uri}") == [
        f"https://open.spotify.com/{kind}/4uLU6hMCjMI75M1A2tKUQC"
    ]


def test_reacting_to_a_spotify_uri_processes_the_open_link(monkeypatch):
    link = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
    processed, _sent = react(
        monkeypatch,
        {link: object()},
        content="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
    )
    assert processed == [link]


def test_truncated_playlist_sends_exactly_the_notice(monkeypatch):
    cut = Truncation(50, TruncationReason.NO_API)
    _, sent = react(monkeypatch, {A: QueuedPlaylist(30, cut)})
    assert sent == [cut.notice(30)]


def test_untruncated_playlist_sends_nothing(monkeypatch):
    _, sent = react(monkeypatch, {A: QueuedPlaylist(30)})
    assert sent == []
