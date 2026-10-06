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

from musicbot.commands.music import SearchView, SongButton
from musicbot.utils import CheckError

OWNER = 1
OTHER = 2
URLS = ["https://youtu.be/one", "https://youtu.be/two"]


def http_error():
    return discord.HTTPException(
        SimpleNamespace(status=500, reason="Server Error"), "boom"
    )


class FakeMessage:
    """Records every edit; raises from it while `fail` is set."""

    def __init__(self, fail=False):
        self.edits = []
        self.fail = fail

    async def edit(self, **kwargs):
        if self.fail:
            raise http_error()
        self.edits.append(kwargs)


class FakeContext:
    def __init__(self, user_id=OWNER):
        self.author = SimpleNamespace(id=user_id)
        self.interaction = None
        self.sent = []

        @asynccontextmanager
        async def typing():
            yield

        self.channel = SimpleNamespace(typing=typing)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeCog:
    """Records _play_song calls; its cog_check raises `check_error`
    while it is set."""

    def __init__(self):
        self.played = []
        self.check_error = None

    async def cog_check(self, ctx):
        if self.check_error is not None:
            raise self.check_error
        return True

    async def _play_song(self, ctx, track):
        self.played.append(track)


class FakeInteraction:
    def __init__(self, user_id=OWNER, custom_id="x"):
        self.user = SimpleNamespace(id=user_id)
        self.guild = None
        self.data = {"custom_id": custom_id}
        self.ctx = FakeContext(user_id)
        self.messages = []
        self.deferred = False

        async def send_message(*args, **kwargs):
            self.messages.append((args, kwargs))

        async def defer(**kwargs):
            self.deferred = True

        self.response = SimpleNamespace(send_message=send_message, defer=defer)

        async def get_context(inter):
            return self.ctx

        self.client = SimpleNamespace(
            get_context=get_context,
            sessions=SimpleNamespace(controller=lambda guild: None),
        )


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


def run(coro_fn):
    return asyncio.run(coro_fn())


async def click(view, index, user_id=OWNER):
    """What discord.py does with a click: the view's check, then the
    item's callback only when the check admits it."""
    inter = FakeInteraction(user_id)
    if await view.interaction_check(inter):
        await view.children[index].callback(inter)
    return inter


# --- Owner check ---


def test_the_owner_is_admitted():
    async def go():
        view, _ = make_view()
        return await view.interaction_check(FakeInteraction(OWNER))

    assert run(go) is True


def test_a_non_owner_is_told_privately_and_nothing_happens():
    async def go():
        view, cog = make_view(message=FakeMessage())
        inter = await click(view, 0, user_id=OTHER)
        return view, cog, inter

    view, cog, inter = run(go)
    assert inter.messages == [
        (("This belongs to someone else.",), {"ephemeral": True})
    ]
    assert cog.played == []
    assert not view.is_finished()
    assert buttons_enabled(view)


def test_a_second_owner_click_is_deferred_silently_and_refused():
    async def go():
        view, cog = make_view(message=FakeMessage())
        first = FakeInteraction(OWNER)
        second = FakeInteraction(OWNER)
        assert await view.interaction_check(first)
        admitted = await view.interaction_check(second)
        await view.children[0].callback(first)
        return admitted, second, cog

    admitted, second, cog = run(go)
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

    view, cog, message = run(go)
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
        retry_admitted = await view.interaction_check(FakeInteraction())
        return state, retry_admitted

    (played, sent, finished, enabled), retry_admitted = run(go)
    assert played == []
    assert [(str(a[0]), k) for a, k in sent] == [
        ("Join a voice channel first.", {"ephemeral": True})
    ]
    assert not finished
    assert enabled
    assert retry_admitted is True


def test_a_failed_message_edit_still_queues_the_pick():
    async def go():
        view, cog = make_view(message=FakeMessage(fail=True))
        await click(view, 0)
        return cog

    assert run(go).played == [URLS[0]]


# --- Timeout ---


def test_timeout_disables_the_buttons_and_edits_the_message():
    async def go():
        message = FakeMessage()
        view, _ = make_view(message=message)
        await view.on_timeout()
        return view, message

    view, message = run(go)
    assert buttons_disabled(view)
    assert message.edits == [{"view": view}]


def test_timeout_swallows_a_failed_edit():
    async def go():
        view, _ = make_view(message=FakeMessage(fail=True))
        await view.on_timeout()
        return view

    assert buttons_disabled(run(go))


def test_timeout_with_no_message_and_no_interaction_edits_nothing():
    async def go():
        view, _ = make_view(message=None)
        await view.on_timeout()
        return view

    assert buttons_disabled(run(go))
