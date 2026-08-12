"""OPDS 2.0 routes: navigation document, OpenSearch, gallery feeds, single publication.

Versioned under /opds/v2.0 (JSON); the v1.2 Atom feeds live under /opds/v1.2.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import GalleryListItem, GalleryMetadata, GalleryPageInfo, GalleryTag
from ..eh.service import EHService
from ..eh.title_parser import parse_title_authors
from ..home_config import (
    Section,
    build_href,
    fetch_section,
    is_auth_required,
    load_home_config,
)
from .feed import (
    MIME_ACQ,
    MIME_NAV,
    Opds2Builder,
    _iso,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opds/v2.0", tags=["opds2"])

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


# Namespaces that should appear in the standard `subject` array.
# These are the content-rating / target-audience dimensions that generic
# OPDS clients can meaningfully consume.  All other tags are available in
# full detail inside `extensions.tags`.
_SUBJECT_NAMESPACES = {"female", "male", "mixed", "other", "parody"}


def _flatten_subjects(tags: list[GalleryTag]) -> list[str]:
    """Flat deduped "ns:key" strings for gender/audience tags only."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t.namespace not in _SUBJECT_NAMESPACES:
            continue
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


def _sort_tags(tags: list[GalleryTag]) -> list[GalleryTag]:
    """Stable sort: highlighted tags (with style) first, matching EH web display."""
    return sorted(tags, key=lambda t: t.style is None)


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

    Standard fields stay out: rating, original title, Japanese title,
    uploader, category, size, expunged and the full tag payload
    (namespace/status/style) all live here.
    """
    ext: dict = {}
    if meta.rating:
        ext["rating"] = meta.rating
    ext["originalTitle"] = meta.title
    if meta.title_jpn:
        ext["titleJpn"] = meta.title_jpn
    ext["uploader"] = meta.uploader or ""
    if meta.filesize:
        ext["sizeBytes"] = meta.filesize
    if meta.expunged:
        ext["expunged"] = True
    ext["category"] = meta.category
    if tags:
        ext["tags"] = [_tag_payload(t) for t in tags]
    return ext


def _publication(
    builder: Opds2Builder,
    item: GalleryListItem,
    meta: GalleryMetadata | None,
) -> dict:
    raw_title = meta.title if meta and meta.title else item.title
    modified = _iso(meta.posted) if meta and meta.posted else _iso()
    page_count = meta.filecount if meta and meta.filecount else item.page_count
    language = meta.language if meta else ""

    # Parse authors from title; use the clean title as the display title.
    if meta:
        clean_title, authors = parse_title_authors(raw_title, meta.category)
    else:
        clean_title = raw_title
        authors = []

    subjects: list[str] | None = None
    extensions: dict | None = None
    if meta:
        all_tags = _all_tags(meta)
        # overlay featured-tag styles parsed from the list page (layout
        # dependent: compact/extended full, thumbnail featured-only, minimal
        # none) onto the gdata tag set
        merged = _sort_tags(_merge_tags(all_tags, item.tags))
        subjects = _flatten_subjects(merged)
        extensions = _extensions(meta, merged)

    return builder.publication(
        gid=item.gid,
        token=item.token,
        title=clean_title,
        modified=modified,
        authors=authors if authors else None,
        language=language,
        page_count=page_count,
        published=modified,
        subjects=subjects,
        number_of_pages=page_count,
        extensions=extensions,
    )


@router.get("", response_class=Response)
async def root_feed(request: Request):
    """Root OPDS 2.0 document.

    Layout is driven by a TOML config: ``[[group]]`` declares named groups;
    ``[[section]]`` references a group via ``group`` field.  Publication and
    navigation sections can co-exist in the same group.

    Sections without a ``group``:
    * ``kind="publication"`` → standalone ``groups[]`` entry
    * ``kind="navigation"``  → root ``navigation[]`` entry

    Watched / Favorites are auth-gated: omitted when no IPB cookie is set.
    """
    service = _service(request)
    builder = _builder(request)
    settings = request.app.state.settings

    has_auth = bool(settings.ipb_member_id and settings.ipb_pass_hash)
    home = load_home_config(settings.home_config_path)

    def _visible(s: Section) -> bool:
        if is_auth_required(s.type, s.query) and not has_auth:
            return False
        return True

    # Collect all visible sections; group definitions always visible.
    sections = [s for s in home.sections if _visible(s)]
    group_defs: dict[str, str] = {g.id: g.title for g in home.groups}

    # Phase 1 — concurrently fetch list pages for publication sections.
    pub_sections = [s for s in sections if s.kind == "publication" and s.count > 0]
    results = await asyncio.gather(
        *[fetch_section(service, s) for s in pub_sections],
        return_exceptions=True,
    )
    fetched: dict[int, GalleryPageInfo] = {}
    for section, result in zip(pub_sections, results):
        if isinstance(result, Exception):
            logger.warning("section %r list error: %s", section.title, result)
        else:
            fetched[id(section)] = result

    # Phase 2 — batch gdata metadata.
    meta_by_gid: dict[int, GalleryMetadata] = {}
    if fetched:
        all_gids: dict[tuple[int, str], None] = {}
        for section in pub_sections:
            if id(section) in fetched:
                for item in fetched[id(section)].galleries[: section.count]:
                    all_gids.setdefault((item.gid, item.token))
        if all_gids:
            try:
                metas = await service.get_metadatas(list(all_gids))
            except Exception as exc:
                logger.warning("home metadata batch error: %s", exc)
                metas = []
            meta_by_gid = {m.gid: m for m in metas}

    # Phase 3 — collect sections by group; preserve insertion order.
    grouped: dict[str, list[Section]] = {}   # group_id → sections
    ungrouped: list[Section] = []            # sections without group (in order)
    for s in sections:
        if s.group:
            grouped.setdefault(s.group, []).append(s)
        else:
            ungrouped.append(s)

    # Phase 4 — walk ungrouped sections in TOML order; emit named groups
    # at the position of their first referencing section (we approximate
    # by emitting them before ungrouped when they share a position, which
    # is fine since named groups are declared separately).
    #
    # Strategy: walk sections, emit each unique group on first encounter.
    groups_out: list[dict] = []
    root_nav: list[dict] = []
    emitted_groups: set[str] = set()

    def _emit_group(gid: str, grp_sections: list[Section]) -> dict:
        """Build an OPDS group dict from a list of sections."""
        title = group_defs.get(gid, grp_sections[0].title)
        first = grp_sections[0]
        g: dict = {
            "metadata": {
                "title": title,
                "identifier": f"urn:ehentai:group:{gid}",
                "modified": _iso(),
            },
            "links": [{
                "rel": "self",
                "href": builder.href(build_href(type=first.type, query=first.query)),
                "type": MIME_ACQ,
                "title": title,
            }],
        }
        pubs: list[dict] = []
        navs: list[dict] = []
        for s in grp_sections:
            if s.kind == "publication" and id(s) in fetched:
                items = fetched[id(s)].galleries[: s.count]
                for item in items:
                    pubs.append(_publication(builder, item, meta_by_gid.get(item.gid)))
            elif s.kind == "navigation":
                navs.append({
                    "title": s.title,
                    "href": builder.href(build_href(type=s.type, query=s.query)),
                    "type": MIME_ACQ,
                })
        if pubs:
            g["publications"] = pubs
        if navs:
            g["navigation"] = navs
        return g

    for s in sections:
        if s.group:
            if s.group not in emitted_groups:
                groups_out.append(_emit_group(s.group, grouped[s.group]))
                emitted_groups.add(s.group)
        else:
            if s.kind == "publication":
                groups_out.append(_emit_group(f"__s_{id(s)}", [s]))
            else:
                root_nav.append({
                    "title": s.title,
                    "href": build_href(type=s.type, query=s.query),
                    "summary": s.title,
                })

    content = builder.navigation_document(
        navigation=root_nav or None,
        groups=groups_out or None,
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
    merged = _sort_tags(_merge_tags(all_tags, detail_tags) if detail_tags else all_tags)
    subjects = _flatten_subjects(merged)

    clean_title, authors = parse_title_authors(meta.title, meta.category)
    pub = builder.publication(
        gid=gid,
        token=token,
        title=clean_title,
        modified=_iso(meta.posted),
        authors=authors if authors else None,
        language=meta.language,
        page_count=meta.filecount,
        published=_iso(meta.posted),
        subjects=subjects,
        number_of_pages=meta.filecount,
        extensions=_extensions(meta, merged),
    )
    content = builder.acquisition_document(
        title=clean_title,
        identifier=f"urn:ehentai:gallery:{gid}:{token}",
        publications=[pub],
        self_href=f"/opds/v2.0/gallery/{gid}/{token}",
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )
