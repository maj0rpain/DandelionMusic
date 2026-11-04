"""Context routes interaction sends through one helper."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.ext import commands

from musicbot.bot import Context


def make_ctx(response_done):
    ctx = object.__new__(Context)
    interaction = SimpleNamespace(
        response=SimpleNamespace(
            is_done=lambda: response_done,
            send_message=AsyncMock(return_value="first"),
        ),
        followup=SimpleNamespace(send=AsyncMock(return_value="followup")),
        original_response=AsyncMock(return_value="original"),
    )
    ctx.interaction = interaction
    return ctx, interaction


def test_response_done_sends_followup():
    ctx, i = make_ctx(True)
    result = asyncio.run(
        ctx._send_to_interaction("hi", original_response=True)
    )
    assert result == "followup"
    i.followup.send.assert_awaited_once_with("hi")
    i.response.send_message.assert_not_awaited()


def test_response_not_done_sends_message():
    ctx, i = make_ctx(False)
    asyncio.run(ctx._send_to_interaction("hi"))
    i.response.send_message.assert_awaited_once_with("hi")
    i.original_response.assert_not_awaited()


def test_response_not_done_returns_send_message_result():
    ctx, i = make_ctx(False)
    assert asyncio.run(ctx._send_to_interaction("hi")) == "first"


def test_response_not_done_returns_original_response():
    ctx, i = make_ctx(False)
    result = asyncio.run(
        ctx._send_to_interaction("hi", original_response=True)
    )
    assert result == "original"
    i.response.send_message.assert_awaited_once_with("hi")


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
