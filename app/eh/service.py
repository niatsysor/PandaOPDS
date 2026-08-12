"""E-Hentai service layer: orchestrates client, parser, cache and throttle.

Every upstream access goes through this class (project rule #3).
Pipeline for one image: page-URL cache -> detail page (1 req / 20 pages)
-> /s/ page (image URL) -> image bytes (disk cached).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from ..cache.disk import DiskImageCache, detect_image_type
from ..cache.memory import MemoryCache
from ..config import Settings
from ..throttle.limiter import KIND_API, KIND_HTML, KIND_IMAGE, Throttle
from .client import EHClient
from .exceptions import EHException, ExceedLimitError, PageNotFoundError
from .models import (
    DetailPageInfo,
    GalleryMetadata,
    GalleryPageInfo,
    ImagePageInfo,
)
from .parser import parse_detail_page, parse_gdata_response, parse_image_page, parse_list_page

logger = logging.getLogger(__name__)

GDATA_BATCH_SIZE = 25
THUMBS_PER_DETAIL_PAGE = 20

# Ranklist periods -> `?tl=` value (aligned with JHenTai RanklistType:
# day=15, month=13, year=12, allTime=11).
TOPLIST_TL = {"yesterday": 15, "month": 13, "year": 12, "alltime": 11}


class EHService:
    def __init__(
        self,
        settings: Settings,
        client: EHClient | None = None,
        throttle: Throttle | None = None,
        memory_cache: MemoryCache | None = None,
        disk_cache: DiskImageCache | None = None,
    ):
        self.settings = settings
        self.client = client or EHClient(settings)
        self.throttle = throttle or Throttle(settings)
        self.mem = memory_cache or MemoryCache()
        self.disk = disk_cache or (
            DiskImageCache(
                settings.cache_dir,
                max_gb=settings.cache_max_gb,
                enabled=settings.image_cache_enabled,
            )
        )
        self.site_host = settings.site_host

    async def close(self) -> None:
        await self.client.close()

    # -- internal helpers --------------------------------------------------

    def _mem_key(self, *parts: object) -> str:
        return ":".join(str(p) for p in parts)

    async def _cached(
        self,
        key: str,
        ttl: float,
        factory: Callable[[], Awaitable],
    ):
        return await self.mem.get_or_set(key, factory, ttl)

    async def _trip_if_fatal(self, exc: EHException) -> None:
        """Trip the circuit breaker for hard failures (banned / image limit)."""
        from .exceptions import BannedError, ExceedLimitError as _E

        if isinstance(exc, (BannedError, _E)):
            await self.throttle.trip(f"{type(exc).__name__}: {exc}")

    # -- list pages --------------------------------------------------------

    async def _list_page(
        self,
        kind: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> GalleryPageInfo:
        """Fetch + parse a list page, caching the parse result (short TTL).

        List pages are the cheapest upstream objects but the home feed and
        toplist feeds hit them repeatedly, so parse results are cached for
        `list_cache_ttl_seconds` (default 10min).
        """
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        key = self._mem_key("list", kind, query)

        async def _fetch() -> GalleryPageInfo:
            html_text = await self._html_get(path, params=params)
            return parse_list_page(html_text)

        return await self.mem.get_or_set(
            key, _fetch, self.settings.list_cache_ttl_seconds
        )

    async def search_galleries(
        self,
        query: str = "",
        last_gid: int | None = None,
        f_cats: int | None = None,
    ) -> GalleryPageInfo:
        """Search / latest list page.

        `last_gid` enables `next` pagination.
        `f_cats` is the EH exclude-category bitmask (e.g. 1021 = Doujinshi only).
        """
        params: dict[str, str] = {}
        if query:
            params["f_search"] = query
        if f_cats is not None:
            params["f_cats"] = str(f_cats)
        if last_gid is not None:
            params["next"] = str(last_gid)
        # Cache key includes f_cats so different category filters don't collide.
        q_key = f"{query}:f_cats={f_cats}" if f_cats is not None else query
        return await self._list_page("search:" + q_key, "/", params)

    async def popular_galleries(self, last_gid: int | None = None) -> GalleryPageInfo:
        params = {"next": str(last_gid)} if last_gid is not None else {}
        return await self._list_page("popular", "/popular", params)

    async def watched_galleries(self, last_gid: int | None = None) -> GalleryPageInfo:
        """Watched galleries list (/watched). Reuses the standard list parser."""
        params = {"next": str(last_gid)} if last_gid is not None else {}
        return await self._list_page("watched", "/watched", params)

    async def favorites_galleries(self, last_gid: int | None = None) -> GalleryPageInfo:
        """Favorites list (/favorites.php). Reuses the standard list parser."""
        params = {"next": str(last_gid)} if last_gid is not None else {}
        return await self._list_page("favorites", "/favorites.php", params)

    async def toplist_galleries(
        self, period: str = "yesterday", page: int = 1
    ) -> GalleryPageInfo:
        """Ranklist page (toplist.php). Periods map to `?tl=` values; the page
        uses `.ptt` page-number pagination (`?p=`), parsed into `next_page`.

        JHenTai reuses the compact list parser for ranklist rows
        (`ranklistPage2GalleryPageInfo` -> `_parseCompactGallery`), so
        `parse_list_page` handles the layout; the rank column is ignored.
        """
        tl = TOPLIST_TL.get(period)
        if tl is None:
            raise EHException(
                f"unknown toplist period {period!r} "
                f"(expected one of {sorted(TOPLIST_TL)})"
            )
        params: dict[str, str] = {"tl": str(tl)}
        if page > 1:
            params["p"] = str(page)
        return await self._list_page(f"toplist:{period}:{page}", "https://e-hentai.org/toplist.php", params)

    async def _html_get(self, path: str, params: dict[str, str] | None = None) -> str:
        async with self.throttle.acquired(KIND_HTML):
            try:
                await self.client.establish_session()
                if path.startswith("http://") or path.startswith("https://"):
                    # absolute upstream URL (e.g. /s/ hrefs returned absolute)
                    resp = await self.client.get_absolute_html(path, params=params)
                    return resp
                return await self.client.get_html(path, params=params)
            except EHException as exc:
                await self._trip_if_fatal(exc)
                raise

    # -- gdata metadata ----------------------------------------------------

    async def get_metadata(self, gid: int, token: str) -> GalleryMetadata | None:
        key = self._mem_key("meta", gid, token)

        async def _fetch() -> GalleryMetadata | None:
            items = await self._gdata([(gid, token)])
            return items[0] if items else None

        return await self.mem.get_or_set(key, _fetch, self.settings.metadata_ttl_seconds)

    async def get_metadatas(
        self, items: list[tuple[int, str]]
    ) -> list[GalleryMetadata]:
        """Batch metadata lookup (max 25 gid per upstream request), cached."""
        return await self._metadatas_impl(items)

    async def _metadatas_impl(self, items: list[tuple[int, str]]) -> list[GalleryMetadata]:
        out: list[GalleryMetadata] = []
        to_fetch: list[tuple[int, str]] = []
        for gid, token in items:
            key = self._mem_key("meta", gid, token)
            cached = await self.mem.get(key)
            if cached is not None:
                out.append(cached)
            else:
                to_fetch.append((gid, token))

        for i in range(0, len(to_fetch), GDATA_BATCH_SIZE):
            batch = to_fetch[i : i + GDATA_BATCH_SIZE]
            for meta in await self._gdata(batch):
                await self.mem.set(
                    self._mem_key("meta", meta.gid, meta.token),
                    meta,
                    self.settings.metadata_ttl_seconds,
                )
                out.append(meta)
        return out

    async def _gdata(self, gidlist: list[tuple[int, str]]) -> list[GalleryMetadata]:
        if not gidlist:
            return []
        payload = {
            "method": "gdata",
            "gidlist": [[gid, token] for gid, token in gidlist],
            "namespace": 1,
        }
        async with self.throttle.acquired(KIND_API):
            try:
                await self.client.establish_session()
                body = await self.client.post_api(payload)
            except EHException as exc:
                await self._trip_if_fatal(exc)
                raise
        return parse_gdata_response(body)

    # -- detail pages (page-URL mapping) -----------------------------------

    async def get_detail_page(
        self, gid: int, token: str, page_index: int
    ) -> DetailPageInfo:
        """Detail page `page_index` (0-based); each page maps 20 images.

        Cached for 1h so a single HTML request serves 20 /stream requests;
        single-flight dedupes concurrent cold-cache misses.
        """
        key = self._mem_key("detail", gid, token, page_index)

        async def _fetch() -> DetailPageInfo:
            html_text = await self._html_get(
                f"/g/{gid}/{token}/", params={"p": str(page_index)}
            )
            return parse_detail_page(html_text, self.site_host, page_index)

        return await self.mem.get_or_set(
            key, _fetch, self.settings.page_url_ttl_seconds
        )

    async def resolve_image_page(
        self, gid: int, token: str, page_no: int
    ) -> ImagePageInfo:
        """Resolve the real image URL for 1-based `page_no` via the /s/ page."""
        key = self._mem_key("imgpage", gid, token, page_no)

        async def _fetch() -> ImagePageInfo:
            detail = await self.get_detail_page(
                gid, token, (page_no - 1) // THUMBS_PER_DETAIL_PAGE
            )
            thumbnails = detail.thumbnails
            idx = (page_no - 1) % THUMBS_PER_DETAIL_PAGE
            if idx >= len(thumbnails):
                raise PageNotFoundError(f"page {page_no} out of range for gallery {gid}")
            href = thumbnails[idx].href
            if not href.startswith("/s/") and not href.startswith("https://"):
                raise EHException(f"unexpected thumbnail href {href!r}")
            html_text = await self._html_get(href)
            return parse_image_page(html_text)

        return await self.mem.get_or_set(
            key, _fetch, self.settings.page_url_ttl_seconds
        )

    # -- image bytes -------------------------------------------------------

    async def get_image(
        self, gid: int, token: str, page_no: int
    ) -> tuple[bytes, str]:
        """Return (image bytes, mime type) for stream page `page_no`.

        `page_no` follows the configured PSE page base (default 1-based,
        LANraragi/Kasane compatible; 0-based with PSE_PAGE_BASE=0). Internally
        E-Hentai /s/ pages are always 1-based.
        """
        base = self.settings.pse_page_base
        if page_no < base:
            raise PageNotFoundError(
                f"page {page_no} invalid (pages start at {base})"
            )
        # E-Hentai /s/ pageNo is 1-based
        page_no_1 = page_no if base == 1 else page_no + 1

        if self.disk.enabled:
            data = await self.disk.get(gid, token, page_no_1)
            if data is not None:
                return data, detect_image_type(data)

        info = await self.resolve_image_page(gid, token, page_no_1)
        if info.is_509:
            raise ExceedLimitError("image limit exceeded (509 placeholder)")

        referer = f"{self.settings.http_origin}/s/{gid}-{page_no_1}"
        try:
            data = await self._fetch_image_bytes(info.image_url, referer)
        except EHException:
            # retry once with the nl() reload key if we have one
            if info.reload_key:
                retry_url = f"{info.image_url}?nl={info.reload_key}"
                data = await self._fetch_image_bytes(retry_url, referer)
            else:
                raise

        if self.disk.enabled:
            await self.disk.put(gid, token, page_no_1, data)
        return data, detect_image_type(data)

    async def _fetch_image_bytes(self, url: str, referer: str) -> bytes:
        async with self.throttle.acquired(KIND_IMAGE):
            try:
                await self.client.establish_session()
                return await self.client.fetch_image_bytes(url, referer=referer)
            except EHException as exc:
                await self._trip_if_fatal(exc)
                raise

    # -- thumbnails --------------------------------------------------------

    async def get_thumb_url(self, gid: int, token: str) -> str:
        """Return the thumbnail URL (for a 302 redirect from /image/...)."""
        meta = await self.get_metadata(gid, token)
        if meta and meta.thumb:
            return meta.thumb
        # fallback: first thumbnail of the detail page
        detail = await self.get_detail_page(gid, token, 0)
        if detail.thumbnails:
            return detail.thumbnails[0].thumb_url
        raise EHException(f"no thumbnail found for gallery {gid}")

    async def get_thumb(self, gid: int, token: str) -> tuple[bytes, str]:
        """Proxy the gallery thumbnail, disk-cached under page_no=-1."""
        if self.disk.enabled:
            data = await self.disk.get(gid, token, -1)
            if data is not None:
                return data, detect_image_type(data)
        url = await self.get_thumb_url(gid, token)
        data = await self._fetch_image_bytes(url, referer=f"{self.settings.http_origin}/")
        if self.disk.enabled:
            await self.disk.put(gid, token, -1, data)
        return data, detect_image_type(data)

    # -- misc --------------------------------------------------------------

    async def stats(self) -> dict:
        return {
            "throttle": {
                "html_requests": self.throttle.html_requests,
                "api_requests": self.throttle.api_requests,
                "image_requests": self.throttle.image_requests,
                "circuit_open": self.throttle.circuit.is_open,
            },
            "memory_cache": self.mem.stats,
            "disk_cache": self.disk.stats,
        }
