"""Async in-memory TTL cache.

Used for gallery metadata (gdata results) and page-URL mappings (1h TTLs),
so repeated OPDS/stream requests need zero outbound calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class MemoryCache:
    def __init__(self, default_ttl: float = 3600.0, max_entries: int = 4096):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()
        # in-flight single-flight dedup: key -> task computing the value
        self._inflight: dict[str, asyncio.Task] = {}
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            expires, value = item
            if expires < time.monotonic():
                del self._data[key]
                self._misses += 1
                return None
            # refresh LRU order
            self._data.move_to_end(key)
            self._hits += 1
            return value

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        expires = time.monotonic() + ttl
        async with self._lock:
            self._data[key] = (expires, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with ``prefix``; returns the count.

        Used to invalidate a whole list-cache family (e.g. ``list:favorites``
        after a write op mutates the upstream favorites page)."""
        removed = 0
        async with self._lock:
            for key in list(self._data):
                if key.startswith(prefix):
                    del self._data[key]
                    removed += 1
        return removed

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    async def get_or_set(
        self, key: str, factory, ttl: float | None = None
    ) -> Any:
        """Return cached value or compute via `factory()` (an awaitable callable).

        Concurrent callers for the same key share a single in-flight
        computation (single-flight) so cold caches don't duplicate upstream
        requests under load.
        """
        value = await self.get(key)
        if value is not None:
            return value

        task = self._inflight.get(key)
        if task is not None:
            return await task

        loop = asyncio.get_running_loop()
        task = loop.create_task(factory())
        self._inflight[key] = task
        try:
            value = await task
            await self.set(key, value, ttl)
            return value
        finally:
            self._inflight.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def stats(self) -> dict:
        return {"size": self.size, "hits": self._hits, "misses": self._misses}
