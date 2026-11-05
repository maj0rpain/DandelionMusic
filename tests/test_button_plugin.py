"""Which links the reaction-button plugin offers a button for."""

import asyncio
from types import SimpleNamespace

import pytest

from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.outcome import EnqueueOutcome, OutcomeKind, render
from musicbot.song import Song
from musicbot.plugins.button import Button
from musicbot.truncation import Truncation, TruncationReason
from discord_fakes import ADMISSION_DJ_ROLE, VoiceGuild, make_bot
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
        embed = kwargs.get("embed")
        self.sent.append(content if embed is None else embed.title)


class FakeController:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.processed = []

    async def process_song(self, url, user=None):
        self.processed.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def react(
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
    announce_songs=False,
):
    """Drive one reaction over links whose load gives `outcomes`
    against real admission and the voice fakes; returns what the
    controller processed and what was sent. `guild` is on the
    function afterwards, for the voice events."""
    sent = []
    message = SimpleNamespace(content=content or " ".join(outcomes))
    channel = FakeChannel(message)
    known = {channel.id: channel}
    controller = FakeController(outcomes)
    sett = SimpleNamespace(
        button_emote="x",
        command_channel=command_channel,
        user_must_be_in_vc=user_must_be_in_vc,
        dj_role=ADMISSION_DJ_ROLE if role == "dj" else None,
        vc_timeout=True,
        announce_songs=announce_songs,
    )

    class Guild(VoiceGuild):
        def get_channel(self, _id):
            # a plain channel lookup does not see threads
            return None if thread or not resolves else channel

        def get_channel_or_thread(self, channel_id):
            # a channel with the id asked for, so the id passed on shows
            if not resolves:
                return None
            fresh = FakeChannel(message, channel_id)
            return known.setdefault(channel_id, fresh)

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
    roles = [SimpleNamespace(id=ADMISSION_DJ_ROLE)] if role == "dj" else []
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
    react.panel = bot.sessions.panel(guild)
    run(Button(bot).on_raw_reaction_add(event))
    return controller.processed, sent


NOTHING = EnqueueOutcome(OutcomeKind.START_FAILED)


def _song(title):
    return Song(
        SiteTypes.YT_DLP, f"https://example.invalid/{title}", title=title
    )


A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def test_failing_link_replies_and_next_link_is_processed():
    processed, sent = react({A: loader.SongError("bad link"), B: NOTHING})
    assert processed == [A, B]
    assert sent == ["bad link"]


def test_reaction_in_a_thread_is_processed():
    processed, _ = react({A: NOTHING}, thread=True)
    assert processed == [A]


def test_unresolvable_channel_does_nothing():
    processed, sent = react({A: NOTHING}, resolves=False)
    assert processed == []
    assert sent == []


@pytest.mark.parametrize("kind", ["track", "album", "playlist"])
def test_spotify_uri_returns_the_open_link(kind):
    uri = f"spotify:{kind}:{SPOTIFY_ID}"
    assert Button.get_links(f"play {uri}") == [
        f"https://open.spotify.com/{kind}/{SPOTIFY_ID}"
    ]


def test_reacting_to_a_spotify_uri_processes_the_open_link():
    processed, _sent = react(
        {SPOTIFY: NOTHING},
        content=f"spotify:track:{SPOTIFY_ID}",
    )
    assert processed == [SPOTIFY]


def test_truncated_playlist_sends_exactly_the_notice():
    cut = Truncation(50, TruncationReason.NO_API)
    outcome = EnqueueOutcome(OutcomeKind.PLAYLIST, count=30, truncation=cut)
    _, sent = react({A: outcome})
    assert sent == [cut.notice(30)]


@pytest.mark.parametrize("announce", [True, False])
def test_started_link_sends_what_render_returns(announce):
    outcome = EnqueueOutcome(OutcomeKind.STARTED, song=_song("a"))
    _, sent = react({A: outcome}, announce_songs=announce)
    expected = render(outcome, announce)
    assert sent == ([] if expected is None else [expected.embed.title])


def test_several_links_send_each_outcome_and_the_error_in_link_order():
    queued = EnqueueOutcome(OutcomeKind.QUEUED, song=_song("a"), position=2)
    playlist = EnqueueOutcome(OutcomeKind.PLAYLIST, count=4)
    processed, sent = react(
        {
            A: queued,
            B: loader.SongError("bad link"),
            SOUNDCLOUD: NOTHING,
            YOUTUBE: playlist,
        }
    )
    assert processed == [A, B, SOUNDCLOUD, YOUTUBE]
    assert sent == [
        "Added to queue - position 2",
        "bad link",
        render(playlist, False).text,
    ]


def test_reaction_outside_the_command_channel_is_refused_silently():
    processed, sent = react({A: NOTHING}, command_channel=3)
    assert (processed, sent) == ([], [])
    assert react.guild.events == []


def test_dj_reacting_from_another_channel_does_not_move_the_bot():
    processed, _ = react({A: NOTHING}, bot_in="busy", role="dj")
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
def test_refused_reaction_is_silent(kwargs):
    processed, sent = react({A: NOTHING}, **kwargs)
    assert (processed, sent) == ([], [])


def test_reaction_while_idle_connects_and_queues():
    processed, _ = react({A: NOTHING})
    assert processed == [A]
    assert react.guild.events == [("connect", "mine")]


def test_reaction_while_the_bot_sits_alone_elsewhere_moves_it(no_sleep):
    processed, _ = react({A: NOTHING}, bot_in="other")
    assert processed == [A]
    assert react.guild.events == [("move", "mine")]


def test_a_configured_command_channel_is_the_panels_fallback_destination():
    react({A: NOTHING}, command_channel=2)

    ((channel, replace),) = react.panel.directed
    assert (channel.id, replace) == (2, False)


def test_no_configured_command_channel_directs_the_panel_nowhere():
    react({A: NOTHING}, command_channel=None)

    assert react.panel.directed == []


def test_an_unresolved_command_channel_directs_the_panel_nowhere():
    react({A: NOTHING}, command_channel=2, resolves=False)

    assert react.panel.directed == []
