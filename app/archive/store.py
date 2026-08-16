"""Persistent archive data directory (store).

Layout: ``ARCHIVE_DIR/{gid}/{token}/``

    archive.zip   — unified zip (cbz) master: random-access page source
    archive.part  — in-flight download target (renamed/removed when done)
    meta.json     — atomic-write metadata (the state machine's source of truth)

The store is a plain on-disk index (directory == database): startup rescans
``*/meta.json`` to rebuild the in-memory index; a corrupt entry is isolated
(flagged + skipped) rather than failing the whole scan.

The archive directory is deliberately NOT part of the disk LRU cache: no TTL,
no CACHE_MAX_GB, no eviction. Entries live until explicitly removed by the
user (WebUI/API). Deleting a local entry never touches the E-Hentai account
archive record — the same archive hash can be re-downloaded later without
spending GP again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# --- archive states (state machine) ---
ST_ABSENT = "absent"          # no local record
ST_QUOTING = "quoting"        # fetching archiver.php options (price preview)
ST_PENDING = "pending"        # submitted to EH, archive being prepared
ST_DOWNLOADING = "downloading"  # bytes flowing into archive.part
ST_ZIPPING = "zipping"        # 7z -> zip conversion (background)
ST_READY = "ready"            # archive.zip complete + validated
ST_FAILED = "failed"          # terminal, retryable (start again)

META_FILENAME = "meta.json"
ARCHIVE_FILENAME = "archive.zip"
PART_FILENAME = "archive.part"
# Local gdata metadata + cover snapshot (regenerable; separate from meta.json,
# which is the state machine's source of truth).
METADATA_FILENAME = "metadata.json"
COVER_FILENAME = "cover.jpg"

# Terminal-ish states: a ready entry backs /stream; failed entries are kept
# on disk for inspection and can be restarted.
_READY_STATES = frozenset({ST_READY})

# meta.json fields persisted per entry.
_META_FIELDS = (
    "gid",
    "token",
    "title",
    "quality",
    "or",
    "download_url",
    "gp_price",
    "status",
    "bytes",
    "total_bytes",
    "page_count",
    "created_at",
    "updated_at",
    "error",
    "metadata_at",  # unix ts of the last successful gdata snapshot
)


def _now() -> float:
    return time.time()


class ArchiveStore:
    """On-disk archive directory: index, meta writes, zip page reads."""

    def __init__(self, archive_dir: str | Path):
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict] = {}  # "gid:token" -> meta dict
        # namelist cache for zip page reads: key -> (zip mtime, list[str])
        self._namelists: dict[str, tuple[float, list[str]]] = {}
        self._lock = asyncio.Lock()
        self._scan()

    # -- paths -------------------------------------------------------------

    def entry_dir(self, gid: int, token: str) -> Path:
        return self.archive_dir / str(gid) / token

    def meta_path(self, gid: int, token: str) -> Path:
        return self.entry_dir(gid, token) / META_FILENAME

    def zip_path(self, gid: int, token: str) -> Path:
        return self.entry_dir(gid, token) / ARCHIVE_FILENAME

    def part_path(self, gid: int, token: str) -> Path:
        return self.entry_dir(gid, token) / PART_FILENAME

    def metadata_path(self, gid: int, token: str) -> Path:
        return self.entry_dir(gid, token) / METADATA_FILENAME

    def cover_path(self, gid: int, token: str) -> Path:
        return self.entry_dir(gid, token) / COVER_FILENAME

    def key(self, gid: int, token: str) -> str:
        return f"{gid}:{token}"

    # -- index -------------------------------------------------------------

    def _scan(self) -> None:
        """Rebuild the in-memory index from ``*/meta.json`` files."""
        found = 0
        corrupt = 0
        for meta_file in self.archive_dir.rglob(META_FILENAME):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                corrupt += 1
                logger.warning("archive meta unreadable %s (%s); kept on disk", meta_file, exc)
                continue
            gid = meta.get("gid")
            token = meta.get("token")
            if gid is None or not token:
                corrupt += 1
                logger.warning("archive meta missing gid/token in %s; skipped", meta_file)
                continue
            self._entries[self.key(int(gid), str(token))] = meta
            found += 1
        total = sum(self._size_of(int(k.split(":", 1)[0]), k.split(":", 1)[1])[0]
                    for k in self._entries)
        logger.info(
            "archive store loaded: %d entries, %.1f MB (%d corrupt skipped)",
            len(self._entries), total / 1024 / 1024, corrupt,
        )

    def _size_of(self, gid: int, token: str) -> tuple[int, int]:
        """(zip_bytes, part_bytes) currently on disk."""
        z = self.zip_path(gid, token)
        p = self.part_path(gid, token)
        try:
            zb = z.stat().st_size
        except OSError:
            zb = 0
        try:
            pb = p.stat().st_size
        except OSError:
            pb = 0
        return zb, pb

    # -- read API ----------------------------------------------------------

    def get(self, gid: int, token: str) -> dict | None:
        """Return the entry meta (with derived runtime fields) or None."""
        meta = self._entries.get(self.key(gid, token))
        if meta is None:
            return None
        return self._decorate(meta, gid, token)

    def list_entries(self) -> list[dict]:
        """All entries, newest first, with derived runtime fields."""
        out = []
        for key, meta in self._entries.items():
            gid, token = key.split(":", 1)
            out.append(self._decorate(meta, int(gid), token))
        out.sort(key=lambda m: m.get("created_at", 0.0), reverse=True)
        return out

    def _decorate(self, meta: dict, gid: int, token: str) -> dict:
        zip_bytes, part_bytes = self._size_of(gid, token)
        return {
            **meta,
            "gid": int(meta.get("gid", gid)),
            "token": str(meta.get("token", token)),
            "archive_size": zip_bytes,
            "part_size": part_bytes,
        }

    # -- mutations ---------------------------------------------------------

    async def upsert(self, gid: int, token: str, patch: dict) -> dict:
        """Merge ``patch`` into the entry meta and atomically persist.

        The entry directory is created on first write. ``created_at`` is set
        on creation, ``updated_at`` on every mutation.
        """
        async with self._lock:
            key = self.key(gid, token)
            meta = dict(self._entries.get(key) or {})
            now = _now()
            if "created_at" not in meta:
                meta["created_at"] = now
            meta.update(
                {
                    k: v
                    for k, v in patch.items()
                    if k in _META_FIELDS and v is not None
                }
            )
            meta["updated_at"] = now
            # keep gid/token in sync even if the caller omitted them
            meta["gid"] = gid
            meta["token"] = token
            entry_dir = self.entry_dir(gid, token)
            await asyncio.to_thread(entry_dir.mkdir, parents=True, exist_ok=True)
            await self._atomic_write(self.meta_path(gid, token), meta)
            self._entries[key] = meta
            return dict(meta)

    async def _atomic_write(self, path: Path, meta: dict) -> None:
        """Write meta.json via a temp file + rename (never a partial file)."""
        tmp = path.with_suffix(".json.tmp")
        data = json.dumps(meta, ensure_ascii=False, indent=2)
        await asyncio.to_thread(tmp.write_text, data, "utf-8")
        await asyncio.to_thread(os.replace, tmp, path)

    async def write_metadata_snapshot(self, gid: int, token: str, payload: dict) -> None:
        """Persist a gdata metadata snapshot (metadata.json), atomically.

        A local copy of upstream metadata + cover info, separate from
        meta.json (the state machine's source of truth) and regenerable on
        demand. The whole entry directory is removed together on delete.
        """
        await self._atomic_write(self.metadata_path(gid, token), payload)

    def read_metadata_snapshot(self, gid: int, token: str) -> dict | None:
        """Read the persisted gdata snapshot (None when absent/unreadable)."""
        try:
            return json.loads(
                self.metadata_path(gid, token).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None

    async def write_cover(self, gid: int, token: str, data: bytes) -> None:
        """Persist the local cover copy (cover.jpg)."""
        await asyncio.to_thread(self.cover_path(gid, token).write_bytes, data)

    async def remove(self, gid: int, token: str) -> bool:
        """Delete the whole entry directory (meta + zip + part).

        Returns True when an entry existed and was removed. The manager must
        cancel any in-flight task before calling this.
        """
        async with self._lock:
            key = self.key(gid, token)
            existed = key in self._entries
            entry_dir = self.entry_dir(gid, token)
            try:
                await asyncio.to_thread(self._rmtree, entry_dir)
            except OSError as exc:
                logger.warning("archive remove failed for %s (%s)", key, exc)
                return False
            self._entries.pop(key, None)
            self._namelists.pop(key, None)
            if existed:
                logger.info("archive entry removed: %s", key)
            return existed

    @staticmethod
    def _rmtree(path: Path) -> None:
        if path.is_dir():
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
            # prune the now-empty {gid} shard (harmless if non-empty)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        elif path.exists():
            path.unlink()

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        total_bytes = 0
        ready = 0
        with_metadata = 0
        for key, meta in self._entries.items():
            gid, token = key.split(":", 1)
            status = meta.get("status", ST_ABSENT)
            by_status[status] = by_status.get(status, 0) + 1
            zip_bytes, part_bytes = self._size_of(int(gid), token)
            total_bytes += zip_bytes + part_bytes
            if status == ST_READY:
                ready += 1
            if meta.get("metadata_at"):
                with_metadata += 1
        return {
            "entries": len(self._entries),
            "ready": ready,
            "by_status": by_status,
            "bytes": total_bytes,
            "with_metadata": with_metadata,
        }

    # -- zip page reads ----------------------------------------------------

    def is_ready(self, gid: int, token: str) -> bool:
        meta = self._entries.get(self.key(gid, token))
        return bool(meta) and meta.get("status") in _READY_STATES

    def _namelist(self, gid: int, token: str) -> list[str] | None:
        """Entry names in page order (zip internal order), cached by zip mtime."""
        path = self.zip_path(gid, token)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = self.key(gid, token)
        cached = self._namelists.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile) as exc:
            logger.warning("archive zip unreadable %s (%s)", path, exc)
            return None
        self._namelists[key] = (mtime, names)
        return names

    async def get_page_bytes(self, gid: int, token: str, page_no_1: int) -> bytes | None:
        """Return page bytes from the zip master (1-based page number).

        Returns None when the gallery is not archived, the zip is unreadable,
        or the page is out of range — the caller falls through to the normal
        upstream pipeline.
        """
        if not self.is_ready(gid, token):
            return None

        def _read() -> bytes | None:
            names = self._namelist(gid, token)
            if not names:
                return None
            idx = page_no_1 - 1
            if idx < 0 or idx >= len(names):
                return None
            path = self.zip_path(gid, token)
            try:
                with zipfile.ZipFile(path) as zf:
                    return zf.read(names[idx])
            except (OSError, zipfile.BadZipFile, KeyError) as exc:
                logger.warning("archive zip page read failed %s p%d (%s)", path, page_no_1, exc)
                return None

        return await asyncio.to_thread(_read)

    def page_count(self, gid: int, token: str) -> int | None:
        """Number of pages in the zip master (None when not ready/unreadable)."""
        names = self._namelist(gid, token)
        return len(names) if names else None
