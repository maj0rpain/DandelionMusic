"""The suite's generic shared helpers: the bounded `run(coro)` and
`FakeClock`."""

import asyncio

from musicbot.linkutils import SiteTypes
from musicbot.song import Song


def run(coro):
    """Run a coroutine to completion, bounded by a 5 s timeout.

    The tests are synchronous functions running their coroutine through
    asyncio.run(): the dev group pins pytest with no asyncio_mode and no
    pytest-asyncio, so an async test would be collected, skipped with a
    warning, and silently pass.
    """
    # bounded, so a waiter left unresolved fails the test, not the run
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


def plain_song(title="a"):
    """A plain YT-DLP `Song` with a title."""
    return Song(
        SiteTypes.YT_DLP, f"https://example.invalid/{title}", title=title
    )


class FakeClock:
    """A controllable clock: calling it returns `now`."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now
