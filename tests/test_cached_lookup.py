"""CachedLookup's remote-lookup policy, driven through a fake backend."""

import asyncio

from musicbot.cached_lookup import ByteBoundedStore, CachedLookup
from helpers import FakeClock, imported_roots, run


class FakeBackend:
    """Records every key it is called with and answers from `results`;
    a key mapped to an exception raises it."""

    def __init__(self, results=None, gate=None):
        self.results = results or {}
        self.gate = gate
        self.calls = []

    async def __call__(self, key):
        self.calls.append(key)
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.get(key)
        if isinstance(result, BaseException):
            raise result
        return result


def lookup(backend, enabled=True, **kw):
    return CachedLookup(lambda: enabled, backend, "Fake artist", **kw)


def test_a_found_result_is_returned_and_cached():
    backend = FakeBackend({"Radiohead": "info"})
    cached = lookup(backend)

    async def scenario():
        return [await cached.get("Radiohead"), await cached.get("Radiohead")]

    assert run(scenario()) == ["info", "info"]
    assert backend.calls == ["Radiohead"]


def test_concurrent_gets_for_one_key_run_the_backend_once():
    async def scenario():
        backend = FakeBackend({"Radiohead": "info"}, gate=asyncio.Event())
        cached = lookup(backend)
        tasks = [
            asyncio.create_task(cached.get("Radiohead")) for _ in range(3)
        ]
        await asyncio.sleep(0)
        backend.gate.set()
        return await asyncio.gather(*tasks), backend.calls

    results, calls = run(scenario())
    assert results == ["info", "info", "info"]
    assert calls == ["Radiohead"]


def test_cancelling_one_waiter_spares_the_lookup_and_other_waiters():
    async def scenario():
        backend = FakeBackend({"Radiohead": "info"}, gate=asyncio.Event())
        cached = lookup(backend)
        owner = asyncio.create_task(cached.get("Radiohead"))
        doomed = asyncio.create_task(cached.get("Radiohead"))
        survivor = asyncio.create_task(cached.get("Radiohead"))
        await asyncio.sleep(0)
        doomed.cancel()
        await asyncio.sleep(0)
        backend.gate.set()
        return (
            await owner,
            await survivor,
            doomed.cancelled(),
            await cached.get("Radiohead"),
            backend.calls,
        )

    owner, survivor, doomed_cancelled, cached_after, calls = run(scenario())
    assert (owner, survivor) == ("info", "info")
    assert doomed_cancelled
    assert cached_after == "info"
    assert calls == ["Radiohead"]


def test_a_timeout_resolves_waiters_with_none_and_starts_a_cooldown():
    async def scenario():
        backend = FakeBackend({"Radiohead": "info"}, gate=asyncio.Event())
        cached = lookup(backend, timeout=0.01, clock=FakeClock())
        results = await asyncio.gather(
            cached.get("Radiohead"), cached.get("Radiohead")
        )
        backend.gate.set()
        during_cooldown = await cached.get("Radiohead")
        return results, during_cooldown, backend.calls

    results, during_cooldown, calls = run(scenario())
    assert results == [None, None]
    assert during_cooldown is None
    assert calls == ["Radiohead"]


def test_an_exception_resolves_waiters_with_none_and_starts_a_cooldown():
    async def scenario():
        backend = FakeBackend(
            {"Radiohead": RuntimeError("boom")}, gate=asyncio.Event()
        )
        cached = lookup(backend, clock=FakeClock())
        tasks = [
            asyncio.create_task(cached.get("Radiohead")) for _ in range(2)
        ]
        await asyncio.sleep(0)
        backend.gate.set()
        results = await asyncio.gather(*tasks)
        backend.results["Radiohead"] = "info"
        during_cooldown = await cached.get("Radiohead")
        return results, during_cooldown, backend.calls

    results, during_cooldown, calls = run(scenario())
    assert results == [None, None]
    assert during_cooldown is None
    assert calls == ["Radiohead"]


def test_the_backend_is_called_again_once_the_cooldown_passes():
    clock = FakeClock()
    backend = FakeBackend({"Radiohead": RuntimeError("boom")})
    cached = lookup(backend, cooldown=60, clock=clock)

    async def scenario():
        first = await cached.get("Radiohead")
        backend.results["Radiohead"] = "info"
        clock.now += 59
        still_cooling = await cached.get("Radiohead")
        clock.now += 1
        after = await cached.get("Radiohead")
        return first, still_cooling, after

    assert run(scenario()) == (None, None, "info")
    assert backend.calls == ["Radiohead", "Radiohead"]


def test_a_successful_no_match_is_cached_and_not_refetched():
    backend = FakeBackend({})
    cached = lookup(backend)

    async def scenario():
        first = await cached.get("Nobody")
        backend.results["Nobody"] = "info"
        return first, await cached.get("Nobody")

    assert run(scenario()) == (None, None)
    assert backend.calls == ["Nobody"]


def test_clear_drops_the_cache_and_the_cooldown():
    clock = FakeClock()
    backend = FakeBackend({"Radiohead": "info", "Bjork": RuntimeError("x")})
    cached = lookup(backend, clock=clock)

    async def scenario():
        await cached.get("Radiohead")
        await cached.get("Bjork")
        backend.results["Bjork"] = "info"
        cached.clear()
        return await cached.get("Radiohead"), await cached.get("Bjork")

    assert run(scenario()) == ("info", "info")
    assert backend.calls == ["Radiohead", "Bjork", "Radiohead", "Bjork"]


