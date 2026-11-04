"""What the d!search results view does when its buttons are clicked
or it times out.

Picking a result used to queue nothing: the pick read view.message,
which discord.py's View (unlike py-cord's) does not provide. These
drive SearchView from outside, at its owner check, a result button's
real callback, and its timeout.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.View needs a running event loop, and the dev group has no
pytest-asyncio.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

from config import config
from musicbot.bot import MusicBot
from musicbot.commands.music import SearchView, SongButton
from musicbot.utils import CheckError
from discord_fakes import FakeInteraction, VoiceGuild, http_error
from helpers import run

OWNER = 1
OTHER = 2
URLS = ["https://youtu.be/one", "https://youtu.be/two"]


class FakeMessage(discord.Message):
    """A bot-sent message. Records every edit; raises from it while
    `fail` is set."""

    def __init__(self, fail=False):
        # no state: discord.Message is only here for isinstance
        self.edits = []
        self.fail = fail

    async def edit(self, **kwargs):
        if self.fail:
            raise http_error()
        self.edits.append(kwargs)


class FakeWebhookMessage(FakeMessage, discord.WebhookMessage):
    """What a deferred slash command's send returns."""


class FakeInteractionResponse(discord.InteractionCallbackResponse):
    """What answering an interaction directly returns: no edit()."""

    def __init__(self):
        pass


class FakeFollowup:
    """Records message edits by id; raises while `fail` is set."""

    def __init__(self, fail=False):
        self.edits = []
        self.fail = fail

    async def edit_message(self, message_id, **kwargs):
        if self.fail:
            raise http_error()
        self.edits.append((message_id, kwargs))


class FakeContext:
    """The bot is already in voice, so a pick needs no join."""

    def __init__(self, user_id=OWNER):
        self.author = SimpleNamespace(id=user_id)
        self.interaction = None
        self.guild = VoiceGuild()
        self.guild.put_bot_in(self.guild.channel("in voice"))

        async def on_command_error(ctx, error):
            # the bot's real handler, with no bot behind it: it uses
            # nothing of self
            await MusicBot.on_command_error(None, ctx, error)

        self.bot = SimpleNamespace(
            on_command_error=on_command_error,
            sessions=SimpleNamespace(
                settings=lambda guild: SimpleNamespace(
                    user_must_be_in_vc=False
                )
            ),
        )
        self.sent = []

        @asynccontextmanager
        async def typing():
            yield

        self.channel = SimpleNamespace(typing=typing)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def bot_out_of_voice(self, connect_error):
        """The bot is in no voice channel, so a pick has to join the
        author's one, and that connect raises `connect_error`."""

        guild = self.guild
        guild.voice_client = None
        guild.fail = connect_error
        self.author.voice = SimpleNamespace(channel=guild.channel("author"))


class FakeCog:
    """Records _play_song calls; its cog_check raises `check_error`
    while it is set."""

    def __init__(self):
        self.played = []
        self.check_error = None
        self.play_error = None

    async def cog_check(self, ctx):
        if self.check_error is not None:
            raise self.check_error
        return True

    async def _play_song(self, ctx, track):
        if self.play_error is not None:
            raise self.play_error
        self.played.append(track)


class SearchInteraction(FakeInteraction):
    """The shared click fake over a FakeContext, with a followup that
    edits messages by id. `followup_fails` and `defer_fails` make
    those calls raise."""

    def __init__(
        self,
        user_id=OWNER,
        custom_id="x",
        followup_fails=False,
        defer_fails=False,
    ):
        ctx = FakeContext(user_id)
        super().__init__(ctx, custom_id, user_id)
        self.ctx = ctx
        self.message = SimpleNamespace(id=99)
        self.followup = FakeFollowup(fail=followup_fails)

        if defer_fails:

            async def defer(**kwargs):
                raise http_error()

            self.response.defer = defer


def make_view(cog=None, message=None):
    cog = cog or FakeCog()
    view = SearchView(FakeContext(OWNER))
    for i, url in enumerate(URLS, start=1):
        view.add_item(SongButton(cog, i, url))
    view.message = message
    return view, cog


