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
        self._trigger_task: asyncio.Task | None = None
        self._requested_run = False
        self._lock = asyncio.Lock()
        self._busy = False
        self._last_error: str | None = None
        # Debounce window for post-write sync triggers (coalesces bursts).
        self.REQUEST_DEBOUNCE_SECONDS = 3.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the periodic loop (no-op when the interval is disabled)."""
        if self._task is not None and not self._task.done():
            return
        if self.settings.favorites_sync_interval_seconds <= 0:
            logger.info(
                "favorites sync periodic loop disabled "
                "(FAVORITES_SYNC_INTERVAL_SECONDS=0); post-write triggers and "
                "manual POST /api/favorites/sync/run still work"
            )
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._trigger_task is not None:
            self._trigger_task.cancel()
            try:
                await self._trigger_task
            except asyncio.CancelledError:
                pass
            self._trigger_task = None
        self._requested_run = False
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

    def request_run(self) -> None:
        """Queue ONE background sync after a manual favorites write op.

        Debounced + coalesced: a burst of write ops (e.g. a 200-item batch)
        schedules at most a single extra scan, started a few seconds after the
        first trigger. Any in-flight/periodic run is serialized by ``run``'s
        lock, and the debounce window makes the scan see the just-committed
        favorite. Safe to call even when the periodic loop is disabled (the
        one-shot scan still runs)."""
        if self._requested_run or (self._trigger_task is not None and not self._trigger_task.done()):
            return
        self._requested_run = True
        self._trigger_task = asyncio.create_task(self._debounced_run())

    async def _debounced_run(self) -> None:
        await asyncio.sleep(self.REQUEST_DEBOUNCE_SECONDS)
        self._requested_run = False
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the index is best-effort
            logger.warning("favorites post-write sync failed (%s)", exc)

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
        # Favorites are account-scoped: without IPB cookies there is nothing
        # to scan — fail fast and quietly so non-logged-in deployments don't
        # log hourly sadpanda warnings.
        if not (self.settings.ipb_member_id and self.settings.ipb_pass_hash):
            return {
                **self.status(),
                "ok": False,
                "baseline": False,
                "error": "favorites require IPB cookies (IPB_MEMBER_ID + IPB_PASS_HASH)",
                "skipped": True,
            }

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
        favcat_map: dict[int, str] = result.get("favcat_map") or {}

        known = self.state.known() | {f"{g}:{t}" for g, t in seen}
        archived = self.state.archived()
        errors: dict[str, str] = dict(self.state.errors())
        favcats = dict(self.state.favcats())
        for item in new_items:
            fc = getattr(item, "favcat", None)
            if fc is not None:
                try:
                    favcats[f"{item.gid}:{item.token}"] = int(fc)
                except (TypeError, ValueError):
                    pass
        favcats = {k: v for k, v in favcats.items() if k in known}

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
            favcats=favcats,
            favcat_map=favcat_map,
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
