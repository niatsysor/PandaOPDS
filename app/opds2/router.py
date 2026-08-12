"""OPDS 2.0 routes: navigation document, OpenSearch, gallery feeds, single publication.

Versioned under /opds/v2.0 (JSON); the v1.2 Atom feeds live under /opds/v1.2.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import GalleryListItem, GalleryMetadata, GalleryTag
from ..eh.service import EHService
from .feed import (
    MIME_ACQ,
    MIME_NAV,
    Opds2Builder,
    _iso,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opds/v2.0", tags=["opds2"])

# All known sections. Items listed in HOME_GROUPS appear as groups with inline
# publication previews on the root document; the rest (minus auth-gated ones
# when no cookie) appear as plain navigation entries.
_SECTIONS = [
    ("Latest", "/opds/v2.0/gallery", "Latest galleries", "latest"),
    ("Watched", "/opds/v2.0/gallery?query=watched", "Watched galleries", "watched"),
    ("Favorites", "/opds/v2.0/gallery?query=favorites", "Favorite galleries", "favorites"),
    ("Popular", "/opds/v2.0/gallery?query=popular", "Popular this week", "popular"),
    ("Toplist: Yesterday", "/opds/v2.0/toplist?period=yesterday", "Top galleries of the last 24 hours", "toplist:yesterday"),
    ("Toplist: Past Month", "/opds/v2.0/toplist?period=month", "Top galleries of the past month", "toplist:month"),
    ("Toplist: Past Year", "/opds/v2.0/toplist?period=year", "Top galleries of the past year", "toplist:year"),
    ("Toplist: All Time", "/opds/v2.0/toplist?period=alltime", "Top galleries of all time", "toplist:alltime"),
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


def _builder(request: Request) -> Opds2Builder:
    return Opds2Builder(request.app.state.settings)


def _all_tags(meta: GalleryMetadata) -> list[GalleryTag]:
    """Flatten the gdata tag groups (namespace -> [tags]) in order."""
    out: list[GalleryTag] = []
    for group in meta.tags.values():
        out.extend(group)
    return out


def _flatten_subjects(tags: list[GalleryTag]) -> list[str]:
    """Flat deduped "ns:key" strings for the standard `subjects` array."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        s = str(t)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _merge_tags(
    base: list[GalleryTag], overlay: list[GalleryTag]
) -> list[GalleryTag]:
    """Overlay list/detail-page tag status+style onto gdata tags.

    Matches by (namespace, key); gdata is the full set, the overlay carries
    featured-tag styles. New GalleryTag objects are built so cached metadata
    objects are never mutated.
    """
    if not overlay:
        return list(base)
    by_key = {(t.namespace, t.key): t for t in overlay}
    out: list[GalleryTag] = []
    for t in base:
        o = by_key.get((t.namespace, t.key))
        if o is not None and (o.status != "confidence" or o.style):
            t = GalleryTag(t.namespace, t.key, o.status, o.style)
        out.append(t)
    return out


def _tag_payload(t: GalleryTag) -> dict:
    """extensions.tags entry: keep it minimal (status/style only when set)."""
    item: dict = {"namespace": t.namespace, "key": t.key}
    if t.status != "confidence":
        item["status"] = t.status
    if t.style:
        item["style"] = t.style.as_dict()
    return item


def _extensions(meta: GalleryMetadata, tags: list[GalleryTag]) -> dict:
    """Single private-extension bucket consumed by the first-party client.

    Standard fields stay out: rating, Japanese title, category, size, expunged
    and the full tag payload (namespace/status/style) all live here.
    """
    ext: dict = {}
    if meta.rating:
        ext["rating"] = meta.rating
    if meta.title_jpn:
        ext["titleJpn"] = meta.title_jpn
    if meta.filesize:
        ext["sizeBytes"] = meta.filesize
    if meta.expunged:
        ext["expunged"] = True
    ext["category"] = meta.category
    if tags:
        ext["tags"] = [_tag_payload(t) for t in tags]
    return ext


