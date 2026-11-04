"""Loader shutdown cancels queued extractions and still waits.

A stub executor stands in for the process pool: no worker is started.
"""

from musicbot import loader


class StubExecutor:
    def __init__(self):
        self.calls = []

    def shutdown(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_shutdown_cancels_queued_futures_and_waits(monkeypatch):
    stub = StubExecutor()
    monkeypatch.setattr(loader, "_executor", stub)

    loader.shutdown()

    assert stub.calls == [((), {"wait": True, "cancel_futures": True})]
