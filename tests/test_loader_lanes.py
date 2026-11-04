"""The two extraction lanes are independent, on the real workers.

Each scenario runs in a fresh interpreter after a real loader.init(),
holding a lane with a picklable blocker that waits on a manager Event.
"""

import pytest

from test_import_inert import _observed, _run_in_fresh_interpreter

PRELUDE = """
import asyncio
import json
import multiprocessing
import os
from concurrent.futures.process import BrokenProcessPool

from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.song import Song

LOAD, PRELOAD = loader.LOAD_LANE, loader.PRELOAD_LANE


def block(started, release):
    started.set()
    release.wait(90)
    return "released"


def ping():
    return os.getpid()


async def hold(lane, events):
    started, release = events
    task = asyncio.ensure_future(
        loader._run_sync(lane, block, started, release)
    )
    await asyncio.get_running_loop().run_in_executor(None, started.wait)
    return task


async def kill(lane):
    try:
        loader._pools[lane].submit(os._exit, 1).result()
    except BrokenProcessPool:
        return True
    return False


def other(lane):
    return PRELOAD if lane == LOAD else LOAD
"""

INDEPENDENCE_PROBE = (
    PRELUDE
    + """
async def main(events):
    blocker = await hold(LOAD, events)
    song = Song(SiteTypes.YT_DLP, "https://example.invalid/a")
    result = await asyncio.wait_for(loader.preload(song), 60)
    probe_pid = await asyncio.wait_for(loader._run_sync(PRELOAD, ping), 60)
    still_blocked = not blocker.done()
    events[1].set()
    return {
        "preload": result.value,
        "probe_ran": isinstance(probe_pid, int),
        "still_blocked": still_blocked,
        "released": await asyncio.wait_for(blocker, 60),
    }


if __name__ == "__main__":
    loader.init()
    try:
        with multiprocessing.Manager() as manager:
            events = (manager.Event(), manager.Event())
            observed = asyncio.run(main(events))
    finally:
        loader.shutdown()
    print(json.dumps(observed))
"""
)

CRASH_PROBE = (
    PRELUDE
    + """
async def main(killed, events):
    held = other(killed)
    blocker = await hold(held, events)
    broke = await kill(killed)
    killed_next = await asyncio.wait_for(loader._run_sync(killed, ping), 60)
    other_blocked = not blocker.done()
    events[1].set()
    return {
        "broke": broke,
        "killed_next_ok": isinstance(killed_next, int),
        "in_flight_untouched": other_blocked,
        "in_flight_result": await asyncio.wait_for(blocker, 60),
        "other_next_ok": isinstance(
            await asyncio.wait_for(loader._run_sync(held, ping), 60), int
        ),
    }


if __name__ == "__main__":
    loader.init()
    try:
        with multiprocessing.Manager() as manager:
            events = (manager.Event(), manager.Event())
            observed = asyncio.run(main(os.environ_lane, events))
    finally:
        loader.shutdown()
    print(json.dumps(observed))
"""
)


@pytest.fixture(scope="module")
def independence(tmp_path_factory):
    return _observed(
        _run_in_fresh_interpreter(
            tmp_path_factory.mktemp("lanes"),
            INDEPENDENCE_PROBE,
            as_file=True,
        )
    )


@pytest.fixture(scope="module", params=["load", "preload"])
def crash(request, tmp_path_factory):
    script = CRASH_PROBE.replace("os.environ_lane", repr(request.param))
    return _observed(
        _run_in_fresh_interpreter(
            tmp_path_factory.mktemp("crash"), script, as_file=True
        )
    )


def test_a_preload_completes_while_the_load_lane_is_held(independence):
    assert independence["preload"] in ("extracted", "failed")
    assert independence["probe_ran"] is True


def test_the_held_load_lane_call_finishes_once_it_is_released(independence):
    assert (independence["still_blocked"], independence["released"]) == (
        True,
        "released",
    )


def test_a_killed_lane_breaks_and_its_next_call_succeeds_after_rebuild(crash):
    assert (crash["broke"], crash["killed_next_ok"]) == (True, True)


def test_the_other_lane_keeps_its_in_flight_call_and_next_call(crash):
    assert (
        crash["in_flight_untouched"],
        crash["in_flight_result"],
        crash["other_next_ok"],
    ) == (True, "released", True)