def _summary(meta: GalleryMetadata | None, item: GalleryListItem) -> str:
    parts: list[str] = []
    if meta:
        parts.append(f"Language: {meta.language}")
        parts.append(f"Pages: {meta.filecount}")
        parts.append(f"Uploader: {meta.uploader or 'unknown'}")
        parts.append(f"Rating: {meta.rating:.2f}")
        parts.append(f"Size: {meta.size_human}")
    elif item.page_count:
        parts.append(f"Pages: {item.page_count}")
    return " | ".join(parts)


def _publication(
    builder: Opds2Builder,
    item: GalleryListItem,
    meta: GalleryMetadata | None,
) -> dict:
    title = meta.title if meta and meta.title else item.title
    modified = _iso(meta.posted) if meta and meta.posted else _iso()
    author = meta.uploader if meta else ""
    page_count = meta.filecount if meta and meta.filecount else item.page_count
    language = meta.language if meta else ""
    summary = _summary(meta, item)

    subjects: list[str] | None = None
    extensions: dict | None = None
    if meta:
        all_tags = _all_tags(meta)
        subjects = _flatten_subjects(all_tags)
        # overlay featured-tag styles parsed from the list page (layout
        # dependent: compact/extended full, thumbnail featured-only, minimal
        # none) onto the gdata tag set
        extensions = _extensions(meta, _merge_tags(all_tags, item.tags))

    return builder.publication(
        gid=item.gid,
        token=item.token,
        title=title,
        modified=modified,
        author=author,
        language=language,
        description=summary,
        page_count=page_count,
        published=modified,
        subjects=subjects,
        number_of_pages=page_count,
        extensions=extensions,
    )


async def _fetch_list_page(
    service: EHService,
    key: str,
) -> tuple[str, str, str, GalleryPageInfo] | None:
    """Fetch one list page. Returns (key, title, href, info) or None on failure."""
    try:
        if key == "latest":
            info = await service.search_galleries(query="")
            return (key, "Latest", "/opds/v2.0/gallery", info)
        elif key == "popular":
            info = await service.popular_galleries()
            return (key, "Popular", "/opds/v2.0/gallery?query=popular", info)
        elif key == "watched":
            info = await service.watched_galleries()
            return (key, "Watched", "/opds/v2.0/gallery?query=watched", info)
        elif key == "favorites":
            info = await service.favorites_galleries()
            return (key, "Favorites", "/opds/v2.0/gallery?query=favorites", info)
        elif key.startswith("toplist:"):
            period = key.split(":", 1)[1]
            info = await service.toplist_galleries(period=period)
            title = {"yesterday": "Toplist: Yesterday", "month": "Toplist: Past Month",
                     "year": "Toplist: Past Year", "alltime": "Toplist: All Time"}.get(period, key)
            return (key, title, f"/opds/v2.0/toplist?period={period}", info)
        else:
            logger.warning("unknown home group key %r", key)
            return None
    except Exception as exc:
        logger.warning("home group %s list error: %s", key, exc)
        return None


