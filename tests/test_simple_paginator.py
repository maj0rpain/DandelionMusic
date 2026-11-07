"""What the d!playlist show paginator does when sent, clicked and timed
out, driven from outside through its real button callbacks."""

from types import SimpleNamespace

import discord

from musicbot.views import OwnedView, SimplePaginator
from discord_fakes import EditRecordingMessage
from helpers import run

OWNER = 1


class SendRecordingContext:
    def __init__(self):
        self.author = SimpleNamespace(id=OWNER)
        self.interaction = None
        self.sent = []
        self.message = EditRecordingMessage()

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return self.message


class Response:
    def __init__(self):
        self.calls = []

    async def edit_message(self, **kwargs):
        self.calls.append(("edit_message", kwargs))

    async def defer(self):
        self.calls.append(("defer", {}))


class Click:
    def __init__(self):
        self.user = SimpleNamespace(id=OWNER)
        self.response = Response()


def pages(n):
    return [discord.Embed(title=f"page {n_}") for n_ in range(n)]


def sent(n):
    """A paginator of n pages, already sent."""
    ctx = SendRecordingContext()
    view = SimplePaginator(ctx, pages(n))
    run(view.send())
    return view, ctx


def click(view, button):
    c = Click()

    async def go():
        if await view.interaction_check(c):
            await button.callback(c)

    run(go())
    return c


def disabled(view):
    return view.prev_page.disabled, view.next_page.disabled


def test_multi_page_send_shows_first_page_with_prev_disabled():
    view, ctx = sent(3)

    assert ctx.sent[0]["embed"].title == "page 0"
    assert ctx.sent[0]["view"] is view
    assert disabled(view) == (True, False)


def test_next_to_a_middle_page_enables_both():
    view, _ = sent(3)

    c = click(view, view.next_page)

    name, kwargs = c.response.calls[0]
    assert name == "edit_message"
    assert kwargs["embed"].title == "page 1"
    assert kwargs["view"] is view
    assert disabled(view) == (False, False)


def test_next_to_the_last_page_disables_next():
    view, _ = sent(2)

    c = click(view, view.next_page)

    assert c.response.calls[0][1]["embed"].title == "page 1"
    assert disabled(view) == (False, True)


def test_prev_back_from_the_last_page_enables_next():
    view, _ = sent(2)
    click(view, view.next_page)

    c = click(view, view.prev_page)

    assert c.response.calls[0][1]["embed"].title == "page 0"
    assert disabled(view) == (True, False)


def test_an_edge_click_is_deferred_and_edits_nothing():
    view, _ = sent(2)

    c = click(view, view.prev_page)

    assert [name for name, _ in c.response.calls] == ["defer"]
    assert view.current_page == 0


def test_a_single_page_is_sent_with_no_view():
    view, ctx = sent(1)

    assert len(ctx.sent) == 1
    assert ctx.sent[0]["embed"].title == "page 0"
    assert "view" not in ctx.sent[0]


def test_no_pages_sends_nothing():
    view, ctx = sent(0)

    assert ctx.sent == []


def test_the_timeout_is_180_seconds():
    view, _ = sent(2)

    assert view.timeout == 180


def test_it_does_not_override_the_owned_view_checks():
    assert SimplePaginator.interaction_check is OwnedView.interaction_check
    assert SimplePaginator.on_timeout is OwnedView.on_timeout
