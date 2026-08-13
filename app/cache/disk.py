"""On-disk LRU image cache.

- Files stored as `CACHE_DIR/{sha256(key)[:2]}/{sha256(key)[2:]}` (raw bytes).
- LRU eviction by file mtime, capped at `CACHE_MAX_GB` (default 4 GB).
- TTL 7 days: entries older than TTL are expired lazily on access and by a
  periodic sweep during `put`.
- Writes happen in a thread executor so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TTL = 7 * 24 * 3600


def detect_image_type(data: bytes) -> str:
    """Return a MIME type guessed from magic bytes (jpg/png/gif/webp/bmp)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


class DiskImageCache:
    def __init__(
        self,
        cache_dir: str | Path,
        max_gb: float = 4.0,
        ttl_seconds: float = DEFAULT_TTL,
        enabled: bool = True,
    ):
        self.cache_dir = Path(cache_dir)
        self.max_bytes = int(max_gb * 1024 * 1024 * 1024)
        self.ttl = ttl_seconds
        self.enabled = enabled
        self._entries: dict[str, tuple[str, int, float]] = {}  # key -> (path, size, mtime)
        self._total_bytes = 0
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._scan()

    def _scan(self) -> None:
        """Load the file index from disk (called once at startup)."""
        now = time.time()
        for path in self.cache_dir.rglob("*"):
            if path.is_file():
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                key = path.name
                self._entries[key] = (str(path), size, path.stat().st_mtime)
                self._total_bytes += size
        # expire old files
        for key, (path, size, mtime) in list(self._entries.items()):
            if now - mtime > self.ttl:
                self._remove_entry(key, path, size)
        logger.info(
            "disk cache loaded: %d entries, %.1f MB",
            len(self._entries), self._total_bytes / 1024 / 1024,
        )

    @staticmethod
    def _key(gid: int, token: str, page_no: int) -> str:
        return hashlib.sha256(f"{gid}:{token}:{page_no}".encode()).hexdigest()

    def _path_for(self, key: str) -> Path:
        return self.cache_dir / key[:2] / key[2:]

    async def get(self, gid: int, token: str, page_no: int) -> bytes | None:
        """Return cached image bytes, or None on miss/expiry."""
        if not self.enabled:
            return None
        key = self._key(gid, token, page_no)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            path, size, mtime = entry
            if time.time() - mtime > self.ttl:
                self._remove_entry(key, path, size)
                self._misses += 1
                return None
            try:
                data = await asyncio.to_thread(Path(path).read_bytes)
            except OSError:
                self._misses += 1
                return None
            # refresh mtime (LRU)
            try:
                os.utime(path)
                self._entries[key] = (path, size, time.time())
            except OSError:
                pass
            self._hits += 1
            return data

    async def put(self, gid: int, token: str, page_no: int, data: bytes) -> Path:
        """Store image bytes on disk (async write). Returns the file path."""
        if not self.enabled:
            return self._path_for(self._key(gid, token, page_no))
        key = self._key(gid, token, page_no)
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with self._lock:
            await asyncio.to_thread(path.write_bytes, data)
            now = time.time()
            old = self._entries.get(key)
            if old is not None:
                self._total_bytes -= old[1]
            self._entries[key] = (str(path), len(data), now)
            self._total_bytes += len(data)
            self._evict_if_needed()

    def _remove_entry(self, key: str, path: str, size: int) -> None:
        self._entries.pop(key, None)
        self._total_bytes -= size
        try:
            os.unlink(path)
        except OSError:
            pass

    def _evict_if_needed(self) -> None:
        """Evict least-recently-used entries while over the size cap."""
        while self._total_bytes > self.max_bytes and self._entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k][2])
            path, size, _ = self._entries[oldest_key]
            logger.info("evicting cache entry %s (%.1f MB)", oldest_key, size / 1024 / 1024)
            self._remove_entry(oldest_key, path, size)

    @property
    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "entries": len(self._entries),
            "bytes": self._total_bytes,
            "enabled": self.enabled,
        }
