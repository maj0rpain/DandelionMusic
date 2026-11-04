"""Context routes interaction sends through one helper."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.ext import commands

from musicbot.bot import Context


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


def test_response_done_sends_followup():
    ctx, interaction = make_ctx(True)
    result = asyncio.run(
        ctx._send_to_interaction("hi", original_response=True)
    )
    assert result == "followup"
    interaction.followup.send.assert_awaited_once_with("hi")
    interaction.response.send_message.assert_not_awaited()


def test_response_not_done_sends_message():
    ctx, interaction = make_ctx(False)
    asyncio.run(ctx._send_to_interaction("hi"))
    interaction.response.send_message.assert_awaited_once_with("hi")
    interaction.original_response.assert_not_awaited()


def test_response_not_done_returns_send_message_result():
    ctx, interaction = make_ctx(False)
    assert asyncio.run(ctx._send_to_interaction("hi")) == "first"


def test_response_not_done_returns_original_response():
    ctx, interaction = make_ctx(False)
    result = asyncio.run(
        ctx._send_to_interaction("hi", original_response=True)
    )
    assert result == "original"
    interaction.response.send_message.assert_awaited_once_with("hi")


def test_no_interaction_view_path_uses_parent_send(monkeypatch):
    ctx = object.__new__(Context)
    ctx.interaction = None
    ctx.guild = object()
    ctx.channel = object()
    parent = AsyncMock(return_value="sent")
    monkeypatch.setattr(commands.Context, "send", parent)

    async def attach_view(send):
        return await send("VIEW")

    controller = SimpleNamespace(command_channel=None, attach_view=attach_view)
    ctx.bot = SimpleNamespace(
        sessions=SimpleNamespace(controller=lambda g: controller)
    )
    assert asyncio.run(ctx.send("x")) == "sent"
    parent.assert_awaited_once_with("x", view="VIEW")


def make_send_ctx(monkeypatch, interaction, controller_channel=None):
    """A Context with a registered controller (None: no controller)."""
    ctx = object.__new__(Context)
    ctx.interaction = interaction
    ctx.guild = object()
    ctx.channel = object()
    parent = AsyncMock(return_value="sent")
    monkeypatch.setattr(commands.Context, "send", parent)

    async def attach_view(send):
        return await send("VIEW")

    controller = SimpleNamespace(
        command_channel=controller_channel,
        attach_view=AsyncMock(side_effect=attach_view),
    )
    ctx.bot = SimpleNamespace(
        sessions=SimpleNamespace(controller=lambda guild: controller)
    )
    return ctx, parent, controller


def make_no_controller_ctx(monkeypatch, interaction):
    ctx, parent, _ = make_send_ctx(monkeypatch, interaction)
    ctx.bot = SimpleNamespace(
        sessions=SimpleNamespace(controller=lambda guild: None)
    )
    return ctx, parent


def test_no_controller_with_interaction_sends_message(monkeypatch):
    interaction = make_interaction(False)
    ctx, parent = make_no_controller_ctx(monkeypatch, interaction)
    assert asyncio.run(ctx.send("x")) == "first"
    interaction.response.send_message.assert_awaited_once_with("x")
    interaction.original_response.assert_not_awaited()
    parent.assert_not_awaited()


def test_no_controller_with_done_interaction_sends_followup(monkeypatch):
    interaction = make_interaction(True)
    ctx, parent = make_no_controller_ctx(monkeypatch, interaction)
    assert asyncio.run(ctx.send("x")) == "followup"
    interaction.followup.send.assert_awaited_once_with("x")
    parent.assert_not_awaited()


def test_no_controller_without_interaction_uses_parent_send(monkeypatch):
    ctx, parent = make_no_controller_ctx(monkeypatch, None)
    assert asyncio.run(ctx.send("x", tts=True)) == "sent"
    parent.assert_awaited_once_with("x", tts=True)


def test_other_triggers_skip_the_view_without_interaction(monkeypatch):
    for kwargs, channel in (
        ({"view": "V"}, None),
        ({"ephemeral": True}, None),
        ({}, object()),
    ):
        ctx, parent, controller = make_send_ctx(monkeypatch, None, channel)
        assert asyncio.run(ctx.send("x", **kwargs)) == "sent"
        parent.assert_awaited_once_with("x", **kwargs)
        controller.attach_view.assert_not_awaited()


def test_other_triggers_skip_the_view_with_interaction(monkeypatch):
    for kwargs, channel in (
        ({"view": "V"}, None),
        ({"ephemeral": True}, None),
        ({}, object()),
    ):
        interaction = make_interaction(False)
        ctx, parent, controller = make_send_ctx(
            monkeypatch, interaction, channel
        )
        assert asyncio.run(ctx.send("x", **kwargs)) == "first"
        interaction.response.send_message.assert_awaited_once_with(
            "x", **kwargs
        )
        parent.assert_not_awaited()
        controller.attach_view.assert_not_awaited()


def test_reference_is_stripped_for_the_parent_send(monkeypatch):
    ctx, parent, _ = make_send_ctx(monkeypatch, None)
    asyncio.run(ctx.send("x", view="V", reference="R"))
    parent.assert_awaited_once_with("x", view="V")


def test_reference_is_stripped_for_the_interaction(monkeypatch):
    interaction = make_interaction(False)
    ctx, _, _ = make_send_ctx(monkeypatch, interaction)
    asyncio.run(ctx.send("x", view="V", reference="R"))
    interaction.response.send_message.assert_awaited_once_with("x", view="V")


def test_view_path_not_done_returns_original_response(monkeypatch):
    interaction = make_interaction(False)
    ctx, _, _ = make_send_ctx(monkeypatch, interaction)
    assert asyncio.run(ctx.send("x")) == "original"
    interaction.response.send_message.assert_awaited_once_with(
        "x", view="VIEW"
    )


def test_view_path_done_returns_followup_result(monkeypatch):
    interaction = make_interaction(True)
    ctx, _, _ = make_send_ctx(monkeypatch, interaction)
    assert asyncio.run(ctx.send("x")) == "followup"
    interaction.followup.send.assert_awaited_once_with("x", view="VIEW")
    interaction.original_response.assert_not_awaited()


def test_response_send_message_with_interaction(monkeypatch):
    interaction = make_interaction(False)
    ctx, parent, _ = make_send_ctx(monkeypatch, interaction)
    assert asyncio.run(ctx.response_send_message("x")) == "first"
    interaction.response.send_message.assert_awaited_once_with("x")
    parent.assert_not_awaited()


def test_response_send_message_without_interaction_goes_via_send(
    monkeypatch,
):
    ctx, parent, controller = make_send_ctx(monkeypatch, None)
    assert asyncio.run(ctx.response_send_message("x")) == "sent"
    controller.attach_view.assert_awaited_once()
    parent.assert_awaited_once_with("x", view="VIEW")