@router.get("", response_class=Response)
async def root_feed(request: Request):
    """Root OPDS 2.0 document.

    Sections listed in ``HOME_GROUPS`` are rendered as ``groups[]`` entries
    with inline publication previews (OPDS 2.0 §2.5).  The remaining sections
    appear in the standard ``navigation[]`` array.
    Watched / Favorites are auth-gated: omitted entirely when no IPB cookie
    is configured.

    Implementation note: list pages are fetched concurrently, then **all**
    gdata metadata is merged into a single batched ``get_metadatas()`` call
    so the API never exceeds the 4–5 request safe window.
    """
    service = _service(request)
    builder = _builder(request)
    settings = request.app.state.settings

    has_auth = bool(settings.ipb_member_id and settings.ipb_pass_hash)
    home_group_keys = set(settings.home_groups)

    # Partition sections: groups (inline previews) vs navigation (links only).
    nav_items: list[dict] = []
    list_tasks: list[asyncio.Task] = []

    for title, href, summary, key in _SECTIONS:
        if key in ("watched", "favorites") and not has_auth:
            continue
        if key in home_group_keys:
            list_tasks.append(
                asyncio.create_task(_fetch_list_page(service, key))
            )
        else:
            nav_items.append({"title": title, "href": href, "summary": summary})

    # Phase 1 — await all list pages concurrently.
    list_results: list[tuple[str, str, str, GalleryPageInfo]] = []
    if list_tasks:
        done, _pending = await asyncio.wait(list_tasks)
        for task in done:
            try:
                result = task.result()
                if result is not None:
                    list_results.append(result)
            except Exception as exc:
                logger.warning("home group list error: %s", exc)

    # Phase 2 — collect all unique gids, fetch metadata in ONE merged batch.
    # This is the critical defence: instead of N independent get_metadatas()
    # calls (N× batch fragmentation), we issue a single call → ≤ 4 API
    # requests total (100 gid / 25 per batch), staying within the EH API
    # safe window of 4–5 sequential requests.
    groups: list[dict] = []
    if list_results:
        all_gids: dict[tuple[int, str], int] = {}  # (gid,token) -> list idx
        group_data: list[tuple[str, str, str, list[GalleryListItem]]] = []
        for idx, (key, title, href, info) in enumerate(list_results):
            items = info.galleries[: settings.home_publications]
            group_data.append((key, title, href, items))
            for item in items:
                all_gids.setdefault((item.gid, item.token), idx)

        try:
            metas = await service.get_metadatas(list(all_gids.keys()))
        except Exception as exc:
            logger.warning("home metadata batch error: %s", exc)
            metas = []
        meta_by_gid = {m.gid: m for m in metas}

        # Phase 3 — build publication dicts and group dicts.
        for key, title, href, items in group_data:
            publications = [
                _publication(builder, item, meta_by_gid.get(item.gid))
                for item in items
            ]
            groups.append({
                "metadata": {
                    "title": title,
                    "identifier": f"urn:ehentai:group:{key}",
                    "modified": _iso(),
                },
                "links": [{
                    "rel": "self",
                    "href": builder.href(href),
                    "type": MIME_ACQ,
                    "title": title,
                }],
                "publications": publications,
            })

    content = builder.navigation_document(
        navigation=nav_items or None,
        groups=groups or None,
    )
    return Response(
        content=content,
        media_type=MIME_NAV,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/search.xml", response_class=Response)
async def open_search(request: Request):
    # OpenSearchDescription is version-agnostic XML; only the template differs.
    from ..opds.feed import FeedBuilder, MIME_OPEN_SEARCH

    content = FeedBuilder(request.app.state.settings).open_search(
        "/opds/v2.0/gallery"
    )
    return Response(
        content=content,
        media_type=MIME_OPEN_SEARCH,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/gallery", response_class=Response)
async def gallery_feed(
    request: Request,
    query: str = "",
    next: int | None = None,  # lastGid pagination (from rel="next" href)
    category: str = "",
):
    service = _service(request)
    builder = _builder(request)
    settings = request.app.state.settings

    # Resolve category name → f_cats exclude mask.
    f_cats: int | None = None
    if category:
        for name, mask in settings.facets:
            if category.lower() == name.lower():
                f_cats = mask
                break

    try:
        if query == "popular":
            info = await service.popular_galleries(last_gid=next)
        elif query == "watched":
            info = await service.watched_galleries(last_gid=next)
        elif query == "favorites":
            info = await service.favorites_galleries(last_gid=next)
        else:
            info = await service.search_galleries(query=query, last_gid=next, f_cats=f_cats)
    except Exception as exc:  # mapped to proper statuses by app-level handlers
        logger.warning("gallery feed upstream error: %s", exc)
        raise

    metas = await service.get_metadatas([(g.gid, g.token) for g in info.galleries])
    meta_by_gid = {m.gid: m for m in metas}

    publications = [
        _publication(builder, item, meta_by_gid.get(item.gid))
        for item in info.galleries
    ]

    next_href = None
    if info.next_gid:
        q_parts = []
        if query:
            q_parts.append(f"query={quote(query)}")
        if category:
            q_parts.append(f"category={quote(category)}")
        next_href = builder.href(
            f"/opds/v2.0/gallery?next={info.next_gid}"
            + ("&" + "&".join(q_parts) if q_parts else "")
        )

    title = _LIST_TITLES.get(query, "Latest") if query else "Latest"
    if category:
        title = f"{title} — {category}"
    title = f"E-Hentai: {title}"

    # Only emit facets for the main search feed (not popular/watched/favorites
    # which use different upstream URLs that may not support f_cats).
    facets = None
    if query not in ("popular", "watched", "favorites"):
        facets = builder.build_category_facets(current_category=category)

    content = builder.acquisition_document(
        title=title,
        identifier=f"urn:ehentai:gallery-list:{query or 'latest'}",
        publications=publications,
        self_href="/opds/v2.0/gallery",
        next_href=next_href,
        facets=facets,
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
    """Ranklist acquisition document. `period` ∈ yesterday|month|year|alltime;
    pagination uses `page` (1-based, `?p=` upstream) — not the lastGid
    `next` mechanism used by the front-page feeds.
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

    publications = [
        _publication(builder, item, meta_by_gid.get(item.gid))
        for item in info.galleries
    ]

    next_href = None
    if info.next_page:
        next_href = builder.href(
            f"/opds/v2.0/toplist?period={period}&page={info.next_page}"
        )

    title = f"E-Hentai: Toplist {_TOPLIST_PERIODS.get(period, period)}"
    content = builder.acquisition_document(
        title=title,
        identifier=f"urn:ehentai:toplist:{period}",
        publications=publications,
        self_href=f"/opds/v2.0/toplist?period={period}",
        next_href=next_href,
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/gallery/{gid}/{token}", response_class=Response)
async def gallery_detail(request: Request, gid: int, token: str):
    service = _service(request)
    builder = _builder(request)

    meta = await service.get_metadata(gid, token)
    if meta is None:
        raise HTTPException(status_code=404, detail="Gallery not found")

    # Full tag set with featured styles/status comes from the detail page
    # #taglist block (cached 1h; a cold miss costs one throttled HTML request).
    # Degrade gracefully to gdata tags when the detail page is unavailable.
    try:
        detail = await service.get_detail_page(gid, token, 0)
        detail_tags = detail.tags
    except Exception as exc:  # network / parse errors: fall back to gdata
        logger.warning("detail page unavailable for tags: %s", exc)
        detail_tags = []

    all_tags = _all_tags(meta)
    subjects = _flatten_subjects(all_tags)
    merged = _merge_tags(all_tags, detail_tags) if detail_tags else all_tags

    summary_parts = [
        f"Language: {meta.language}",
        f"Pages: {meta.filecount}",
        f"Uploader: {meta.uploader or 'unknown'}",
        f"Rating: {meta.rating:.2f}",
        f"Category: {meta.category}",
        f"Size: {meta.size_human}",
    ]
    pub = builder.publication(
        gid=gid,
        token=token,
        title=meta.title,
        modified=_iso(meta.posted),
        author=meta.uploader,
        language=meta.language,
        description=" | ".join(summary_parts),
        page_count=meta.filecount,
        published=_iso(meta.posted),
        subjects=subjects,
        number_of_pages=meta.filecount,
        extensions=_extensions(meta, merged),
    )
    content = builder.acquisition_document(
        title=meta.title,
        identifier=f"urn:ehentai:gallery:{gid}:{token}",
        publications=[pub],
        self_href=f"/opds/v2.0/gallery/{gid}/{token}",
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )
