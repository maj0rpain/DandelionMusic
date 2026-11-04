"""The loader replaces a broken worker pool and retries the call once.

A fake executor stands in for the process pool: nothing is spawned.
Calls go through the loader's async API (load_song, and _load for a lane).
"""

import asyncio
from concurrent.futures.process import BrokenProcessPool

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.song import Song

URL = "https://example.invalid/a.mp3"


class FakePool:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.shutdown_calls = []

    def submit(self, fn, *args):
        from concurrent.futures import Future

        future = Future()
        outcome = (
            self.outcomes.pop(0)
            if self.outcomes
            else Song(SiteTypes.YT_DLP, URL, title="ok")
        )
        if isinstance(outcome, str) and outcome == "break":
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
    monkeypatch.setattr(loader, "_pools", {})
    return built


def _use(pool, lane=loader.LOAD_LANE):
    loader._pools[lane] = pool


def test_a_break_then_a_good_retry_returns_the_retried_result(pools):
    broken = FakePool(["break"])
    _use(broken)
    assert asyncio.run(loader.load_song(URL)).title == "ok"
    in_load_lane = loader._pools[loader.LOAD_LANE] is pools[0]
    assert (len(pools), in_load_lane) == (1, True)


def test_the_broken_pool_is_shut_down_without_waiting(pools):
    broken = FakePool(["break"])
    _use(broken)
    asyncio.run(loader.load_song(URL))
    assert broken.shutdown_calls == [False]


def test_a_rebuild_prints_one_stderr_line(pools, capsys):
    _use(FakePool(["break"]))
    asyncio.run(loader.load_song(URL))
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_a_double_break_leaves_a_fresh_pool(pools, monkeypatch):
    _use(FakePool(["break"]))
    made = []

    def build(*a, **k):
        pool = FakePool(["break"] if not made else [])
        made.append(pool)
        return pool

    monkeypatch.setattr(loader, "ProcessPoolExecutor", build)
    with pytest.raises(loader.SongError) as e:
        asyncio.run(loader.load_song(URL))
    assert str(e.value) == config.LOADER_CRASHED
    assert len(made) == 2
    assert loader._pools[loader.LOAD_LANE] is made[1]
    assert made[0].shutdown_calls == [False]


def test_concurrent_calls_on_one_broken_pool_rebuild_once(pools):
    _use(FakePool(["break"] * 4))

    async def many():
        return await asyncio.gather(*[loader.load_song(URL) for _ in range(4)])

    assert [s.title for s in asyncio.run(many())] == ["ok"] * 4
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


def test_a_break_on_the_retry_after_shutdown_raises_not_running(
    pools, monkeypatch
):
    class ShutdownDuringRetry(FakePool):
        def submit(self, fn, *args):
            loader.shutdown()
            return super().submit(fn, *args)

    monkeypatch.setattr(
        loader,
        "ProcessPoolExecutor",
        lambda *a, **k: ShutdownDuringRetry(["break"]),
    )
    _use(FakePool(["break"]))
    with pytest.raises(loader.LoaderNotRunning):
        asyncio.run(loader.load_song(URL))
    assert loader._pools == {}


def test_a_break_in_the_preload_lane_rebuilds_only_that_lane(pools):
    load_pool = FakePool()
    _use(load_pool, loader.LOAD_LANE)
    _use(FakePool(["break"]), loader.PRELOAD_LANE)
    song = asyncio.run(loader._load(loader.PRELOAD_LANE, URL))
    assert (
        song.title,
        len(pools),
        loader._pools[loader.PRELOAD_LANE] is pools[0],
        loader._pools[loader.LOAD_LANE] is load_pool,
        load_pool.shutdown_calls,
    ) == ("ok", 1, True, True, [])


def test_a_break_in_the_load_lane_leaves_the_preload_lane_alone(pools):
    preload_pool = FakePool()
    _use(FakePool(["break"]), loader.LOAD_LANE)
    _use(preload_pool, loader.PRELOAD_LANE)
    asyncio.run(loader.load_song(URL))
    assert (
        len(pools),
        loader._pools[loader.PRELOAD_LANE] is preload_pool,
        preload_pool.shutdown_calls,
    ) == (1, True, [])