def test_clear_leaves_an_in_flight_lookup_intact():
    async def scenario():
        backend = FakeBackend({"Radiohead": "info"}, gate=asyncio.Event())
        cached = lookup(backend)
        owner = asyncio.create_task(cached.get("Radiohead"))
        await asyncio.sleep(0)
        cached.clear()
        waiter = asyncio.create_task(cached.get("Radiohead"))
        await asyncio.sleep(0)
        backend.gate.set()
        return await owner, await waiter, backend.calls

    assert run(scenario()) == ("info", "info", ["Radiohead"])


def test_a_cancelled_owner_gives_waiters_none_and_caches_nothing():
    async def scenario():
        backend = FakeBackend({"Radiohead": "info"}, gate=asyncio.Event())
        cached = lookup(backend, clock=FakeClock())
        owner = asyncio.create_task(cached.get("Radiohead"))
        waiter = asyncio.create_task(cached.get("Radiohead"))
        await asyncio.sleep(0)
        owner.cancel()
        waited = await waiter
        backend.gate.set()
        # neither cached nor cooling down: the next get asks again
        again = await cached.get("Radiohead")
        return owner.cancelled(), waited, again, backend.calls

    owner_cancelled, waited, again, calls = run(scenario())
    assert owner_cancelled
    assert waited is None
    assert again == "info"
    assert calls == ["Radiohead", "Radiohead"]


def test_a_disabled_lookup_returns_none_without_calling_the_backend():
    backend = FakeBackend({"Radiohead": "info"})
    cached = lookup(backend, enabled=False)

    assert run(cached.get("Radiohead")) is None
    assert backend.calls == []


def test_the_enabled_predicate_is_read_on_every_get():
    state = {"on": False}
    backend = FakeBackend({"Radiohead": "info"})
    cached = CachedLookup(lambda: state["on"], backend, "Fake artist")

    async def scenario():
        off = await cached.get("Radiohead")
        state["on"] = True
        return off, await cached.get("Radiohead")

    assert run(scenario()) == (None, "info")


def test_log_lines_carry_the_label_and_key(capsys):
    async def scenario():
        slow = FakeBackend({"a": "info"}, gate=asyncio.Event())
        await lookup(slow, timeout=0.01).get("a")
        await lookup(FakeBackend({"b": RuntimeError("boom")})).get("b")
        await lookup(FakeBackend({})).get("c")

    run(scenario())
    assert capsys.readouterr().err.splitlines() == [
        "Fake artist timed out for a",
        "Fake artist failed for b: boom",
        "Fake artist found no match for c",
    ]


def test_a_passed_in_store_holds_the_results_and_clear_empties_it():
    store = {}
    backend = FakeBackend({"Radiohead": "info"})
    cached = lookup(backend, store=store)

    async def scenario():
        await cached.get("Radiohead")
        held = dict(store)
        await cached.get("Radiohead")
        cached.clear()
        return held, dict(store)

    held, after = run(scenario())
    assert held == {"Radiohead": "info"}
    assert after == {}
    assert backend.calls == ["Radiohead"]


def sized(budget=10, max_entries=100):
    return ByteBoundedStore(budget, max_entries, len)


def keys(store):
    return [k for k in "abcdefgh" if k in store]


def test_byte_budget_evicts_least_recent_weighted_entries_only():
    store = sized(budget=10)
    store["a"] = "xxxx"
    store["w"] = ""
    store["b"] = "xxxx"
    store["c"] = "xxxx"
    assert "a" not in store
    assert "w" in store and "b" in store and "c" in store


def test_byte_budget_never_evicts_the_entry_just_stored():
    store = sized(budget=10)
    store["a"] = "xx"
    store["b"] = "x" * 50
    assert keys(store) == ["b"]


def test_entry_cap_evicts_plain_least_recently_used():
    store = sized(budget=1000, max_entries=3)
    store["a"] = ""
    store["b"] = "xx"
    store["c"] = ""
    store["d"] = ""
    assert keys(store) == ["b", "c", "d"]


def test_a_read_refreshes_recency():
    store = sized(budget=1000, max_entries=3)
    store["a"] = ""
    store["b"] = ""
    store["c"] = ""
    assert store["a"] == ""
    store["d"] = ""
    assert keys(store) == ["a", "c", "d"]


def test_replacing_an_entry_keeps_the_total_exact():
    store = sized(budget=10)
    store["a"] = "xxxx"
    store["a"] = "xxxx"
    store["a"] = "xxxx"
    store["b"] = "xxxx"
    assert keys(store) == ["a", "b"]
    store["c"] = "xxx"
    assert keys(store) == ["b", "c"]


def test_store_clear_resets_the_total():
    store = sized(budget=10)
    store["a"] = "x" * 9
    store.clear()
    assert keys(store) == []
    store["b"] = "x" * 5
    store["c"] = "x" * 5
    assert keys(store) == ["b", "c"]


def test_the_module_imports_only_the_standard_library_it_names():
    from musicbot import cached_lookup

    roots = imported_roots(cached_lookup)
    # __future__ is allowed too, should the module ever need it
    assert roots - {"__future__"} == {"asyncio", "sys", "time", "typing"}
