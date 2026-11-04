"""Loader shutdown cancels queued extractions and still waits.

Stub executors stand in for the process pool: no worker is started.
"""

from musicbot import loader


class StubExecutor:
    def __init__(self):
        self.calls = []

    def shutdown(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_shutdown_cancels_queued_futures_and_waits(monkeypatch):
    stubs = [StubExecutor(), StubExecutor()]
    monkeypatch.setattr(
        loader,
        "_pools",
        {loader.Lane.LOAD: stubs[0], loader.Lane.PRELOAD: stubs[1]},
    )

    loader.shutdown()

    expected = [((), {"wait": True, "cancel_futures": True})]
    assert [s.calls for s in stubs] == [expected, expected]
