"""Unit tests for the throttle (semaphore, interval, circuit breaker)."""

import asyncio
import time

import pytest

from app.config import Settings
from app.throttle.limiter import (
    KIND_HTML,
    CircuitBreaker,
    CircuitOpenError,
    Throttle,
)


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc", html_interval_seconds=0)
    base.update(kw)
    return Settings(**base)


@pytest.mark.asyncio
async def test_circuit_breaker_trip_and_cooldown():
    cb = CircuitBreaker(cooldown_seconds=0.1)
    await cb.check()  # closed -> no-op
    await cb.trip("banned")
    assert cb.is_open
    with pytest.raises(CircuitOpenError):
        await cb.check()
    await asyncio.sleep(0.15)
    await cb.check()  # cooldown expired -> closed
    assert not cb.is_open


@pytest.mark.asyncio
async def test_circuit_breaker_trip_with_cooldown_override():
    cb = CircuitBreaker(cooldown_seconds=600)
    # per-trip override wins over the constructor default
    await cb.trip("exceedLimit", cooldown=300)
    with pytest.raises(CircuitOpenError) as exc_info:
        await cb.check()
    assert exc_info.value.retry_after <= 300
    # default (600) applies when no override is given
    await cb.reset()
    await cb.trip("banned")
    with pytest.raises(CircuitOpenError) as exc_info:
        await cb.check()
    assert exc_info.value.retry_after > 300
    await cb.reset()
    assert not cb.is_open


@pytest.mark.asyncio
async def test_circuit_breaker_expiry_uses_trip_cooldown():
    cb = CircuitBreaker(cooldown_seconds=600)
    await cb.trip("exceedLimit", cooldown=0.1)
    with pytest.raises(CircuitOpenError):
        await cb.check()
    await asyncio.sleep(0.15)
    await cb.check()  # expires per the trip cooldown, not the default
    assert not cb.is_open


@pytest.mark.asyncio
async def test_circuit_breaker_reset():
    cb = CircuitBreaker(cooldown_seconds=60)
    await cb.trip("exceedLimit")
    await cb.reset()
    await cb.check()  # no raise


@pytest.mark.asyncio
async def test_throttle_concurrency_limit():
    t = Throttle(_settings(max_concurrency=1, html_interval_seconds=0))

    async def use_slot():
        async with t.acquired(KIND_HTML):
            pass

    async with t.acquired(KIND_HTML):
        # second acquire must block; verify by checking it hasn't completed yet
        second = asyncio.create_task(use_slot())
        await asyncio.sleep(0.05)
        assert not second.done()
    await asyncio.wait_for(second, timeout=1.0)


@pytest.mark.asyncio
async def test_html_interval_enforced():
    t = Throttle(_settings(max_concurrency=4, html_interval_seconds=0.2))
    times: list[float] = []

    async def worker():
        async with t.acquired(KIND_HTML):
            times.append(time.monotonic())
            await asyncio.sleep(0.01)

    await asyncio.gather(*(worker() for _ in range(3)))
    # consecutive HTML requests must be spaced by the interval
    for a, b in zip(times, times[1:]):
        assert b - a >= 0.18, (a, b)


@pytest.mark.asyncio
async def test_throttle_circuit_open_rejects():
    t = Throttle(_settings())
    await t.trip("banned")
    with pytest.raises(CircuitOpenError):
        async with t.acquired(KIND_HTML):
            pass  # pragma: no cover
