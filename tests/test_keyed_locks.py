import asyncio
import gc
import weakref

from musicbot import keyed_locks
from musicbot.keyed_locks import KeyedLocks

from helpers import imported_roots, run


def test_the_same_key_gives_the_same_lock_while_it_is_referenced():
    locks = KeyedLocks()
    first = locks("a")
    assert locks("a") is first


def test_different_keys_give_independent_locks():
    locks = KeyedLocks()
    a, b = locks("a"), locks("b")
    assert a is not b


def test_an_entry_is_gone_once_nothing_references_its_lock():
    locks = KeyedLocks()
    ref = weakref.ref(locks("a"))
    gc.collect()
    assert ref() is None


def test_two_tasks_on_one_key_run_one_at_a_time():
    run(_two_tasks_peak())


async def _two_tasks_peak():
    locks = KeyedLocks()
    running = 0
    peak = 0

    async def work():
        nonlocal running, peak
        async with locks("a"):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            running -= 1

    await asyncio.gather(work(), work())
    assert peak == 1


def test_the_module_imports_only_the_standard_library():
    assert imported_roots(keyed_locks) <= {"asyncio", "weakref", "typing"}
