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

    def favcats(self) -> dict[str, int]:
        """Per-gallery favcat id (gid:token -> favcat)."""
        raw = self._data.get("favcats") or {}
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def favcat_map(self) -> dict[int, str]:
        """Long-lived favcat id -> name cache (fallback when upstream fails)."""
        raw = self._data.get("favcat_map") or {}
        out: dict[int, str] = {}
        for k, v in raw.items():
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        return out

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

    async def discard_keys(self, keys: set[str]) -> bool:
        """Remove keys from known/archived/errors/favcats (immediate prune after unfavorite).

        Returns True if anything was removed."""
        async with self._lock:
            known = set(self._data.get("known") or [])
            archived = set(self._data.get("archived") or [])
            errors = dict(self._data.get("errors") or {})
            favcats = dict(self._data.get("favcats") or {})
            before = (len(known), len(archived), len(errors), len(favcats))
            known -= keys
            archived -= keys
            for k in keys:
                errors.pop(k, None)
                favcats.pop(k, None)
            if (len(known), len(archived), len(errors), len(favcats)) == before:
                return False
            self._data["known"] = sorted(known)
            self._data["archived"] = sorted(archived)
            self._data["errors"] = errors
            self._data["favcats"] = favcats
            self._data["last_run"] = time.time()
            await self._atomic_write()
            return True

    async def update(self, *, known: set[str] | None = None,
                     archived: set[str] | None = None,
                     errors: dict[str, str] | None = None,
                     favcats: dict[str, int] | None = None,
                     favcat_map: dict[int, str] | None = None,
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
            if favcats is not None:
                self._data["favcats"] = {str(k): int(v) for k, v in favcats.items()}
            if favcat_map is not None:
                # merge, long-lived cache
                cur = self.favcat_map()
                cur.update({int(k): str(v) for k, v in favcat_map.items()})
                self._data["favcat_map"] = {str(k): v for k, v in cur.items()}
            if baseline is not None:
                self._data["baseline"] = baseline
            if last_ok is not None:
                self._data["last_ok"] = last_ok
            if scanned_pages is not None:
                self._data["scanned_pages"] = scanned_pages
            self._data["last_run"] = time.time()
            await self._atomic_write()
