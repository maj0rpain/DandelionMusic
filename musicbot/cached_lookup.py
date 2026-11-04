"""A cached, deduplicated, cooldown-guarded remote lookup.

`CachedLookup` owns the whole policy for asking a remote backend about
a key: skip it when the backend isn't configured, answer from the
cache, refuse during a failure cooldown, join a lookup already in
flight, and otherwise run the backend under a timeout. The backend
itself - an HTTP call, an SDK call in an executor, a fake in tests -
is passed in at construction.

Imports only the standard library (`tests/test_cached_lookup.py`
asserts it).
"""

import asyncio
import sys
import time
from typing import (
    Awaitable,
    Callable,
    Dict,
    Generic,
    Hashable,
    Optional,
    TypeVar,
)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class CachedLookup(Generic[K, V]):
    """One backend's lookups, with its own cache, in-flight futures and
    cooldown deadlines.

    - `enabled`: read on every `get()`; when false, `get()` returns
      `None` without touching the backend.
    - `backend`: `async (key) -> V | None`. A `None` is a real "no
      match" and is cached like any other result.
    - `label`: prefixes every log line, so include the caller's own
      module prefix in it.
    - `timeout`: seconds a backend call may take.
    - `cooldown`: seconds a key is refused after a transient failure.
      A transient failure is deliberately not cached as a result, but
      retrying it on every lookup would make a persistently broken
      backend (bad credentials, an outage, an unreachable host) pay
      its full timeout again for each new key, so the key's retries
      are suppressed for this long.
    - `clock`: monotonic seconds, injectable for tests.
    """

    def __init__(
        self,
        enabled: Callable[[], bool],
        backend: Callable[[K], Awaitable[Optional[V]]],
        label: str,
        timeout: float = 3,
        cooldown: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._backend = backend
        self._label = label
        self._timeout = timeout
        self._cooldown = cooldown
        self._clock = clock
        self._cache: Dict[K, Optional[V]] = {}
        self._futures: Dict[K, "asyncio.Future[Optional[V]]"] = {}
        self._cooldown_until: Dict[K, float] = {}

    def clear(self) -> None:
        """Forget cached results and cooldowns. In-flight lookups are
        left alone: their owners pop their own futures, and waiters on
        them must still be resolved."""
        self._cache.clear()
        self._cooldown_until.clear()

    def _in_cooldown(self, key: K) -> bool:
        deadline = self._cooldown_until.get(key)
        if deadline is None:
            return False
        if self._clock() < deadline:
            return True
        del self._cooldown_until[key]
        return False

    def _start_cooldown(self, key: K) -> None:
        self._cooldown_until[key] = self._clock() + self._cooldown

    def _log(self, message: str) -> None:
        print(f"{self._label} {message}", file=sys.stderr)

    async def get(self, key: K) -> Optional[V]:
        if not self._enabled():
            return None
        if key in self._cache:
            return self._cache[key]
        if self._in_cooldown(key):
            return None
        future = self._futures.get(key)
        if future is not None:
            # shielded: this waiter being cancelled (view teardown, bot
            # shutdown) must not cancel the shared future out from
            # under the call that owns it, nor the other waiters on it
            return await asyncio.shield(future)
        self._futures[key] = asyncio.get_running_loop().create_future()

        result: Optional[V] = None
        try:
            # a timeout/exception is transient and must not be cached
            # as a permanent "no match" - only a fetched-successfully
            # outcome (match or genuine no-match) is worth remembering
            # for the rest of the process's lifetime
            try:
                result = await asyncio.wait_for(
                    self._backend(key), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                self._log(f"timed out for {key}")
                self._start_cooldown(key)
            except Exception as e:
                self._log(f"failed for {key}: {e}")
                self._start_cooldown(key)
            else:
                if result is None:
                    self._log(f"found no match for {key}")
                self._cache[key] = result
        finally:
            # an owner cancelled mid-lookup skips both branches above:
            # nothing cached, no cooldown, and its waiters get None. Only
            # this owner pops and resolves the future (waiters reach it
            # only through asyncio.shield, and clear() leaves in-flight
            # futures alone), so it cannot already be done
            self._futures.pop(key).set_result(result)

        return result
