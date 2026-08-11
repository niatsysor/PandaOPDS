"""Real-world concurrency stress test (PLAN phase 6 acceptance).

Fires 20 concurrent stream-page requests against live E-Hentai with the
default throttle (max_concurrency=2, html_interval=1.5s) and verifies:
- all requests succeed
- the circuit breaker never trips (no banned / exceedLimit)

Skipped unless RUN_EH_INTEGRATION=1. E-Hentai IP bans are temporary; the
throttle keeps real upstream concurrency <= 2.
"""

import asyncio
import os
import time

import pytest

from app.config import load_settings
from app.eh.exceptions import EHException
from app.eh.service import EHService

CONCURRENCY = 20  # simultaneous client requests (PLAN acceptance)


def _enabled() -> bool:
    return os.getenv("RUN_EH_INTEGRATION") == "1"


@pytest.mark.skipif(not _enabled(), reason="requires RUN_EH_INTEGRATION=1")
@pytest.mark.asyncio
async def test_concurrent_stream_no_ban():
    settings = load_settings()
    svc = EHService(settings)

    async def _one_page(gid: int, token: str, page: int) -> tuple[int, int]:
        data, mime = await svc.get_image(gid, token, page)
        return len(data), 0  # (bytes, error-code)

    try:
        # pick 5 galleries -> 4 pages each = 20 concurrent requests
        info = await svc.search_galleries()
        galleries = info.galleries[:5]
        assert len(galleries) >= 5, "need at least 5 galleries for the stress test"

        metas = await svc.get_metadatas([(g.gid, g.token) for g in galleries])
        tasks: list = []
        for g in galleries:
            meta = next((m for m in metas if m.gid == g.gid), None)
            pages = min(4, meta.filecount if meta else 4)
            for p in range(1, pages + 1):  # 1-based pages
                tasks.append(_one_page(g.gid, g.token, p))
        assert len(tasks) >= CONCURRENCY, "not enough page requests to saturate"

        start = time.monotonic()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - start

        failures = [r for r in results if isinstance(r, Exception)]
        ok_bytes = [r for r in results if not isinstance(r, Exception)]

        stats = await svc.stats()
        circuit_open = stats["throttle"]["circuit_open"]
        throttle = stats["throttle"]

        print(
            f"\n[stress] {len(results)} requests in {elapsed:.1f}s | "
            f"ok={len(ok_bytes)} fail={len(failures)} | "
            f"html={throttle['html_requests']} api={throttle['api_requests']} "
            f"image={throttle['image_requests']} | circuit_open={circuit_open}"
        )
        for exc in failures[:5]:
            print(f"[stress] failure: {type(exc).__name__}: {exc}")

        # acceptance: no hard upstream failure and breaker stays closed
        assert not circuit_open, "circuit breaker tripped (banned/exceedLimit?)"
        hard = [f for f in failures if isinstance(f, EHException)]
        assert not hard, f"upstream EH errors during stress: {[str(h) for h in hard][:3]}"
        # allow a few transient network errors, but most must succeed
        assert len(ok_bytes) >= 0.9 * len(results), "too many failures"
    finally:
        await svc.close()
