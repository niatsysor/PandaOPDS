"""OPDS routes: root nav feed, OpenSearch, gallery feeds, chapter feeds."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import GalleryListItem, GalleryMetadata
from ..eh.service import EHService
from ..eh.title_parser import parse_title_authors
from ..home_config import (
    build_href,
    is_auth_required,
    load_home_config,
)
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
    raw_title = meta.title if meta and meta.title else item.title
    category = meta.category if meta and meta.category else item.category
    updated = _iso(meta.posted) if meta and meta.posted else _iso()
    page_count = meta.filecount if meta and meta.filecount else item.page_count

    # Parse authors from title; use clean title as display title.
    if meta:
        clean_title, authors = parse_title_authors(raw_title, meta.category)
    else:
        clean_title = raw_title
        authors = []

    # v1.2 <author> element supports a single name; join multiple authors.
    author = ", ".join(authors) if authors else ""

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
        title=clean_title,
        updated=updated,
        author=author,
        category_term=category,
        category_label=category,
        summary="",
        links=links,
    )


@router.get("", response_class=Response)
async def root_feed(request: Request):
    """Root OPDS 1.2 navigation feed.

    Flattens the home TOML config (groups + navigation) into a single
    navigation list.  v1.2 has no ``groups[]`` — everything is a plain
    navigation link.
    """
    builder = _builder(request)
    settings = request.app.state.settings
    has_auth = bool(settings.ipb_member_id and settings.ipb_pass_hash)
    home = load_home_config(settings.home_config_path)

    def _visible(type: str, query: str) -> bool:
        if is_auth_required(type, query) and not has_auth:
            return False
        return True

    nav: list[tuple[str, str, str]] = []
    for g in home.groups:
        if _visible(g.type, g.query):
            href = build_href(type=g.type, query=g.query, base="/opds/v1.2")
            nav.append((g.title, href, g.title))
        for sub in g.navigation:
            if _visible(sub.type, sub.query):
                href = build_href(type=sub.type, query=sub.query, base="/opds/v1.2")
                nav.append((sub.title, href, sub.title))
    for n in home.navigation:
        if _visible(n.type, n.query):
            href = build_href(type=n.type, query=n.query, base="/opds/v1.2")
            nav.append((n.title, href, n.title))

    # Keep OpenSearch as the last nav entry (protocol-level, not in TOML).
    nav.append(("Search", "/opds/v1.2/search.xml", "Search E-Hentai galleries"))

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

    clean_title, authors = parse_title_authors(meta.title, meta.category)
    author = ", ".join(authors) if authors else ""
    content = builder.chapter_feed(
        gid=gid,
        token=token,
        title=clean_title,
        updated=_iso(meta.posted),
        author=author,
        category_term=meta.category,
        category_label=meta.category,
        summary="",
        filecount=meta.filecount,
        thumb_href=f"/image/{gid}/{token}/thumb",
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )
