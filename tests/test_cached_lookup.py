"""CachedLookup's remote-lookup policy, driven through a fake backend.

Synchronous test functions running their coroutine through
asyncio.run(): the dev group pins pytest with no asyncio_mode, so an
async test would be collected, skipped with a warning, and silently
pass.
"""

import asyncio

from musicbot.cached_lookup import CachedLookup


def run(coro):
    # bounded, so a waiter left unresolved fails the test, not the run
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


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


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


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
        "library_metadata: Fake artist timed out for a",
        "library_metadata: Fake artist failed for b: boom",
        "library_metadata: Fake artist found no match for c",
    ]


def test_the_module_imports_only_the_standard_library_it_names():
    import ast
    from pathlib import Path

    from musicbot import cached_lookup

    source = Path(cached_lookup.__file__).read_text(encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split(".")[0])
    # __future__ is allowed too, should the module ever need it
    assert roots - {"__future__"} == {"asyncio", "sys", "time", "typing"}
