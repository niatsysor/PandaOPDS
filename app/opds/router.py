"""OPDS routes: root nav feed, OpenSearch, gallery feeds, chapter feeds."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import GalleryListItem
from ..eh.parser import parse_publish_time_iso
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


def _updated_from_publish(publish_time: str) -> str:
    """ISO `updated` from a publish-time string; fall back to now."""
    if publish_time:
        iso = parse_publish_time_iso(publish_time)
        if iso:
            return iso
    return _iso()


def _item_updated(item: GalleryListItem) -> str:
    """`updated` from the list page's publish time; fall back to now."""
    return _updated_from_publish(item.publish_time)


def _gallery_entry(
    builder: FeedBuilder,
    item: GalleryListItem,
) -> FeedEntry:
    """A list entry rendered purely from list-page HTML data (no gdata).

    Browsing feeds must not call the ehapi: title/category/page-count come
    from the parsed list row. Full metadata is only fetched when the client
    opens the detail feed (/chapters).
    """
    clean_title, authors = parse_title_authors(item.title, item.category)
    # v1.2 <author> element supports a single name; join multiple authors.
    author = ", ".join(authors) if authors else ""
    page_count = item.page_count

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
        updated=_item_updated(item),
        author=author,
        category_term=item.category,
        category_label=item.category,
        summary="",
        links=links,
    )


@router.get("", response_class=Response)
async def root_feed(request: Request):
    """Root OPDS 1.2 navigation feed.

    Flattens the home TOML config (all sections) into a single navigation
    list.  v1.2 has no ``groups[]`` — everything is a plain navigation link.
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
    for s in home.sections:
        if _visible(s.type, s.query):
            href = build_href(type=s.type, query=s.query, base="/opds/v1.2")
            nav.append((s.title, href, s.title))

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

    entries = [_gallery_entry(builder, item) for item in info.galleries]

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

    entries = [_gallery_entry(builder, item) for item in info.galleries]

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
    """Detail feed: rendered from the detail-page HTML (zero gdata).

    Fetching the detail page here also pre-warms the page-URL mapping cache,
    so the first /stream request after opening a gallery skips one upstream
    round trip (fast reader entry).
    """
    service = _service(request)
    builder = _builder(request)

    detail = await service.get_detail_page(gid, token, 0)
    clean_title, authors = parse_title_authors(detail.title, detail.category)
    author = ", ".join(authors) if authors else ""
    content = builder.chapter_feed(
        gid=gid,
        token=token,
        title=clean_title,
        updated=_updated_from_publish(detail.publish_time),
        author=author,
        category_term=detail.category,
        category_label=detail.category,
        summary="",
        filecount=detail.image_count,
        thumb_href=f"/image/{gid}/{token}/thumb",
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )
