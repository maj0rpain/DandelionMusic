"""Which links the reaction-button plugin offers a button for."""

import asyncio
from types import SimpleNamespace

import pytest

from musicbot import loader
from musicbot.audiocontroller import QueuedPlaylist
from musicbot.plugins.button import Button
from musicbot.truncation import Truncation, TruncationReason
from discord_fakes import VoiceGuild, make_bot
from helpers import run

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTU_BE = "https://youtu.be/dQw4w9WgXcQ"
SOUNDCLOUD = "https://soundcloud.com/some-artist/some-track"
SPOTIFY_ID = "4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY = f"https://open.spotify.com/track/{SPOTIFY_ID}"


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


CMD_CHANNEL = 2
DJ_ROLE = 99


class FakeChannel:
    def __init__(self, message, id=CMD_CHANNEL):
        self.message = message
        self.id = id

    def permissions_for(self, _member):
        return SimpleNamespace(manage_messages=False, administrator=False)

    async def fetch_message(self, _id):
        return self.message


class FakeContext:
    def __init__(self, sent, bot, guild, channel):
        self.sent = sent
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.command = None
        self.author = None

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class FakeController:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.processed = []
        self.command_channel = object()

    async def process_song(self, url, user=None):
        self.processed.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def react(
    monkeypatch,
    outcomes,
    *,
    resolves=True,
    thread=False,
    content=None,
    bot_in=None,  # None (idle), "mine" (the reactor's), "other" or "busy"
    reactor_in="mine",  # "mine" or None (out of voice)
    command_channel=None,
    reactor_perms=None,
    role=None,
    user_must_be_in_vc=True,
    fail=None,
):
    """Drive one reaction over links whose load gives `outcomes`
    against real admission and the voice fakes; returns what the
    controller processed and what was sent. `guild` and `ctx` are on
    the function afterwards, for the voice events."""
    sent = []
    message = SimpleNamespace(content=content or " ".join(outcomes))
    channel = FakeChannel(message)
    controller = FakeController(outcomes)
    sett = SimpleNamespace(
        button_emote="x",
        command_channel=command_channel,
        user_must_be_in_vc=user_must_be_in_vc,
        dj_role=DJ_ROLE if role == "dj" else None,
        vc_timeout=True,
    )

    class Guild(VoiceGuild):
        def get_channel(self, _id):
            # a plain channel lookup does not see threads
            return None if thread or not resolves else channel

        def get_channel_or_thread(self, _id):
            return channel if resolves else None

    guild = Guild()
    mine = guild.channel("mine", humans=1, **(reactor_perms or {}))
    if bot_in == "mine":
        guild.put_bot_in(mine)
    elif bot_in == "other":
        guild.put_bot_in(guild.channel("other", bots=1))
    elif bot_in == "busy":
        guild.put_bot_in(guild.channel("busy", humans=1))
    guild.fail = fail

    bot = make_bot({guild: controller}, settings={guild: sett})
    bot.get_guild = lambda _id: guild

    async def get_context(_message):
        return FakeContext(sent, bot, guild, channel)

    bot.get_context = get_context
    roles = [SimpleNamespace(id=DJ_ROLE)] if role == "dj" else []
    member = SimpleNamespace(
        id=5,
        bot=False,
        roles=roles,
        voice=SimpleNamespace(channel=mine) if reactor_in else None,
    )
    event = SimpleNamespace(
        guild_id=1,
        channel_id=2,
        message_id=3,
        member=member,
        emoji=SimpleNamespace(name="x", id=None),
    )
    react.guild = guild
    run(Button(bot).on_raw_reaction_add(event))
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
    uri = f"spotify:{kind}:{SPOTIFY_ID}"
    assert Button.get_links(f"play {uri}") == [
        f"https://open.spotify.com/{kind}/{SPOTIFY_ID}"
    ]


def test_reacting_to_a_spotify_uri_processes_the_open_link(monkeypatch):
    processed, _sent = react(
        monkeypatch,
        {SPOTIFY: object()},
        content=f"spotify:track:{SPOTIFY_ID}",
    )
    assert processed == [SPOTIFY]


def test_truncated_playlist_sends_exactly_the_notice(monkeypatch):
    cut = Truncation(50, TruncationReason.NO_API)
    _, sent = react(monkeypatch, {A: QueuedPlaylist(30, cut)})
    assert sent == [cut.notice(30)]


def test_untruncated_playlist_sends_nothing(monkeypatch):
    _, sent = react(monkeypatch, {A: QueuedPlaylist(30)})
    assert sent == []


def test_reaction_outside_the_command_channel_is_refused_silently(
    monkeypatch,
):
    processed, sent = react(monkeypatch, {A: object()}, command_channel=3)
    assert (processed, sent) == ([], [])
    assert react.guild.events == []


def test_dj_reacting_from_another_channel_does_not_move_the_bot(
    monkeypatch,
):
    processed, _ = react(monkeypatch, {A: object()}, bot_in="busy", role="dj")
    assert processed == [A]
    assert react.guild.events == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reactor_perms": {"connect": False}},
        {"reactor_perms": {"speak": False}},
        {"reactor_in": None},
        {"fail": asyncio.TimeoutError()},
    ],
    ids=["no connect", "no speak", "not in voice", "failed join"],
)
def test_refused_reaction_is_silent(monkeypatch, kwargs):
    processed, sent = react(monkeypatch, {A: object()}, **kwargs)
    assert (processed, sent) == ([], [])


def test_reaction_while_idle_connects_and_queues(monkeypatch):
    processed, _ = react(monkeypatch, {A: object()})
    assert processed == [A]
    assert react.guild.events == [("connect", "mine")]


def test_reaction_while_the_bot_sits_alone_elsewhere_moves_it(
    monkeypatch, no_sleep
):
    processed, _ = react(monkeypatch, {A: object()}, bot_in="other")
    assert processed == [A]
    assert react.guild.events == [("move", "mine")]


@pytest.fixture
def no_sleep(monkeypatch):
    async def sleep(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", sleep)
