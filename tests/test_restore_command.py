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
    def __init__(self, outcome):
        self.outcome = outcome

    async def restore(self):
        return self.outcome


@pytest.mark.parametrize(
    "outcome, reply",
    [
        (RestoreResult.RESTORED, "Restored playlist"),
        (RestoreResult.NOTHING_TO_RESTORE, config.QUEUE_EMPTY),
        (
            RestoreResult.REFUSED_WHILE_ACTIVE,
            "Something is already playing - stop it before restoring :x:",
        ),
    ],
)
def test_restore_command_replies_with_the_outcome(outcome, reply):
    sent = []

    async def send(message):
        sent.append(message)

    ctx = types.SimpleNamespace(
        audiocontroller=OutcomeController(outcome), send=send
    )

    asyncio.run(Music._restore.callback(Music(None), ctx))

    assert sent == [reply]
