"""d!restore: the reply for each outcome of AudioController.load_backup().

Drives the command callback on a fake context whose controller reports
a fixed outcome, so nothing here needs Discord or playback.
"""

import types

import pytest

from config import config
from musicbot.audiocontroller import RestoreResult
from musicbot.commands.music import Music
from discord_fakes import admission_without_join
from helpers import run


class OutcomeController:
    def __init__(self, outcome, events):
        self.outcome = outcome
        self.events = events

    def load_backup(self):
        self.events.append("load_backup")
        if self.outcome is RestoreResult.RESTORED:
            return RestoreResult.READY
        return self.outcome

    async def start_restored(self):
        self.events.append("start_restored")
        return self.outcome


@pytest.mark.parametrize(
    "outcome, reply",
    [
        (RestoreResult.RESTORED, "Restored playlist"),
        (RestoreResult.NOTHING_TO_RESTORE, config.QUEUE_EMPTY),
        (RestoreResult.REFUSED_WHILE_ACTIVE, config.RESTORE_WHILE_ACTIVE),
        (
            RestoreResult.BACKUP_UNREADABLE,
            config.RESTORE_BACKUP_UNREADABLE,
        ),
    ],
)
def test_restore_command_replies_with_the_outcome(outcome, reply):
    sent = []
    events = []

    async def send(message):
        sent.append(message)

    async def defer(**kwargs):
        events.append(("defer", kwargs.get("ephemeral", False)))

    controller = OutcomeController(outcome, events)

    ctx = types.SimpleNamespace(
        audiocontroller=controller,
        admitted=admission_without_join(
            lambda: controller, lambda: events.append("enter")
        ),
        send=send,
        defer=defer,
    )

    run(Music._restore.callback(Music(None), ctx))

    expected = [("defer", False), "load_backup"]
    if outcome is RestoreResult.RESTORED:
        expected += ["enter", "start_restored"]
    assert events == expected
    assert sent == [reply]
