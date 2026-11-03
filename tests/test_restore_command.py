"""d!restore: the reply for each outcome of AudioController.restore().

Drives the command callback on a fake context whose controller reports
a fixed outcome, so nothing here needs Discord or playback.
"""

import asyncio
import types

import pytest

from config import config
from musicbot.audiocontroller import RestoreResult
from musicbot.commands.music import Music


class OutcomeController:
    def __init__(self, outcome, events):
        self.outcome = outcome
        self.events = events

    async def restore(self):
        self.events.append("restore")
        return self.outcome


@pytest.mark.parametrize(
    "outcome, reply",
    [
        (RestoreResult.RESTORED, "Restored playlist"),
        (RestoreResult.NOTHING_TO_RESTORE, config.QUEUE_EMPTY),
        (RestoreResult.REFUSED_WHILE_ACTIVE, config.RESTORE_WHILE_ACTIVE),
    ],
)
def test_restore_command_replies_with_the_outcome(outcome, reply):
    sent = []
    events = []

    async def send(message):
        sent.append(message)

    async def defer(**kwargs):
        events.append(("defer", kwargs.get("ephemeral", False)))

    ctx = types.SimpleNamespace(
        audiocontroller=OutcomeController(outcome, events),
        send=send,
        defer=defer,
    )

    asyncio.run(Music._restore.callback(Music(None), ctx))

    assert events == [("defer", False), "restore"]
    assert sent == [reply]
