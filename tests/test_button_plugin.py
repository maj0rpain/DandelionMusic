"""Which links the reaction-button plugin offers a button for."""

from types import SimpleNamespace

import pytest

from musicbot import loader
from musicbot.outcome import EnqueueOutcome, OutcomeKind, render
from musicbot.plugins.button import Button
from musicbot.truncation import Truncation, TruncationReason
from discord_fakes import (
    ReactionEffects,
    VoiceGuild,
    admission_settings,
    make_bot,
)
from helpers import plain_song, run

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTU_BE = "https://youtu.be/dQw4w9WgXcQ"
SOUNDCLOUD = "https://soundcloud.com/some-artist/some-track"
SPOTIFY_ID = "4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY = f"https://open.spotify.com/track/{SPOTIFY_ID}"


@pytest.mark.parametrize(
    "link",
    [
        YOUTUBE,
        YOUTU_BE,
        SOUNDCLOUD,
        SPOTIFY,
        "https://on.soundcloud.com/AbC123",
        "https://spotify.link/xyz",
    ],
)
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
    def is_disconnecting(self):
        return False

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.processed = []

    async def process_song(self, checked, user=None):
        self.processed.append(checked.track)
        outcome = self.outcomes[checked.track]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def react(
    outcomes,
    *,
    resolves=True,
    thread=False,
    content=None,
    in_voice=True,
    command_channel=None,
    announce_songs=False,
):
    """Drive one reaction over links whose load gives `outcomes`
    against real admission and the voice fakes, the bot out of voice;
    returns what the controller processed, what was sent, the guild's
    voice events and the panel's `direct_to` calls."""
    sent = []
    message = SimpleNamespace(content=content or " ".join(outcomes))
    channel = FakeChannel(message)
    known = {channel.id: channel}
    controller = FakeController(outcomes)
    sett = admission_settings(
        button_emote="x",
        command_channel=command_channel,
        user_must_be_in_vc=True,
        dj_role=None,
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
    mine = guild.channel("mine", humans=1)

    bot = make_bot({guild: controller}, settings={guild: sett})
    bot.get_guild = lambda _id: guild

    async def get_context(_message):
        return FakeContext(sent, bot, guild, channel)

    bot.get_context = get_context
    member = SimpleNamespace(
        id=5,
        bot=False,
        roles=[],
        voice=SimpleNamespace(channel=mine) if in_voice else None,
    )
    event = SimpleNamespace(
        guild_id=1,
        channel_id=2,
        message_id=3,
        member=member,
        emoji=SimpleNamespace(name="x", id=None),
    )
    run(Button(bot).on_raw_reaction_add(event))
    return ReactionEffects(
        controller.processed,
        sent,
        guild.events,
        bot.sessions.panel(guild).directed,
    )


SILENT_OUTCOME = EnqueueOutcome(OutcomeKind.START_FAILED)


A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def test_failing_link_replies_and_next_link_is_processed():
    effects = react({A: loader.SongError("bad link"), B: SILENT_OUTCOME})
    assert effects.processed == [A, B]
    assert effects.sent == ["bad link"]


def test_reaction_in_a_thread_is_processed():
    assert react({A: SILENT_OUTCOME}, thread=True).processed == [A]


def test_unresolvable_channel_does_nothing():
    effects = react({A: SILENT_OUTCOME}, resolves=False)
    assert effects.processed == []
    assert effects.sent == []


@pytest.mark.parametrize("kind", ["track", "album", "playlist"])
def test_spotify_uri_returns_the_open_link(kind):
    uri = f"spotify:{kind}:{SPOTIFY_ID}"
    assert Button.get_links(f"play {uri}") == [
        f"https://open.spotify.com/{kind}/{SPOTIFY_ID}"
    ]


def test_reacting_to_a_spotify_uri_processes_the_open_link():
    effects = react(
        {SPOTIFY: SILENT_OUTCOME},
        content=f"spotify:track:{SPOTIFY_ID}",
    )
    assert effects.processed == [SPOTIFY]


def test_truncated_playlist_sends_exactly_the_notice():
    cut = Truncation(50, TruncationReason.NO_API)
    outcome = EnqueueOutcome(OutcomeKind.PLAYLIST, count=30, truncation=cut)
    assert react({A: outcome}).sent == [cut.notice(30)]


@pytest.mark.parametrize("announce", [True, False])
def test_started_link_sends_what_render_returns(announce):
    outcome = EnqueueOutcome(OutcomeKind.STARTED, song=plain_song("a"))
    effects = react({A: outcome}, announce_songs=announce)
    expected = render(outcome, announce)
    assert effects.sent == ([] if expected is None else [expected.embed.title])


def test_several_links_send_each_outcome_and_the_error_in_link_order():
    queued = EnqueueOutcome(
        OutcomeKind.QUEUED, song=plain_song("a"), position=2
    )
    playlist = EnqueueOutcome(OutcomeKind.PLAYLIST, count=4)
    effects = react(
        {
            A: queued,
            B: loader.SongError("bad link"),
            SOUNDCLOUD: SILENT_OUTCOME,
            YOUTUBE: playlist,
        }
    )
    assert effects.processed == [A, B, SOUNDCLOUD, YOUTUBE]
    assert effects.sent == [
        render(queued, False).embed.title,
        "bad link",
        render(playlist, False).text,
    ]


def test_reaction_outside_the_command_channel_is_refused_silently():
    effects = react({A: SILENT_OUTCOME}, command_channel=3)
    assert (effects.processed, effects.sent) == ([], [])
    assert effects.events == []


def test_reaction_from_outside_voice_is_refused_silently():
    effects = react({A: SILENT_OUTCOME}, in_voice=False)
    assert (effects.processed, effects.sent) == ([], [])
    assert effects.events == []


def test_reaction_while_idle_connects_and_queues():
    effects = react({A: SILENT_OUTCOME})
    assert effects.processed == [A]
    assert effects.events == [("connect", "mine")]


def test_a_configured_command_channel_is_the_panels_fallback_destination():
    effects = react({A: SILENT_OUTCOME}, command_channel=2)

    ((channel, replace),) = effects.directed
    assert (channel.id, replace) == (2, False)


def test_no_configured_command_channel_directs_the_panel_nowhere():
    assert react({A: SILENT_OUTCOME}, command_channel=None).directed == []


def test_an_unresolved_command_channel_directs_the_panel_nowhere():
    effects = react({A: SILENT_OUTCOME}, command_channel=2, resolves=False)

    assert effects.directed == []
