"""Sync snapshot persistence for the favorites scanner.

A single JSON file (``FAVORITES_SYNC_STATE``) records what the incremental
scanner has already seen/archived so each run only picks up new items. Written
atomically (temp file + rename) exactly like the archive store's meta.json.

Shape::

    {
      "known": ["123:abc", ...],      # gid:token strings seen (scoped)
      "archived": ["123:abc", ...],   # gid:token strings auto-archived
      "errors": {"123:abc": "msg"},   # last failure per gid (auto-archive)
      "baseline": true,                # first run recorded everything
      "last_run": 1725000000.0,       # unix ts
      "last_ok": true,                # overall success of the last run
      "scanned_pages": 3
    }

``baseline`` is set by the FIRST successful run: that run records every scoped
favorite as known but never auto-archives (a fresh snapshot must not treat all
current favorites as "new" — that would mass-spend GP). Auto-archive only
applies from the second run onward.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


class FavoritesSyncState:
    """Thread-safe (async-locked) JSON state file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._data: dict = self._load()

    # -- internals ---------------------------------------------------------

    def _load(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "known": [],
            "archived": [],
            "errors": {},
            "baseline": False,
            "last_run": 0.0,
            "last_ok": True,
            "scanned_pages": 0,
        }

    async def _atomic_write(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        data = json.dumps(self._data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(tmp.write_text, data, "utf-8")
        await asyncio.to_thread(os.replace, tmp, self.path)

    # -- read API ----------------------------------------------------------

    def known(self) -> set[str]:
        return set(self._data.get("known") or [])

    def archived(self) -> set[str]:
        return set(self._data.get("archived") or [])

    def errors(self) -> dict[str, str]:
        return dict(self._data.get("errors") or {})

    def baseline_established(self) -> bool:
        """True once the first (baseline) scan has completed."""
        return bool(self._data.get("baseline", False))

    def summary(self) -> dict:
        return {
            "known_count": len(self.known()),
            "archived_count": len(self.archived()),
            "error_count": len(self.errors()),
            "baseline": self.baseline_established(),
            "last_run": self._data.get("last_run", 0.0),
            "last_ok": self._data.get("last_ok", True),
            "scanned_pages": self._data.get("scanned_pages", 0),
        }

    # -- mutations ---------------------------------------------------------

    async def update(self, *, known: set[str] | None = None,
                     archived: set[str] | None = None,
                     errors: dict[str, str] | None = None,
                     baseline: bool | None = None,
                     last_ok: bool | None = None,
                     scanned_pages: int | None = None) -> None:
        """Merge new values into the snapshot and persist atomically."""
        async with self._lock:
            if known is not None:
                self._data["known"] = sorted(known)
            if archived is not None:
                self._data["archived"] = sorted(archived)
            if errors is not None:
                self._data["errors"] = errors
            if baseline is not None:
                self._data["baseline"] = baseline
            if last_ok is not None:
                self._data["last_ok"] = last_ok
            if scanned_pages is not None:
                self._data["scanned_pages"] = scanned_pages
            self._data["last_run"] = time.time()
            await self._atomic_write()
