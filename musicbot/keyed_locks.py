import asyncio
import weakref
from typing import Hashable


class KeyedLocks:
    """One `asyncio.Lock` per key, with weak values: an entry goes once
    nothing holds or awaits its lock."""

    def __init__(self) -> None:
        self._locks: "weakref.WeakValueDictionary[Hashable, asyncio.Lock]"
        self._locks = weakref.WeakValueDictionary()

    def __call__(self, key: Hashable) -> asyncio.Lock:
        "The key's lock, created on first use"
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock
