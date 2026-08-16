"""Global outbound traffic control.

- `asyncio.Semaphore` caps concurrent outbound requests, split by traffic
  class so the ban-risk HTML/API pool stays tight while CDN image fetches
  get browser-parity concurrency:
  - HTML + API: `max_concurrency` (default 5, docker-compose 2)
  - full-res /stream bytes: `image_max_concurrency` (default 5, conservative:
    original-image fetches count against the 509 image quota)
  - cover/thumbnail fetches: `thumb_max_concurrency` (default 25, matching a
    browser's 25-thumbnail tile load on the source site)
- A minimum interval is enforced between consecutive HTML page requests
  (default 1.5s in docker-compose, 0.3s code default) — the highest
  ban-risk traffic.
- A circuit breaker trips globally on banned / image-limit-exceeded; while
  open, all new upstream work is rejected (HTTP 503) until the cooldown
  expires or the breaker is reset manually.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from ..config import Settings

logger = logging.getLogger(__name__)

KIND_HTML = "html"
KIND_API = "api"
KIND_IMAGE = "image"  # full-res /stream bytes (509-quota traffic)
KIND_THUMB = "thumb"  # cover / thumbnail CDN fetches


class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open (upstream throttled/banned)."""

    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(f"circuit open: {reason}")
        self.reason = reason
        self.retry_after = retry_after


class CircuitBreaker:
    """Simple open/closed breaker with a cooldown.

    The cooldown can be overridden per trip (e.g. longer for IP bans, shorter
    for image-quota limits); when not overridden the constructor default
    applies.
    """

    def __init__(self, cooldown_seconds: float = 600.0):
        self.cooldown = cooldown_seconds
        self._state = "closed"  # "closed" | "open"
        self._reason: str | None = None
        self._opened_at: float | None = None
        self._cooldown: float = cooldown_seconds
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._state == "open"

    @property
    def state(self) -> dict:
        """Public snapshot for monitoring/WebUI: state, trip reason and
        remaining cooldown (0 when closed)."""
        remaining = 0.0
        if self._state == "open" and self._opened_at is not None:
            remaining = max(0.0, self._cooldown - (time.monotonic() - self._opened_at))
        return {
            "state": self._state,
            "reason": self._reason,
            "remaining": round(remaining, 1),
            "cooldown": self._cooldown,
        }

    async def trip(self, reason: str, cooldown: float | None = None) -> None:
        async with self._lock:
            self._state = "open"
            self._reason = reason
            self._opened_at = time.monotonic()
            self._cooldown = cooldown if cooldown is not None else self.cooldown
            logger.error(
                "CIRCUIT BREAKER TRIPPED: %s (cooldown %.0fs)",
                reason, self._cooldown,
            )

    async def reset(self) -> None:
        async with self._lock:
            self._state = "closed"
            self._reason = None
            self._opened_at = None
            self._cooldown = self.cooldown
            logger.info("circuit breaker reset")

    async def check(self) -> None:
        """Raise CircuitOpenError if the breaker is open and not yet cooled down."""
        async with self._lock:
            if self._state != "open":
                return
            assert self._opened_at is not None
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._cooldown:
                self._state = "closed"
                self._reason = None
                self._opened_at = None
                self._cooldown = self.cooldown
                logger.info("circuit breaker cooldown expired, closed again")
                return
            raise CircuitOpenError(
                self._reason or "unknown",
                retry_after=max(0.0, self._cooldown - elapsed),
            )


class Throttle:
    def __init__(self, settings: Settings):
        self.settings = settings
        # three independent pools: HTML/API (ban-risk), full images (quota),
        # thumbnails (CDN, browser-parity)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        self.image_semaphore = asyncio.Semaphore(settings.image_max_concurrency)
        self.thumb_semaphore = asyncio.Semaphore(settings.thumb_max_concurrency)
        self.circuit = CircuitBreaker()
        self._html_interval = settings.html_interval_seconds
        self._last_html_at = 0.0
        self._lock = asyncio.Lock()
        # stats for monitoring
        self.html_requests = 0
        self.api_requests = 0
        self.image_requests = 0
        self.thumb_requests = 0

    def _semaphore_for(self, kind: str) -> asyncio.Semaphore:
        if kind == KIND_THUMB:
            return self.thumb_semaphore
        if kind == KIND_IMAGE:
            return self.image_semaphore
        return self.semaphore

    async def acquire(self, kind: str = KIND_HTML) -> None:
        await self.circuit.check()
        if kind == KIND_HTML:
            await self._space_html_requests()
        await self._semaphore_for(kind).acquire()
        if kind == KIND_HTML:
            self.html_requests += 1
        elif kind == KIND_API:
            self.api_requests += 1
        elif kind == KIND_THUMB:
            self.thumb_requests += 1
        else:
            self.image_requests += 1

    def release(self, kind: str = KIND_HTML) -> None:
        self._semaphore_for(kind).release()

    async def __aenter__(self) -> "Throttle":
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.release()

    @asynccontextmanager
    async def acquired(self, kind: str = KIND_HTML):
        """Async context manager: acquire the slot, release on exit."""
        await self.acquire(kind)
        try:
            yield self
        finally:
            self.release(kind)

    async def _space_html_requests(self) -> None:
        """Sleep so HTML requests are at least `html_interval_seconds` apart."""
        if self._html_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._last_html_at + self._html_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_html_at = time.monotonic()

    async def trip(self, reason: str, cooldown: float | None = None) -> None:
        await self.circuit.trip(reason, cooldown=cooldown)