def buttons_disabled(view):
    return all(item.disabled for item in view.children)


def buttons_enabled(view):
    return not any(item.disabled for item in view.children)


async def click(view, index, user_id=OWNER, inter=None):
    """What discord.py does with a click: the view's check, then the
    item's callback only when the check admits it. Pass a prebuilt
    interaction to click with one set up to fail."""
    if inter is None:
        inter = SearchInteraction(user_id)
    if await view.interaction_check(inter):
        await view.children[index].callback(inter)
    return inter


# --- Owner check ---


def test_the_owner_is_admitted():
    async def go():
        view, _ = make_view()
        return await view.interaction_check(SearchInteraction(OWNER))

    assert run(go()) is True


def test_a_non_owner_is_told_privately_and_nothing_happens():
    async def go():
        view, cog = make_view(message=FakeMessage())
        inter = await click(view, 0, user_id=OTHER)
        return view, cog, inter

    view, cog, inter = run(go())
    assert inter.messages == [
        (("This belongs to someone else.",), {"ephemeral": True})
    ]
    assert cog.played == []
    assert not view.is_finished()
    assert buttons_enabled(view)


def test_a_second_owner_click_is_deferred_silently_and_refused():
    async def go():
        view, cog = make_view(message=FakeMessage())
        first = SearchInteraction(OWNER)
        second = SearchInteraction(OWNER)
        assert await view.interaction_check(first)
        admitted = await view.interaction_check(second)
        await view.children[0].callback(first)
        return admitted, second, cog

    admitted, second, cog = run(go())
    assert admitted is False
    assert second.deferred
    assert second.messages == []
    assert cog.played == [URLS[0]]


# --- Pick ---


def test_the_owners_pick_queues_its_url_and_disables_the_buttons():
    async def go():
        message = FakeMessage()
        view, cog = make_view(message=message)
        await click(view, 1)
        return view, cog, message

    view, cog, message = run(go())
    assert cog.played == [URLS[1]]
    assert message.edits == [{"view": view}]
    assert buttons_disabled(view)
    assert view.is_finished()


def test_a_pick_failing_the_play_check_is_refused_and_can_be_retried():
    async def go():
        view, cog = make_view(message=FakeMessage())
        cog.check_error = CheckError("Join a voice channel first.")
        inter = await click(view, 0)
        state = (
            list(cog.played),
            inter.ctx.sent,
            view.is_finished(),
            buttons_enabled(view),
        )
        cog.check_error = None
        await click(view, 1)
        return state, list(cog.played)

    (played, sent, finished, enabled), retried = run(go())
    assert played == []
    assert [(str(a[0]), k) for a, k in sent] == [
        ("Join a voice channel first.", {"ephemeral": True})
    ]
    assert not finished
    assert enabled
    assert retried == [URLS[1]]


def test_a_pick_whose_join_fails_is_refused_and_can_be_retried():
    async def go():
        view, cog = make_view(message=FakeMessage())
        inter = SearchInteraction()
        inter.ctx.bot_out_of_voice(connect_error=asyncio.TimeoutError())
        await click(view, 0, inter=inter)
        state = (
            list(cog.played),
            inter.ctx.sent,
            view.is_finished(),
            buttons_enabled(view),
        )
        retry_admitted = await view.interaction_check(SearchInteraction())
        return state, retry_admitted

    (played, sent, finished, enabled), retry_admitted = run(go())
    assert played == []
    assert [(str(a[0]), k) for a, k in sent] == [
        (config.VOICE_CONNECT_FAILED, {"ephemeral": True})
    ]
    assert not finished
    assert enabled
    assert retry_admitted is True


def test_a_pick_failing_after_the_claim_is_used_up_and_reported():
    async def go():
        message = FakeMessage()
        view, cog = make_view(message=message)
        cog.check_error = discord.ClientException("voice join failed")
        inter = await click(view, 0)
        return view, cog, message, inter

    view, cog, message, inter = run(go())
    assert [(str(a[0]), k) for a, k in inter.ctx.sent] == [
        ("voice join failed", {})
    ]
    assert cog.played == []
    assert view.is_finished()
    assert buttons_disabled(view)
    assert message.edits == [{"view": view}]


