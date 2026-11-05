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
from musicbot.song import LoadResult, Song
from helpers import run

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
            else LoadResult([Song(SiteTypes.YT_DLP, URL, title="ok")], True)
        )
        if isinstance(outcome, str) and outcome == "break":
            future.set_exception(BrokenProcessPool("worker died"))
        else:
            future.set_result(outcome)
        return future

    def shutdown(self, wait=True, **kwargs):
        self.shutdown_calls.append(wait)


class ShutdownOnSubmit(FakePool):
    """A pool that shuts the loader down when work is submitted."""

    def submit(self, fn, *args):
        loader.shutdown()
        return super().submit(fn, *args)


class PoolFactory:
    """The patched process-pool constructor.

    `built` holds every pool handed out, in order. A test sets `upcoming`
    to the pools the next builds return; once it is empty, each build
    returns a fresh healthy FakePool.
    """

    def __init__(self):
        self.built = []
        self.upcoming = []

    def __call__(self, *args, **kwargs):
        pool = self.upcoming.pop(0) if self.upcoming else FakePool()
        self.built.append(pool)
        return pool


@pytest.fixture
def pools(monkeypatch):
    factory = PoolFactory()
    monkeypatch.setattr(loader, "ProcessPoolExecutor", factory)
    monkeypatch.setattr(loader, "_pools", {})
    return factory


def _use(pool, lane=loader.Lane.LOAD):
    loader._pools[lane] = pool


def test_a_break_then_a_good_retry_returns_the_retried_result(pools):
    broken = FakePool(["break"])
    _use(broken)
    assert run(loader.load_song(URL)).songs[0].title == "ok"
    in_load_lane = loader._pools[loader.Lane.LOAD] is pools.built[0]
    assert (len(pools.built), in_load_lane) == (1, True)


def test_the_broken_pool_is_shut_down_without_waiting(pools):
    broken = FakePool(["break"])
    _use(broken)
    run(loader.load_song(URL))
    assert broken.shutdown_calls == [False]


def test_a_rebuild_prints_one_stderr_line(pools, capsys):
    _use(FakePool(["break"]))
    run(loader.load_song(URL))
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


@pytest.mark.parametrize("lane", list(loader.Lane))
def test_a_rebuild_names_the_lane_by_its_plain_value(pools, capsys, lane):
    _use(FakePool(["break"]), lane)
    run(loader._load(lane, URL))
    err = capsys.readouterr().err
    assert (f"the {lane.value} lane" in err, "Lane." in err) == (True, False)


def test_a_double_break_leaves_a_fresh_pool(pools):
    _use(FakePool(["break"]))
    pools.upcoming = [FakePool(["break"])]
    with pytest.raises(loader.SongError) as e:
        run(loader.load_song(URL))
    assert str(e.value) == config.LOADER_CRASHED
    assert len(pools.built) == 2
    assert loader._pools[loader.Lane.LOAD] is pools.built[1]
    assert pools.built[0].shutdown_calls == [False]


def test_concurrent_calls_on_one_broken_pool_rebuild_once(pools):
    _use(FakePool(["break"] * 4))

    async def many():
        return await asyncio.gather(*[loader.load_song(URL) for _ in range(4)])

    assert [r.songs[0].title for r in run(many())] == ["ok"] * 4
    assert len(pools.built) == 1


def test_a_break_after_shutdown_raises_not_running_and_builds_nothing(
    pools,
):
    _use(ShutdownOnSubmit(["break"]))
    with pytest.raises(loader.LoaderNotRunning):
        run(loader.load_song(URL))
    assert pools.built == []


def test_a_break_on_the_retry_after_shutdown_raises_not_running(pools):
    pools.upcoming = [ShutdownOnSubmit(["break"])]
    _use(FakePool(["break"]))
    with pytest.raises(loader.LoaderNotRunning):
        run(loader.load_song(URL))
    assert loader._pools == {}
    assert len(pools.built) == 1


def test_a_break_in_the_preload_lane_rebuilds_only_that_lane(pools):
    load_pool = FakePool()
    _use(load_pool, loader.Lane.LOAD)
    _use(FakePool(["break"]), loader.Lane.PRELOAD)
    song = run(loader._load(loader.Lane.PRELOAD, URL)).songs[0]
    assert (
        song.title,
        len(pools.built),
        loader._pools[loader.Lane.PRELOAD] is pools.built[0],
        loader._pools[loader.Lane.LOAD] is load_pool,
        load_pool.shutdown_calls,
    ) == ("ok", 1, True, True, [])


def test_a_break_in_the_load_lane_leaves_the_preload_lane_alone(pools):
    preload_pool = FakePool()
    _use(FakePool(["break"]), loader.Lane.LOAD)
    _use(preload_pool, loader.Lane.PRELOAD)
    run(loader.load_song(URL))
    assert (
        len(pools.built),
        loader._pools[loader.Lane.PRELOAD] is preload_pool,
        preload_pool.shutdown_calls,
    ) == (1, True, [])


def test_a_double_break_in_one_lane_fails_without_touching_the_other(pools):
    other = FakePool()
    _use(FakePool(["break"]), loader.Lane.PRELOAD)
    _use(other, loader.Lane.LOAD)
    pools.upcoming = [FakePool(["break"])]
    with pytest.raises(loader.SongError) as e:
        run(loader._load(loader.Lane.PRELOAD, URL))
    assert (str(e.value), loader._pools[loader.Lane.LOAD] is other) == (
        config.LOADER_CRASHED,
        True,
    )


def test_concurrent_breaks_in_two_lanes_rebuild_each_once(pools):
    _use(FakePool(["break"] * 2), loader.Lane.LOAD)
    _use(FakePool(["break"] * 2), loader.Lane.PRELOAD)

    async def many():
        return await asyncio.gather(
            loader.load_song(URL),
            loader.load_song(URL),
            loader._load(loader.Lane.PRELOAD, URL),
            loader._load(loader.Lane.PRELOAD, URL),
        )

    run(many())
    assert len(pools.built) == 2


def test_a_break_in_the_preload_lane_after_shutdown_builds_nothing(pools):
    _use(FakePool(), loader.Lane.LOAD)
    _use(ShutdownOnSubmit(["break"]), loader.Lane.PRELOAD)
    with pytest.raises(loader.LoaderNotRunning):
        run(loader._load(loader.Lane.PRELOAD, URL))
    assert pools.built == []


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
    names = iter(loader.Lane)
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
    failing.add(loader.Lane.LOAD)
    with pytest.raises(RuntimeError, match="warm-up failed"):
        loader.init()
    loader.init()
    assert (sorted(loader._pools), len(built)) == (
        [loader.Lane.LOAD, loader.Lane.PRELOAD],
        2,
    )
