"""The loader replaces a broken worker pool and retries the call once.

A fake executor stands in for the process pool: nothing is spawned.
Calls go through the loader's public async API (load_song).
"""

import asyncio
from concurrent.futures.process import BrokenProcessPool

import pytest

from config import config
from musicbot import loader

URL = "https://example.invalid/a.mp3"


class FakePool:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.shutdown_calls = []

    def submit(self, fn, *args):
        from concurrent.futures import Future

        future = Future()
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "break":
            future.set_exception(BrokenProcessPool("worker died"))
        else:
            future.set_result(outcome)
        return future

    def shutdown(self, wait=True, **kwargs):
        self.shutdown_calls.append(wait)


@pytest.fixture
def pools(monkeypatch):
    built = []

    def build(*args, **kwargs):
        pool = FakePool()
        built.append(pool)
        return pool

    monkeypatch.setattr(loader, "ProcessPoolExecutor", build)
    monkeypatch.setattr(loader, "_executor", None)
    return built


def _use(pool):
    loader._executor = pool


def test_a_break_then_a_good_retry_returns_the_retried_result(pools):
    broken = FakePool(["break"])
    _use(broken)
    assert asyncio.run(loader.load_song(URL)) == "ok"
    assert (len(pools), loader._executor is pools[0]) == (1, True)


def test_the_broken_pool_is_shut_down_without_waiting(pools):
    broken = FakePool(["break"])
    _use(broken)
    asyncio.run(loader.load_song(URL))
    assert broken.shutdown_calls == [False]


def test_a_rebuild_prints_one_stderr_line(pools, capsys):
    _use(FakePool(["break"]))
    asyncio.run(loader.load_song(URL))
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_a_break_on_the_retry_raises_loader_crashed(pools):
    _use(FakePool(["break"]))
    pools_outcomes = FakePool(["break"])

    def build(*a, **k):
        pools.append(pools_outcomes)
        return pools_outcomes

    loader.ProcessPoolExecutor = build  # undone by monkeypatch below
    with pytest.raises(loader.SongError) as e:
        asyncio.run(loader.load_song(URL))
    assert str(e.value) == config.LOADER_CRASHED


def test_a_double_break_leaves_a_fresh_pool(pools, monkeypatch):
    _use(FakePool(["break"]))
    made = []

    def build(*a, **k):
        pool = FakePool(["break"] if not made else [])
        made.append(pool)
        return pool

    monkeypatch.setattr(loader, "ProcessPoolExecutor", build)
    with pytest.raises(loader.SongError):
        asyncio.run(loader.load_song(URL))
    assert len(made) == 2
    assert loader._executor is made[1]
    assert made[0].shutdown_calls == [False]


def test_concurrent_calls_on_one_broken_pool_rebuild_once(pools):
    _use(FakePool(["break"] * 4))

    async def many():
        return await asyncio.gather(*[loader.load_song(URL) for _ in range(4)])

    assert asyncio.run(many()) == ["ok"] * 4
    assert len(pools) == 1


def test_a_break_after_shutdown_raises_not_running_and_builds_nothing(
    pools,
):
    class ShutdownDuringCall(FakePool):
        def submit(self, fn, *args):
            loader.shutdown()
            return super().submit(fn, *args)

    _use(ShutdownDuringCall(["break"]))
    with pytest.raises(loader.LoaderNotRunning):
        asyncio.run(loader.load_song(URL))
    assert pools == []