def test_a_failed_defer_uses_up_the_pick_and_is_reported_once():
    async def go():
        message = FakeMessage()
        view, cog = make_view(message=message)
        inter = SearchInteraction(OWNER, defer_fails=True)
        await click(view, 0, inter=inter)
        return view, cog, message, inter

    view, cog, message, inter = run(go())
    assert len(inter.ctx.sent) == 1
    assert cog.played == []
    assert view.is_finished()
    assert message.edits == [{"view": view}]


def test_a_play_failing_after_the_pick_was_used_up_edits_once_and_reports():
    async def go():
        message = FakeMessage()
        view, cog = make_view(message=message)
        cog.play_error = discord.ClientException("play failed")
        inter = await click(view, 0)
        return view, message, inter

    view, message, inter = run(go())
    assert message.edits == [{"view": view}]
    assert [(str(a[0]), k) for a, k in inter.ctx.sent] == [("play failed", {})]


def test_a_failed_message_edit_still_queues_the_pick():
    async def go():
        view, cog = make_view(message=FakeMessage(fail=True))
        await click(view, 0)
        return cog

    assert run(go()).played == [URLS[0]]


# --- Timeout ---


def test_timeout_disables_the_buttons_and_edits_the_message():
    async def go():
        message = FakeMessage()
        view, _ = make_view(message=message)
        await view.on_timeout()
        return view, message

    view, message = run(go())
    assert buttons_disabled(view)
    assert message.edits == [{"view": view}]


def test_timeout_swallows_a_failed_edit():
    async def go():
        view, _ = make_view(message=FakeMessage(fail=True))
        await view.on_timeout()
        return view

    assert buttons_disabled(run(go()))


def test_timeout_with_no_message_and_no_interaction_edits_nothing():
    async def go():
        view, _ = make_view(message=None)
        await view.on_timeout()
        return view

    assert buttons_disabled(run(go()))


# --- Refresh through the pick's click followup ---


def refreshed_by_pick(message):
    async def go():
        view, cog = make_view(message=message)
        inter = await click(view, 0)
        return cog, inter

    return run(go())


def test_a_pick_with_no_message_refreshes_through_its_followup():
    cog, inter = refreshed_by_pick(None)
    assert cog.played == [URLS[0]]
    assert [m for m, _ in inter.followup.edits] == [99]


def test_a_pick_holding_a_webhook_message_refreshes_through_its_followup():
    message = FakeWebhookMessage()
    cog, inter = refreshed_by_pick(message)
    assert message.edits == []
    assert [m for m, _ in inter.followup.edits] == [99]


def test_a_pick_holding_a_callback_response_refreshes_through_its_followup():
    cog, inter = refreshed_by_pick(FakeInteractionResponse())
    assert [m for m, _ in inter.followup.edits] == [99]


def test_a_failing_followup_edit_still_queues_and_reports_no_error():
    async def go():
        view, cog = make_view(message=None)
        inter = SearchInteraction(OWNER, followup_fails=True)
        await click(view, 0, inter=inter)
        return cog, inter

    cog, inter = run(go())
    assert cog.played == [URLS[0]]
    assert inter.ctx.sent == []


# --- Timeout through the command interaction ---


def slash_timeout(message):
    async def go():
        view, _ = make_view(message=message)
        edited = []

        async def edit_original_response(**kwargs):
            edited.append(kwargs)

        view.ctx.interaction = SimpleNamespace(
            edit_original_response=edit_original_response
        )
        await view.on_timeout()
        return view, edited

    return run(go())


def test_timeout_with_no_message_and_no_click_edits_the_original_response():
    view, edited = slash_timeout(None)
    assert edited == [{"view": view}]


def test_timeout_holding_a_callback_response_edits_the_original_response():
    view, edited = slash_timeout(FakeInteractionResponse())
    assert edited == [{"view": view}]
