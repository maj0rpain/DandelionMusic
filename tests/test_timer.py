"""The inactivity Timer.

Timer.cancel() is called from inside the timer's own callback -
timeout_handler() -> udisconnect() -> timer.cancel() - so "cancel" has
to mean "drop the pending timeout" without killing the coroutine doing
the asking.
"""

import asyncio

import pytest

from config import config
from musicbot.utils import Timer


@pytest.fixture(autouse=True)
def instant_timeout(monkeypatch):
    monkeypatch.setattr(config, "VC_TIMEOUT", 0)


def run(coro):
    return asyncio.run(coro)


def test_callback_runs_to_completion_when_it_cancels_its_own_timer():
    """The regression this exists for: cancelling from inside the
    callback used to raise CancelledError at the callback's next
    await, so udisconnect() tore down its state and then never
    reached voice_client.disconnect() - the bot stayed in the voice
    channel after every inactivity timeout."""
    steps = []

    async def callback():
        steps.append("start")
        timer.cancel()
        # udisconnect() awaits several times after cancelling: the
        # disconnect announcement, then the disconnect itself
        await asyncio.sleep(0)
        steps.append("after await")
        await asyncio.sleep(0)
        steps.append("disconnected")

    timer = Timer(callback)

    async def main():
        await timer.start()
        await asyncio.shield(timer._task)

    run(main())
    assert steps == ["start", "after await", "disconnected"]


def test_cancel_clears_the_pending_task():
    async def callback():
        pass

    timer = Timer(callback)

    async def main():
        await timer.start()
        assert timer._task is not None
        timer.cancel()
        assert timer._task is None

    run(main())


def test_cancel_from_outside_stops_the_callback_firing():
    """The ordinary case still has to work: a timer cancelled before
    it fires must not run its callback."""
    fired = []

    async def callback():
        fired.append(True)

    async def main():
        timer = Timer(callback)
        monkey = 0.05
        config.VC_TIMEOUT = monkey
        await timer.start()
        timer.cancel()
        await asyncio.sleep(0.1)

    run(main())
    assert fired == []


def test_cancel_is_safe_with_no_pending_task():
    async def callback():
        pass

    async def main():
        Timer(callback).cancel()

    run(main())
