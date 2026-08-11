"""Unit tests for memory + disk caches."""

import asyncio

import pytest

from app.cache.disk import DiskImageCache, detect_image_type
from app.cache.memory import MemoryCache


@pytest.mark.asyncio
async def test_memory_cache_ttl_and_lru():
    c = MemoryCache(default_ttl=1.0, max_entries=2)
    await c.set("a", 1)
    await c.set("b", 2)
    await c.set("c", 3)  # evicts "a" (LRU)
    assert await c.get("a") is None
    assert await c.get("b") == 2
    assert await c.get("c") == 3

    # TTL expiry
    await c.set("exp", 42, ttl=0.05)
    assert await c.get("exp") == 42
    await asyncio.sleep(0.1)
    assert await c.get("exp") is None


@pytest.mark.asyncio
async def test_memory_cache_get_or_set(tmp_path):
    c = MemoryCache()

    async def factory():
        return "computed"

    assert await c.get_or_set("k", factory) == "computed"
    assert await c.get_or_set("k", factory) == "computed"  # cached now
    assert c.size == 1


@pytest.mark.asyncio
async def test_memory_cache_single_flight():
    """Concurrent get_or_set for the same key runs the factory only once."""
    c = MemoryCache()
    calls = {"n": 0}

    async def slow_factory():
        calls["n"] += 1
        await asyncio.sleep(0.1)
        return "value"

    results = await asyncio.gather(*(c.get_or_set("k", slow_factory) for _ in range(8)))
    assert all(r == "value" for r in results)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_memory_cache_hit_miss_stats():
    c = MemoryCache()
    await c.set("a", 1)
    assert await c.get("a") == 1
    assert await c.get("a") == 1
    assert await c.get("miss") is None
    stats = c.stats
    assert stats["hits"] == 2
    assert stats["misses"] == 1


def test_detect_image_type():
    assert detect_image_type(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert detect_image_type(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert detect_image_type(b"GIF89a...") == "image/gif"
    assert detect_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert detect_image_type(b"BM...") == "image/bmp"
    assert detect_image_type(b"hello") == "application/octet-stream"


@pytest.mark.asyncio
async def test_disk_cache_put_get_evict(tmp_path):
    cache = DiskImageCache(tmp_path, max_gb=0.000001, ttl_seconds=3600)  # ~1KB cap
    data = b"x" * 600
    await cache.put(1, "tok", 1, data)
    assert await cache.get(1, "tok", 1) == data
    assert cache.stats["hits"] == 1

    # exceeding cap evicts oldest
    await cache.put(1, "tok", 2, b"y" * 600)
    assert await cache.get(1, "tok", 1) is None
    assert await cache.get(1, "tok", 2) == b"y" * 600


@pytest.mark.asyncio
async def test_disk_cache_ttl_expiry(tmp_path):
    cache = DiskImageCache(tmp_path, max_gb=1.0, ttl_seconds=0.05)
    await cache.put(1, "tok", 1, b"data")
    assert await cache.get(1, "tok", 1) == b"data"
    await asyncio.sleep(0.1)
    assert await cache.get(1, "tok", 1) is None


@pytest.mark.asyncio
async def test_disk_cache_disabled(tmp_path):
    cache = DiskImageCache(tmp_path, enabled=False)
    await cache.put(1, "tok", 1, b"data")  # no-op
    assert await cache.get(1, "tok", 1) is None
