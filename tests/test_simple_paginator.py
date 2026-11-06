"""What the d!playlist show paginator does when sent, clicked and timed
out, driven from outside through its real button callbacks."""

from types import SimpleNamespace

import discord

from musicbot.views import OwnedView, SimplePaginator
from helpers import run

OWNER = 1
OTHER = 2


class EditRecordingMessage(discord.Message):
    def __init__(self):
        # no state: discord.Message is only here for isinstance
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class SendRecordingContext:
    def __init__(self, message=None):
        self.author = SimpleNamespace(id=OWNER)
        self.interaction = None
        self.sent = []
        self.message = message or EditRecordingMessage()

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

    async def send_message(self, *args, **kwargs):
        self.calls.append(("send_message", (args, kwargs)))


class Followup:
    def __init__(self):
        self.edits = []

    async def edit_message(self, message_id, **kwargs):
        self.edits.append((message_id, kwargs))


class Click:
    def __init__(self, user_id=OWNER):
        self.user = SimpleNamespace(id=user_id)
        self.response = Response()
        self.followup = Followup()
        self.message = SimpleNamespace(id=555)


def pages(n):
    return [discord.Embed(title=f"page {n_}") for n_ in range(n)]


def sent(n, message=None):
    """A paginator of n pages, already sent."""
    ctx = SendRecordingContext(message)
    view = SimplePaginator(ctx, pages(n))
    run(view.send())
    return view, ctx


def click(view, button, who=OWNER):
    c = Click(who)

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


def test_a_non_owner_is_refused_and_the_page_stays():
    view, _ = sent(3)

    c = click(view, view.next_page, who=OTHER)

    name, (args, kwargs) = c.response.calls[0]
    assert name == "send_message"
    assert args == ("This belongs to someone else.",)
    assert kwargs == {"ephemeral": True}
    assert len(c.response.calls) == 1
    assert view.current_page == 0


def test_timeout_without_a_click_disables_the_prefix_message():
    view, ctx = sent(3)

    run(view.on_timeout())

    assert disabled(view) == (True, True)
    assert ctx.message.edits == [{"view": view}]


def test_timeout_after_a_click_edits_through_its_followup():
    # a slash-sent listing hands back no discord.Message
    view, ctx = sent(3, message=SimpleNamespace())
    c = click(view, view.next_page)

    run(view.on_timeout())

    assert disabled(view) == (True, True)
    assert c.followup.edits == [(555, {"view": view})]
