"""Context routes interaction sends through one helper."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord.ext import commands

from musicbot.bot import Context
from musicbot.player_panel import PlayerPanel
from helpers import run

VIEW_SKIP_TRIGGERS = [
    pytest.param({"view": "V"}, None, id="view"),
    pytest.param({"ephemeral": True}, None, id="ephemeral"),
    pytest.param({}, object(), id="other-channel"),
]


def make_interaction(response_done):
    return SimpleNamespace(
        response=SimpleNamespace(
            is_done=lambda: response_done,
            send_message=AsyncMock(return_value="first"),
        ),
        followup=SimpleNamespace(send=AsyncMock(return_value="followup")),
        original_response=AsyncMock(return_value="original"),
    )


def make_ctx(response_done):
    ctx = object.__new__(Context)
    interaction = make_interaction(response_done)
    ctx.interaction = interaction
    return ctx, interaction


def make_send_ctx(monkeypatch, interaction, destination=None, registered=True):
    """A Context, its patched parent send, and its guild's panel.

    registered=False: no panel is registered for the guild
    (bot.sessions returns None) and None is returned in the panel
    position. registered=True: a real PlayerPanel is, directed to
    `destination` (None: no destination set yet); only its `attach` is
    replaced, sending with the view "VIEW".
    """
    ctx = object.__new__(Context)
    ctx.interaction = interaction
    ctx.guild = object()
    ctx.channel = object()
    parent = AsyncMock(return_value="sent")
    monkeypatch.setattr(commands.Context, "send", parent)

    async def attach(send):
        return await send("VIEW")

    panel = None
    if registered:
        panel = PlayerPanel(object(), lambda: None, lambda: None)
        if destination is not None:
            panel.direct_to(destination)
        panel.attach = AsyncMock(side_effect=attach)
    ctx.bot = SimpleNamespace(
        sessions=SimpleNamespace(panel=lambda guild: panel)
    )
    return ctx, parent, panel


def test_response_done_sends_followup():
    ctx, interaction = make_ctx(True)
    result = run(ctx._send_to_interaction("hi", original_response=True))
    assert result == "followup"
    interaction.followup.send.assert_awaited_once_with("hi")
    interaction.response.send_message.assert_not_awaited()
    interaction.original_response.assert_not_awaited()


def test_response_not_done_sends_message():
    ctx, interaction = make_ctx(False)
    run(ctx._send_to_interaction("hi"))
    interaction.response.send_message.assert_awaited_once_with("hi")
    interaction.original_response.assert_not_awaited()


def test_response_not_done_returns_send_message_result():
    ctx, interaction = make_ctx(False)
    assert run(ctx._send_to_interaction("hi")) == "first"


def test_response_not_done_returns_original_response():
    ctx, interaction = make_ctx(False)
    result = run(ctx._send_to_interaction("hi", original_response=True))
    assert result == "original"
    interaction.response.send_message.assert_awaited_once_with("hi")


def test_no_interaction_view_path_uses_parent_send(monkeypatch):
    ctx, parent, _ = make_send_ctx(monkeypatch, None)
    assert run(ctx.send("x")) == "sent"
    parent.assert_awaited_once_with("x", view="VIEW")


def test_reply_in_the_directed_channel_carries_the_view(monkeypatch):
    ctx, parent, panel = make_send_ctx(monkeypatch, None)
    panel.direct_to(ctx.channel)
    assert run(ctx.send("x")) == "sent"
    parent.assert_awaited_once_with("x", view="VIEW")


def test_no_panel_with_interaction_sends_message(monkeypatch):
    interaction = make_interaction(False)
    ctx, parent, _ = make_send_ctx(monkeypatch, interaction, registered=False)
    assert run(ctx.send("x")) == "first"
    interaction.response.send_message.assert_awaited_once_with("x")
    interaction.original_response.assert_not_awaited()
    parent.assert_not_awaited()


def test_no_panel_without_interaction_uses_parent_send(monkeypatch):
    ctx, parent, _ = make_send_ctx(monkeypatch, None, registered=False)
    assert run(ctx.send("x", tts=True)) == "sent"
    parent.assert_awaited_once_with("x", tts=True)


@pytest.mark.parametrize(
    "kwargs, channel",
    VIEW_SKIP_TRIGGERS,
)
def test_other_triggers_skip_the_view_without_interaction(
    monkeypatch, kwargs, channel
):
    ctx, parent, panel = make_send_ctx(monkeypatch, None, channel)
    assert run(ctx.send("x", **kwargs)) == "sent"
    parent.assert_awaited_once_with("x", **kwargs)
    panel.attach.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs, channel",
    VIEW_SKIP_TRIGGERS,
)
def test_other_triggers_skip_the_view_with_interaction(
    monkeypatch, kwargs, channel
):
    interaction = make_interaction(False)
    ctx, parent, panel = make_send_ctx(monkeypatch, interaction, channel)
    assert run(ctx.send("x", **kwargs)) == "first"
    interaction.response.send_message.assert_awaited_once_with("x", **kwargs)
    parent.assert_not_awaited()
    panel.attach.assert_not_awaited()


def test_reference_is_stripped_for_the_parent_send(monkeypatch):
    ctx, parent, _ = make_send_ctx(monkeypatch, None)
    run(ctx.send("x", view="V", reference="R"))
    parent.assert_awaited_once_with("x", view="V")


def test_reference_is_stripped_for_the_interaction(monkeypatch):
    interaction = make_interaction(False)
    ctx, _, _ = make_send_ctx(monkeypatch, interaction)
    run(ctx.send("x", view="V", reference="R"))
    interaction.response.send_message.assert_awaited_once_with("x", view="V")


def test_view_path_not_done_returns_original_response(monkeypatch):
    interaction = make_interaction(False)
    ctx, _, _ = make_send_ctx(monkeypatch, interaction)
    assert run(ctx.send("x")) == "original"
    interaction.response.send_message.assert_awaited_once_with(
        "x", view="VIEW"
    )
