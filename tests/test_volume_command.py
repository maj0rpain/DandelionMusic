"""d!volume: the reply for an out-of-range value names the accepted range."""

import asyncio
import types

import pytest

from musicbot.commands.music import Music


@pytest.mark.parametrize("value", [-1, 101])
def test_volume_command_rejects_out_of_range_with_the_range(value):
    sent = []

    async def send(message):
        sent.append(message)

    ctx = types.SimpleNamespace(audiocontroller=None, send=send)

    asyncio.run(Music._volume.callback(Music(None), ctx, value))

    assert sent == ["Error: Volume must be a number 0-100"]
