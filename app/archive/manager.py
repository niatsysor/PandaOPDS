"""Archive lifecycle manager: quote / trigger / download / zip-convert / validate.

State machine (per gallery, persisted in meta.json):

    absent → pending (POST triggered, upstream preparing)
          → downloading (bytes → archive.part)
          → zipping (7z → zip conversion)
          → ready | failed (failed is retryable)

Concurrency: at most ``archive_download_concurrency`` active archive tasks
(download or zip-conversion) share a semaphore; the rest queue. Archive
traffic is NOT subject to the reading throttle pools, but the global circuit
breaker is always checked (banned / image-limit trips pause archiving too).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import zipfile
from dataclasses import asdict
from pathlib import Path

from ..config import Settings
from ..eh.client import EHClient
from ..eh.exceptions import (
    ArchiverUnavailableError,
    BannedError,
    EHException,
    ExceedLimitError,
    InsufficientGPError,
)
from ..eh.parser import parse_archiver_page
from ..eh.models import ArchiveOption
from ..eh.service import EHService
from ..throttle.limiter import KIND_HTML, Throttle
from .store import ST_DOWNLOADING, ST_FAILED, ST_PENDING, ST_READY, ST_ZIPPING, ArchiveStore

try:  # py7zr is only needed for the rare 7z archives
    import py7zr  # type: ignore

    _HAS_PY7ZR = True
except ImportError:
    py7zr = None  # type: ignore
    _HAS_PY7ZR = False

logger = logging.getLogger(__name__)

_7Z_MAGIC = b"7z\xbc\xaf\x27\x1c"
_ZIP_MAGIC = b"PK\x03\x04"

# Preparing poll: EH generates large archives for minutes; poll every 20s up
# to 2h before giving up (the page itself suggests checking back later).
_PREPARE_POLL_SECONDS = 20.0
_PREPARE_TIMEOUT_SECONDS = 2 * 3600.0


def _detect_archive_format(path: Path) -> str:
    """zip | 7z | unknown by magic bytes."""
    try:
        head = path.read_bytes()[:8]
    except OSError:
        return "unknown"
    if head[:4] == _ZIP_MAGIC:
        return "zip"
    if head[:6] == _7Z_MAGIC:
        return "7z"
    return "unknown"


class ArchiveManager:
    def __init__(
        self,
        settings: Settings,
        *,
        client: EHClient,
        throttle: Throttle,
        store: ArchiveStore,
        service: EHService | None = None,
    ):
        self.settings = settings
        self.client = client
        self.throttle = throttle
        self.store = store
        # Optional service layer: when injected, a gdata metadata + cover
        # snapshot is persisted after each successful archive download (and on
        # manual refresh). None keeps the manager self-contained (tests).
        self.service = service
        self._slots = asyncio.Semaphore(settings.archive_download_concurrency)
        self._tasks: dict[str, asyncio.Task] = {}
        self._progress: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # -- availability ------------------------------------------------------

    @property
    def has_ipb(self) -> bool:
        """The archiver requires a logged-in session (Star membership)."""
        return bool(self.settings.ipb_member_id and self.settings.ipb_pass_hash)

    # -- errors ------------------------------------------------------------

    @staticmethod
    def _map_error(message: str) -> EHException:
        if "Star membership" in message:
            return ArchiverUnavailableError(message)
        if "Insufficient GP" in message:
            return InsufficientGPError(message)
        if "cannot be archived" in message:
            return ArchiverUnavailableError(message)
        return EHException(message)

    async def _trip_if_fatal(self, exc: EHException) -> None:
        if isinstance(exc, BannedError):
            await self.throttle.trip(
                f"{type(exc).__name__}: {exc}",
                cooldown=self.settings.banned_cooldown_seconds,
            )
        elif isinstance(exc, ExceedLimitError):
            await self.throttle.trip(
                f"{type(exc).__name__}: {exc}",
                cooldown=self.settings.exceed_cooldown_seconds,
            )

    # -- upstream access (HTML traffic: throttle + session + breaker) ------

    async def _archiver_get(self, gid: int, token: str) -> str:
        async with self.throttle.acquired(KIND_HTML):
            await self.client.establish_session()
            try:
                return await self.client.get_archiver_page(gid, token)
            except EHException as exc:
                await self._trip_if_fatal(exc)
                raise

    async def _archiver_submit(
        self, gid: int, token: str, dltype: str, dlcheck: str
    ) -> str:
        async with self.throttle.acquired(KIND_HTML):
            await self.client.establish_session()
            try:
                return await self.client.submit_archiver(gid, token, dltype, dlcheck)
            except EHException as exc:
                await self._trip_if_fatal(exc)
                raise

    async def _fetch_url(self, url: str) -> str:
        """GET an arbitrary (hath.network) status/download page."""
        async with self.throttle.acquired(KIND_HTML):
            await self.client.establish_session()
            return await self.client.get_absolute_html(url)

    # -- public API --------------------------------------------------------

    @staticmethod
    def _match_option(
        options: list[ArchiveOption], quality: str | None, default_quality: str
    ) -> ArchiveOption | None:
        """Pick a tier for the requested quality.

        Matches the ``dltype`` value exactly, then the tier label, then a
        known-name map (original->org, resample->res). Falls back to the
        configured default quality and finally to the single available tier.
        """
        quality = (quality or default_quality).strip().lower()
        if not options:
            return None
        for o in options:
            if o.or_value.lower() == quality:
                return o
        for o in options:
            if quality in o.label.lower():
                return o
        alias = {"original": "org", "resample": "res", "resized": "res"}
        if quality in alias:
            for o in options:
                if o.or_value.lower() == alias[quality]:
                    return o
        if len(options) == 1:
            return options[0]
        for o in options:
            if o.available:
                return o
        return options[0]

    async def quote(self, gid: int, token: str) -> dict:
        """Fetch the archiver page and return tiers + prices (no GP spent)."""
        if not self.has_ipb:
            raise ArchiverUnavailableError(
                "archiver requires IPB cookies (IPB_MEMBER_ID + IPB_PASS_HASH)"
            )
        html = await self._archiver_get(gid, token)
        page = parse_archiver_page(html)
        if page.error:
            raise self._map_error(page.error)
        return {
            "gid": gid,
            "token": token,
            "title": page.title,
            "gp_balance": page.gp_balance,
            "options": [
                {
                    "or": o.or_value,
                    "label": o.label,
                    "gp_price": o.gp_price,
                    "size": o.size,
                    "available": o.available,
                    "unlocked": o.unlocked,
                }
                for o in page.options
            ],
            "download_state": page.download_state,
            "download_url": page.download_url,
        }

    async def start(self, gid: int, token: str, quality: str | None = None) -> dict:
        """Trigger an archive download and start the background task.

        POSTs the tier form (``dltype``/``dlcheck``) — free tiers (already
        unlocked) cost no GP, paid tiers are debited here. The response
        carries the hath.network status URL that the task polls until the
        archive is ready.
        """
        if not self.has_ipb:
            raise ArchiverUnavailableError(
                "archiver requires IPB cookies (IPB_MEMBER_ID + IPB_PASS_HASH)"
            )
        html = await self._archiver_get(gid, token)
        page = parse_archiver_page(html)
        if page.error:
            raise self._map_error(page.error)

        option = self._match_option(
            page.options, quality, self.settings.archive_quality
        )
        if option is None:
            raise EHException("archiver offered no tiers for this gallery")
        if not option.available:
            raise EHException(
                f"tier {option.label!r} is not available for this gallery"
            )

        submit_url = f"{self.settings.http_origin}/archiver.php?gid={gid}&token={token}"
        html = await self._archiver_submit(gid, token, option.or_value, option.dlcheck)
        page = parse_archiver_page(html, page_url=submit_url)
        if page.error:
            raise self._map_error(page.error)
        if not page.download_url:
            raise EHException(
                "archive submission returned no status URL "
                f"(state={page.download_state!r})"
            )

        await self.store.upsert(gid, token, {
            "title": page.title or None,
            "quality": option.label,
            "or": option.or_value,
            "gp_price": option.gp_price,
            "download_url": page.download_url,
            "status": ST_PENDING,
            "error": None,
        })
        self._spawn(gid, token)
        return self._status(gid, token)

    async def refresh(self, gid: int, token: str) -> dict:
        """Re-trigger the archive download (existing entry; same tier).

        Free/unlocked tiers cost no GP; a paid tier whose session already
        exists simply returns its status URL again. The local zip is replaced
        once the re-download finishes.
        """
        if not self.has_ipb:
            raise ArchiverUnavailableError(
                "archiver requires IPB cookies (IPB_MEMBER_ID + IPB_PASS_HASH)"
            )
        meta = self.store.get(gid, token)
        if meta is None:
            raise EHException("no archive entry to refresh (start first)")
        return await self.start(gid, token, quality=meta.get("or"))

    async def remove(self, gid: int, token: str) -> bool:
        """Cancel any in-flight task and delete the local entry."""
        key = self.store.key(gid, token)
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._progress.pop(key, None)
        return await self.store.remove(gid, token)

    # -- metadata snapshot (gdata + cover, persisted locally) --------------

    async def _snapshot_metadata(
        self, gid: int, token: str, *, force: bool = False
    ) -> dict | None:
        """Fetch gdata metadata + cover and persist them as a local snapshot.

        Runs after a successful archive download (and on manual refresh).
        Best-effort by design: failures are logged and never fail the archive
        task — the entry stays ready; a missing snapshot is regenerated on
        the next manual refresh.
        """
        if self.service is None:
            return None
        try:
            meta = await self.service.get_metadata(gid, token, force=force)
            if meta is None:
                logger.warning(
                    "archive metadata snapshot skipped for %s:%s "
                    "(gdata returned nothing)", gid, token,
                )
                return None
            cover_mime = ""
            if meta.thumb:
                try:
                    data, cover_mime = await self.service.fetch_cover_bytes(meta.thumb)
                    await self.store.write_cover(gid, token, data)
                except EHException as exc:
                    # cover is optional: metadata snapshot is still persisted
                    logger.warning(
                        "archive cover snapshot failed for %s:%s (%s); "
                        "metadata kept", gid, token, exc,
                    )
            saved_at = time.time()
            payload = {
                **asdict(meta),
                "gid": gid,
                "token": token,
                "saved_at": saved_at,
                "cover_mime": cover_mime,
            }
            await self.store.write_metadata_snapshot(gid, token, payload)
            await self.store.upsert(gid, token, {"metadata_at": saved_at})
            return {
                "gid": gid,
                "token": token,
                "title": meta.title,
                "title_jpn": meta.title_jpn,
                "category": meta.category,
                "rating": meta.rating,
                "filecount": meta.filecount,
                "filesize": meta.filesize,
                "posted": meta.posted,
                "uploader": meta.uploader,
                "expunged": meta.expunged,
                "cover_mime": cover_mime or None,
                "saved_at": saved_at,
            }
        except Exception as exc:  # noqa: BLE001 - snapshot must never fail the task
            logger.warning(
                "archive metadata snapshot failed for %s:%s (%s)", gid, token, exc
            )
            return None

    async def refresh_metadata(self, gid: int, token: str) -> dict:
        """Force-refetch gdata metadata + cover and overwrite the snapshot."""
        meta = self.store.get(gid, token)
        if meta is None:
            raise EHException(
                "no archive entry to refresh metadata (start an archive first)"
            )
        summary = await self._snapshot_metadata(gid, token, force=True)
        if summary is None:
            raise EHException("metadata refresh failed (gdata returned nothing)")
        return summary

    async def get_metadata_snapshot(self, gid: int, token: str) -> dict | None:
        """Return the persisted gdata snapshot (None when not archived)."""
        return self.store.read_metadata_snapshot(gid, token)

    def get_status(self, gid: int, token: str) -> dict | None:
        return self._status(gid, token)

    def list_entries(self) -> list[dict]:
        return self.store.list_entries()

    def stats(self) -> dict:
        return self.store.stats()

    async def get_page_bytes(self, gid: int, token: str, page_no_1: int) -> bytes | None:
        """Page bytes from the archived zip (None when not archived/ready)."""
        return await self.store.get_page_bytes(gid, token, page_no_1)

    # -- status assembly ---------------------------------------------------

    def _status(self, gid: int, token: str) -> dict:
        meta = self.store.get(gid, token)
        if meta is None:
            return {"gid": gid, "token": token, "status": "absent"}
        key = self.store.key(gid, token)
        return {
            **meta,
            "active": key in self._tasks and not self._tasks[key].done(),
            "bytes_downloaded": self._progress.get(key, meta.get("bytes", 0)),
        }

    # -- task scheduling ---------------------------------------------------

    def _spawn(self, gid: int, token: str) -> None:
        key = self.store.key(gid, token)
        # single-threaded event loop: no await points below -> atomic check+create
        if key in self._tasks and not self._tasks[key].done():
            return
        task = asyncio.create_task(self._run_entry(gid, token))

        def _done(t: asyncio.Task, k: str = key) -> None:
            self._tasks.pop(k, None)
            self._progress.pop(k, None)

        task.add_done_callback(_done)
        self._tasks[key] = task

    # -- background pipeline ----------------------------------------------

    async def _run_entry(self, gid: int, token: str) -> None:
        try:
            await self._run(gid, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - terminal failed state
            logger.warning("archive task failed for %s:%s (%s)", gid, token, exc)
            await self._fail(gid, token, str(exc))

    async def _run(self, gid: int, token: str) -> None:
        meta = self.store.get(gid, token)
        if meta is None:
            return
        status = meta["status"]
        if status == ST_PENDING:
            await self._wait_ready(gid, token)
        meta = self.store.get(gid, token)
        if meta is None or meta["status"] != ST_PENDING:
            return  # cancelled / failed / removed meanwhile
        await self._download(gid, token)
        await self._finalize(gid, token)
        # Best-effort gdata + cover snapshot (never fails the task).
        await self._snapshot_metadata(gid, token)

    async def _wait_ready(self, gid: int, token: str) -> None:
        """Poll the hath.network status URL until the archive is ready."""
        meta = self.store.get(gid, token)
        if meta is None:
            return
        url = meta.get("download_url", "")
        if not url:
            raise EHException("no status URL to poll")
        deadline = time.monotonic() + _PREPARE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                html = await self._fetch_url(url)
            except EHException as exc:
                logger.warning("archive status poll failed (%s); retrying", exc)
            else:
                page = parse_archiver_page(html, page_url=url)
                if page.error:
                    raise self._map_error(page.error)
                if page.download_state == "ready" and page.download_url:
                    await self.store.upsert(gid, token, {
                        "download_url": page.download_url,
                    })
                    return
            await asyncio.sleep(_PREPARE_POLL_SECONDS)
        raise EHException("archive preparation timed out upstream")

    async def _download(self, gid: int, token: str) -> None:
        meta = self.store.get(gid, token)
        if meta is None:
            return
        url = meta.get("download_url", "")
        if not url or "?start=" not in url:
            raise EHException("no final download URL available")

        await self.store.upsert(gid, token, {"status": ST_DOWNLOADING, "error": None})
        part = self.store.part_path(gid, token)
        key = self.store.key(gid, token)

        async with self._slots:
            await self.throttle.circuit.check()
            await self.client.establish_session()
            offset = 0
            try:
                offset = part.stat().st_size
            except OSError:
                offset = 0
            self._progress[key] = offset

            def _on_progress(n: int) -> None:
                self._progress[key] = n

            total = 0
            try:
                total = await self.client.stream_archive(
                    url, part, progress_cb=_on_progress, offset=offset
                )
            finally:
                self._progress.pop(key, None)
            await self.store.upsert(gid, token, {
                "bytes": total,
                "total_bytes": total,
            })

    async def _finalize(self, gid: int, token: str) -> None:
        """Detect format, produce archive.zip, validate, mark ready."""
        part = self.store.part_path(gid, token)
        if not part.exists():
            raise EHException("download produced no file")
        fmt = _detect_archive_format(part)
        zip_path = self.store.zip_path(gid, token)

        if fmt == "zip":
            await asyncio.to_thread(os.replace, part, zip_path)
        elif fmt == "7z":
            await self.store.upsert(gid, token, {"status": ST_ZIPPING})
            ok = await self._convert_7z(part, zip_path)
            if not ok:
                raise EHException("7z conversion failed")
            await asyncio.to_thread(self._remove_file, part)
        else:
            # keep the unknown file for inspection, mark failed
            raise EHException(
                f"unknown archive format ({fmt}); file kept at {part.name}"
            )

        ok, count = await asyncio.to_thread(self._verify_zip, zip_path)
        if not ok:
            raise EHException("archive zip failed validation")
        await self.store.upsert(gid, token, {
            "status": ST_READY,
            "page_count": count,
            "total_bytes": zip_path.stat().st_size if zip_path.exists() else 0,
        })
        logger.info("archive ready: %s:%s (%d pages, %d bytes)",
                    gid, token, count, zip_path.stat().st_size if zip_path.exists() else 0)

    async def _convert_7z(self, src: Path, dst: Path) -> bool:
        """Re-package a 7z archive as zip, preserving internal (page) order."""
        if not _HAS_PY7ZR:
            logger.error("py7zr not installed; cannot convert 7z archive %s", src)
            return False
        tmpdir = src.parent / ".tmp7z"
        try:
            await asyncio.to_thread(tmpdir.mkdir, parents=True, exist_ok=True)

            def _extract() -> list[str]:
                with py7zr.SevenZipFile(src) as z:
                    names = z.getnames()
                    z.extractall(tmpdir)
                    return names

            names = await asyncio.to_thread(_extract)

            def _repack() -> None:
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as zf:
                    for name in names:
                        src_file = tmpdir / name
                        zf.write(src_file, name)

            await asyncio.to_thread(_repack)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("7z -> zip conversion failed for %s (%s)", src, exc)
            return False
        finally:
            await asyncio.to_thread(self._remove_tree, tmpdir)

    def _verify_zip(self, path: Path) -> tuple[bool, int]:
        """Verify the zip opens, has entries, and the first page is readable."""
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if not names:
                    return False, 0
                zf.read(names[0])  # spot-check the first member's data
                return True, len(names)
        except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
            return False, 0

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        for child in path.iterdir():
            if child.is_dir():
                ArchiveManager._remove_tree(child)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
        try:
            path.rmdir()
        except OSError:
            pass

    async def _fail(self, gid: int, token: str, message: str) -> None:
        await self.store.upsert(gid, token, {
            "status": ST_FAILED,
            "error": message[:500],
        })
