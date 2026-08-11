"""OPDS routes: root nav feed, OpenSearch, gallery feeds, chapter feeds."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import GalleryListItem, GalleryMetadata
from ..eh.service import EHService
from .feed import (
    MIME_ACQ,
    MIME_NAV,
    MIME_OPEN_SEARCH,
    MIME_THUMB,
    REL_ACQUISITION,
    REL_STREAM,
    REL_THUMB,
    FeedBuilder,
    FeedEntry,
    FeedLink,
    _iso,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opds/v1.2", tags=["opds"])

# Root navigation entries (v1.2, pure navigation — no extensions, no
# publications mixing). Home is intentionally absent: on v1.2 the Latest feed
# lives at /opds/v1.2/gallery (no Home entry; the v2.0 home embeds Latest as
# top-level publications instead). Watched/Favorites are auth-gated.
_ROOT_NAV_BASE = [
    ("Watched", "/opds/v1.2/gallery?query=watched", "Watched galleries", "watched"),
    ("Favorites", "/opds/v1.2/gallery?query=favorites", "Favorite galleries", "favorites"),
    ("Popular", "/opds/v1.2/gallery?query=popular", "Popular this week", "popular"),
    ("Toplist: Yesterday", "/opds/v1.2/toplist?period=yesterday", "Top galleries of the last 24 hours", "toplist:yesterday"),
    ("Toplist: Past Month", "/opds/v1.2/toplist?period=month", "Top galleries of the past month", "toplist:month"),
    ("Toplist: Past Year", "/opds/v1.2/toplist?period=year", "Top galleries of the past year", "toplist:year"),
    ("Toplist: All Time", "/opds/v1.2/toplist?period=alltime", "Top galleries of all time", "toplist:alltime"),
    ("Search", "/opds/v1.2/search.xml", "Search E-Hentai galleries", "search"),
]

# Gallery list titles for the built-in browsing dimensions.
_LIST_TITLES = {"popular": "Popular", "watched": "Watched", "favorites": "Favorites"}

_TOPLIST_PERIODS = {
    "yesterday": "Yesterday",
    "month": "Past Month",
    "year": "Past Year",
    "alltime": "All Time",
}


def _service(request: Request) -> EHService:
    return request.app.state.service


def _builder(request: Request) -> FeedBuilder:
    return FeedBuilder(request.app.state.settings)


def _entry_href(builder: FeedBuilder, gid: int, token: str) -> str:
    return builder.href(f"/opds/v1.2/gallery/{gid}/{token}/chapters")


def _gallery_entry(
    builder: FeedBuilder,
    item: GalleryListItem,
    meta: GalleryMetadata | None,
) -> FeedEntry:
    title = meta.title if meta and meta.title else item.title
    category = meta.category if meta and meta.category else item.category
    updated = _iso(meta.posted) if meta and meta.posted else _iso()
    author = meta.uploader if meta else ""
    page_count = meta.filecount if meta and meta.filecount else item.page_count

    parts: list[str] = []
    if meta:
        parts.append(f"Language: {meta.language}")
        parts.append(f"Pages: {meta.filecount}")
        parts.append(f"Uploader: {meta.uploader or 'unknown'}")
        parts.append(f"Rating: {meta.rating:.2f}")
        parts.append(f"Size: {meta.size_human}")
    elif item.page_count:
        parts.append(f"Pages: {item.page_count}")
    summary = " | ".join(parts)

    links = [
        FeedLink(
            rel=REL_THUMB,
            href=builder.href(f"/image/{item.gid}/{item.token}/thumb"),
            type=MIME_THUMB,
        ),
        FeedLink(
            rel=REL_ACQUISITION,
            href=_entry_href(builder, item.gid, item.token),
            type=MIME_ACQ,
        ),
    ]
    # PSE stream link on the list entry itself: clients that register chapters
    # directly from the gallery feed (e.g. Kasane) need streamHref here.
    if page_count:
        links.append(
            FeedLink(
                rel=REL_STREAM,
                href=builder.href(f"/stream/{item.gid}/{item.token}/page/{{pageNumber}}"),
                type="image/jpeg",
                count=page_count,
            )
        )

    return FeedEntry(
        id=f"urn:ehentai:gallery:{item.gid}:{item.token}",
        title=title,
        updated=updated,
        author=author,
        category_term=category,
        category_label=category,
        summary=summary,
        links=links,
    )


@router.get("", response_class=Response)
async def root_feed(request: Request):
    builder = _builder(request)
    settings = request.app.state.settings
    has_auth = bool(settings.ipb_member_id and settings.ipb_pass_hash)
    nav = [
        (title, href, summary)
        for title, href, summary, key in _ROOT_NAV_BASE
        if not (key in ("watched", "favorites") and not has_auth)
    ]
    return Response(
        content=builder.root_feed(nav),
        media_type=MIME_NAV,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/search.xml", response_class=Response)
async def open_search(request: Request):
    builder = _builder(request)
    return Response(
        content=builder.open_search("/opds/v1.2/gallery"),
        media_type=MIME_OPEN_SEARCH,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/gallery", response_class=Response)
async def gallery_feed(
    request: Request,
    query: str = "",
    next: int | None = None,  # lastGid pagination (from rel="next" href)
):
    service = _service(request)
    builder = _builder(request)

    try:
        if query == "popular":
            info = await service.popular_galleries(last_gid=next)
        elif query == "watched":
            info = await service.watched_galleries(last_gid=next)
        elif query == "favorites":
            info = await service.favorites_galleries(last_gid=next)
        else:
            info = await service.search_galleries(query=query, last_gid=next)
    except Exception as exc:  # mapped to proper statuses by app-level handlers
        logger.warning("gallery feed upstream error: %s", exc)
        raise

    metas = await service.get_metadatas([(g.gid, g.token) for g in info.galleries])
    meta_by_gid = {m.gid: m for m in metas}

    entries = [
        _gallery_entry(builder, item, meta_by_gid.get(item.gid))
        for item in info.galleries
    ]

    next_href = None
    if info.next_gid:
        q = f"&query={quote(query)}" if query else ""
        next_href = builder.href(f"/opds/v1.2/gallery?next={info.next_gid}{q}")

    title = _LIST_TITLES.get(query, "Latest") if query else "Latest"
    title = f"E-Hentai: {title}"

    content = builder.gallery_feed(
        query=query,
        entries=entries,
        updated=_iso(),
        next_href=next_href,
        feed_id=query or "latest",
        title=title,
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/toplist", response_class=Response)
async def toplist_feed(
    request: Request,
    period: str = "yesterday",
    page: int = 1,
):
    """Ranklist acquisition feed. `period` ∈ yesterday|month|year|alltime;
    pagination uses `page` (1-based, `?p=` upstream) — not the lastGid
    `next` mechanism used by the front-page feeds. Pure standard Atom, no
    extensions (v1.2 constraint).
    """
    if period not in _TOPLIST_PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown period {period!r} (expected {sorted(_TOPLIST_PERIODS)})",
        )

    service = _service(request)
    builder = _builder(request)

    try:
        info = await service.toplist_galleries(period=period, page=page)
    except Exception as exc:  # mapped to proper statuses by app-level handlers
        logger.warning("toplist feed upstream error: %s", exc)
        raise

    metas = await service.get_metadatas(
        [(g.gid, g.token) for g in info.galleries]
    )
    meta_by_gid = {m.gid: m for m in metas}

    entries = [
        _gallery_entry(builder, item, meta_by_gid.get(item.gid))
        for item in info.galleries
    ]

    next_href = None
    if info.next_page:
        next_href = builder.href(
            f"/opds/v1.2/toplist?period={period}&page={info.next_page}"
        )

    title = f"E-Hentai: Toplist {_TOPLIST_PERIODS.get(period, period)}"
    content = builder.gallery_feed(
        query=f"toplist:{period}",
        entries=entries,
        updated=_iso(),
        next_href=next_href,
        feed_id=f"toplist:{period}",
        title=title,
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/gallery/{gid}/{token}/chapters", response_class=Response)
async def chapter_feed(request: Request, gid: int, token: str):
    service = _service(request)
    builder = _builder(request)

    meta = await service.get_metadata(gid, token)
    if meta is None:
        raise HTTPException(status_code=404, detail="Gallery not found")

    summary_parts = [
        f"Language: {meta.language}",
        f"Pages: {meta.filecount}",
        f"Uploader: {meta.uploader or 'unknown'}",
        f"Rating: {meta.rating:.2f}",
        f"Category: {meta.category}",
        f"Size: {meta.size_human}",
    ]
    content = builder.chapter_feed(
        gid=gid,
        token=token,
        title=meta.title,
        updated=_iso(meta.posted),
        author=meta.uploader,
        category_term=meta.category,
        category_label=meta.category,
        summary=" | ".join(summary_parts),
        filecount=meta.filecount,
        thumb_href=f"/image/{gid}/{token}/thumb",
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )
