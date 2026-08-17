"""Periodic favorites sync: incremental scan + optional auto-archive.

Runs every ``FAVORITES_SYNC_INTERVAL_SECONDS`` (0 = disabled). Each run scans
``/favorites.php`` incrementally against the persisted snapshot; newly
discovered scoped items are optionally auto-archived when
``FAVORITES_SYNC_ARCHIVE`` is on — a GP-spending action the operator must
enable deliberately (default off). A manual ``run()`` is always available
(single-flighted with the periodic loop) and does the same work on demand.

Scan/archive dedup is belt-and-braces: an item is only auto-archived when it
is new to the snapshot AND not present in the archive store (any state). Items
that fail (GP insufficient, not a Star member, …) are recorded in the state
``errors`` map and treated as known — they are never retried automatically,
preventing repeated GP spends on a permanently failing gallery.

**Baseline semantics**: the FIRST successful run only establishes the
baseline — it records every scoped favorite as known but never auto-archives,
so enabling auto-archive on a fresh snapshot never mass-spends GP on existing
favorites. Auto-archive applies from the second run onward (genuinely new
items only). Deleting the state file resets the baseline (the next run
records everything again, still without archiving).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..config import Settings
from .state import FavoritesSyncState

if TYPE_CHECKING:
    from ..archive.manager import ArchiveManager
    from ..eh.service import EHService

logger = logging.getLogger(__name__)


class FavoritesSyncer:
    def __init__(
        self,
        settings: Settings,
        *,
        service: EHService,
        archive: ArchiveManager | None = None,
        state: FavoritesSyncState | None = None,
    ):
        self.settings = settings
        self.service = service
        self.archive = archive
        self.state = state or FavoritesSyncState(settings.favorites_sync_state)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._busy = False
        self._last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the periodic loop (no-op when the interval is disabled)."""
        if self._task is not None and not self._task.done():
            return
        if self.settings.favorites_sync_interval_seconds <= 0:
            logger.info(
                "favorites sync disabled (FAVORITES_SYNC_INTERVAL_SECONDS=0); "
                "manual runs still available via POST /api/favorites/sync/run"
            )
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        interval = self.settings.favorites_sync_interval_seconds
        logger.info("favorites sync loop started (every %.0fs)", interval)
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning("favorites sync run failed (%s)", exc)
            await asyncio.sleep(interval)

    # -- public API --------------------------------------------------------

    async def run(self) -> dict:
        """Run one incremental scan + (optionally) auto-archive new items.

        Single-flighted: the lock serializes concurrent callers (a periodic
        tick or a manual trigger while one is running simply waits, then
        runs again — the scan is idempotent against the persisted snapshot).
        """
        async with self._lock:
            self._busy = True
            try:
                return await self._run_once()
            finally:
                self._busy = False

    async def _run_once(self) -> dict:
        is_baseline = not self.state.baseline_established()
        try:
            result = await self.service.scan_favorites(
                self.state.known(),
                favcat_whitelist=self.settings.favorites_sync_categories,
                match_threshold=self.settings.favorites_sync_match_threshold,
                max_pages=self.settings.favorites_sync_max_pages,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("favorites scan failed (%s)", exc)
            await self.state.update(last_ok=False, scanned_pages=0)
            return {
                "ok": False,
                "error": str(exc),
                **self.status(),
                "baseline": is_baseline,
            }

        new_items = result["new"]
        seen = result["seen"]
        pages = result["pages"]

        known = self.state.known() | {f"{g}:{t}" for g, t in seen}
        archived = self.state.archived()
        errors: dict[str, str] = dict(self.state.errors())

        archived_now: list[str] = []
        # The FIRST run establishes the baseline: it records every scoped
        # favorite as known but never auto-archives (a fresh snapshot must not
        # treat all current favorites as "new" — that would mass-spend GP).
        # Auto-archive only applies from the second run onward.
        if not is_baseline and self.settings.favorites_sync_archive and self.archive is not None:
            for item in new_items:
                key = f"{item.gid}:{item.token}"
                # never touch items already archived or already in the store
                if key in archived:
                    continue
                if self.archive.store.get(item.gid, item.token) is not None:
                    continue
                try:
                    await self.archive.start(item.gid, item.token)
                    archived.add(key)
                    archived_now.append(key)
                    errors.pop(key, None)
                except Exception as exc:  # noqa: BLE001 - one bad item ≠ abort
                    errors[key] = str(exc)[:300]
                    logger.warning(
                        "auto-archive failed for %s:%s (%s)",
                        item.gid, item.token, exc,
                    )

        await self.state.update(
            known=known,
            archived=archived,
            errors=errors,
            baseline=True,
            last_ok=True,
            scanned_pages=pages,
        )
        self._last_error = None

        logger.info(
            "favorites sync: %s run, %d new (scanned %d pages), %d auto-archived%s",
            "baseline" if is_baseline else "incremental",
            len(new_items), pages, len(archived_now),
            "" if (not is_baseline and self.settings.favorites_sync_archive)
            else " (auto-archive off)",
        )
        return {
            **self.status(),
            "ok": True,
            "baseline": is_baseline,
            "new": [{"gid": i.gid, "token": i.token, "title": i.title}
                    for i in new_items],
            "auto_archived": archived_now,
            "errors": errors,
            "pages": pages,
        }

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": self.settings.favorites_sync_interval_seconds > 0,
            "interval_seconds": self.settings.favorites_sync_interval_seconds,
            "auto_archive": self.settings.favorites_sync_archive,
            "categories": (
                list(self.settings.favorites_sync_categories) or "all"
            ),
            "running": self._busy,
            "last_error": self._last_error,
            **self.state.summary(),
        }