def test_a_double_break_in_one_lane_fails_without_touching_the_other(
    pools, monkeypatch
):
    other = FakePool()
    _use(FakePool(["break"]), loader.PRELOAD_LANE)
    _use(other, loader.LOAD_LANE)
    monkeypatch.setattr(
        loader, "ProcessPoolExecutor", lambda *a, **k: FakePool(["break"])
    )
    with pytest.raises(loader.SongError) as e:
        asyncio.run(loader._load(loader.PRELOAD_LANE, URL))
    assert (str(e.value), loader._pools[loader.LOAD_LANE] is other) == (
        config.LOADER_CRASHED,
        True,
    )


def test_concurrent_breaks_in_two_lanes_rebuild_each_once(pools):
    _use(FakePool(["break"] * 2), loader.LOAD_LANE)
    _use(FakePool(["break"] * 2), loader.PRELOAD_LANE)

    async def many():
        return await asyncio.gather(
            loader.load_song(URL),
            loader.load_song(URL),
            loader._load(loader.PRELOAD_LANE, URL),
            loader._load(loader.PRELOAD_LANE, URL),
        )

    asyncio.run(many())
    assert len(pools) == 2


def test_a_break_in_the_preload_lane_after_shutdown_builds_nothing(pools):
    class ShutdownDuringCall(FakePool):
        def submit(self, fn, *args):
            loader.shutdown()
            return super().submit(fn, *args)

    _use(FakePool(), loader.LOAD_LANE)
    _use(ShutdownDuringCall(["break"]), loader.PRELOAD_LANE)
    with pytest.raises(loader.LoaderNotRunning):
        asyncio.run(loader._load(loader.PRELOAD_LANE, URL))
    assert pools == []


class WarmPool:
    """A fake pool that logs when its warm-up is submitted and awaited."""

    def __init__(self, log, name, fail=False):
        from concurrent.futures import Future

        self.log, self.name, self.fail = log, name, fail
        self.future = Future()
        self.shutdown_calls = []

    def submit(self, fn, *args):
        self.log.append(("submit", self.name))
        if self.fail:
            self.future.set_exception(RuntimeError("warm-up failed"))
        else:
            self.future.set_result(None)
        return _Logged(self.log, self.name, self.future)

    def shutdown(self, wait=True, **kwargs):
        self.shutdown_calls.append(wait)


class _Logged:
    def __init__(self, log, name, future):
        self.log, self.name, self.future = log, name, future

    def result(self):
        self.log.append(("result", self.name))
        return self.future.result()


@pytest.fixture
def warm_pools(monkeypatch):
    log, built = [], []
    names = iter([loader.LOAD_LANE, loader.PRELOAD_LANE])
    failing = set()

    def new_pool():
        name = next(names)
        pool = WarmPool(log, name, fail=name in failing)
        built.append(pool)
        return pool

    monkeypatch.setattr(loader, "_pools", {})
    monkeypatch.setattr(loader, "_new_pool", new_pool)
    monkeypatch.setattr(loader, "wrap_stdio", lambda: None)
    monkeypatch.setattr(loader._context, "Process", loader._context.Process)
    return log, built, failing


def test_init_submits_both_warm_ups_before_awaiting_either(warm_pools):
    log, built, _ = warm_pools
    loader.init()
    kinds = [kind for kind, _ in log]
    assert kinds == ["submit", "submit", "result", "result"]


def test_a_failing_warm_up_raises_and_keeps_both_pools(warm_pools):
    _, built, failing = warm_pools
    failing.add(loader.LOAD_LANE)
    with pytest.raises(RuntimeError, match="warm-up failed"):
        loader.init()
    loader.init()
    assert (sorted(loader._pools), len(built)) == (
        [loader.LOAD_LANE, loader.PRELOAD_LANE],
        2,
    )
