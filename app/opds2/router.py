"""OPDS 2.0 routes: navigation document, OpenSearch, gallery feeds, single publication.

Versioned under /opds/v2.0 (JSON); the v1.2 Atom feeds live under /opds/v1.2.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import (
    DetailPageInfo,
    GalleryListItem,
    GalleryPageInfo,
    GalleryTag,
)
from ..eh.parser import _parse_size_text, apply_status_filter, parse_publish_time_iso
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


# Namespaces excluded from the *list* subject (already exposed as dedicated
# fields / parsed by the client): language (standalone field) and artist
# (the client derives the author from image filenames). Detail documents
# carry the full #taglist subject, so list subject stays a strict subset.
_LIST_SUBJECT_EXCLUDED = frozenset({"language", "artist"})


def _flatten_subjects(tags: list[GalleryTag], exclude: frozenset[str] = frozenset()) -> list[str]:
    """Flat deduped "ns:key" strings over the full tag set.

    List feeds pass ``_LIST_SUBJECT_EXCLUDED`` to drop fields already exposed
    elsewhere; detail documents pass nothing (complete #taglist).
    """
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t.namespace in exclude:
            continue
        s = str(t)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _sort_tags(tags: list[GalleryTag]) -> list[GalleryTag]:
    """Stable sort: highlighted tags (with style) first, matching EH web display."""
    return sorted(tags, key=lambda t: t.style is None)


def _mytags_payload(t: GalleryTag) -> dict:
    """extensions.mytags entry: minimal — namespace/key/style only.

    status is consumed server-side by the global filter (never transmitted);
    mytags is the list-feeds-only highlighted-tag subset clients use to look
    up highlight styles when merging the detail document.
    """
    item: dict = {"namespace": t.namespace, "key": t.key}
    if t.style:
        item["style"] = t.style.as_dict()
    return item


def _detail_extensions(detail: DetailPageInfo) -> dict:
    """Single private-extension bucket consumed by the first-party client.

    Scraped from the detail page (gdata-equivalent): rating, Japanese title,
    uploader, size, expunged, category. Tags never appear here: mytags is a
    list-feeds-only field (the client merges the list item's mytags into the
    detail view) and the full tag set lives in `subject`.
    """
    ext: dict = {}
    if detail.rating:
        ext["rating"] = detail.rating
    if detail.title_jpn:
        ext["titleJpn"] = detail.title_jpn
    if detail.uploader:
        ext["uploader"] = detail.uploader
    if detail.filesize_text:
        size = _parse_size_text(detail.filesize_text)
        if size:
            ext["sizeBytes"] = size
    if detail.expunged:
        ext["expunged"] = True
    if detail.category:
        ext["category"] = detail.category
    return ext


def _item_modified(item: GalleryListItem) -> str:
    """`modified` from the list page's publish time; else now."""
    return _detail_modified(item.publish_time)


def _detail_modified(publish_time: str) -> str:
    """ISO `modified` from a publish-time string; fall back to now."""
    if publish_time:
        iso = parse_publish_time_iso(publish_time)
        if iso:
            return iso
    return _iso()


def _publication(
    builder: Opds2Builder,
    item: GalleryListItem,
) -> dict:
    """One publication object rendered purely from list-page HTML data.

    Browsing feeds never call the ehapi; extensions carry only what the list
    page exposed (category, rating) plus the highlighted-tag subset
    ``mytags``. Full tags live in `subject` (minus fields exposed elsewhere:
    language/artist). The client opens the detail document for full metadata.
    """
    category = item.category
    modified = _item_modified(item)
    page_count = item.page_count
    language = item.language

    clean_title, authors = parse_title_authors(item.title, category)

    tags = apply_status_filter(list(item.tags), builder.settings.tag_status_filter)
    tags = _sort_tags(tags)
    subjects = _flatten_subjects(tags, _LIST_SUBJECT_EXCLUDED)
    ext: dict = {}
    if item.rating:
        ext["rating"] = item.rating
    if item.category:
        ext["category"] = item.category
    mytags = [t for t in tags if t.style]
    if mytags:
        ext["mytags"] = [_mytags_payload(t) for t in mytags]
    extensions = ext or None

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

    # Phase 2 — collect sections by group; preserve insertion order.
    grouped: dict[str, list[Section]] = {}   # group_id → sections
    ungrouped: list[Section] = []            # sections without group (in order)
    for s in sections:
        if s.group:
            grouped.setdefault(s.group, []).append(s)
        else:
            ungrouped.append(s)

    # Phase 3 — walk ungrouped sections in TOML order; emit named groups
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
                    pubs.append(_publication(builder, item))
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

    publications = [_publication(builder, item) for item in info.galleries]

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

    title = _LIST_TITLES.get(query, "Search") if query else "Latest"
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

    publications = [_publication(builder, item) for item in info.galleries]

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
    """Single-publication document rendered from the detail-page HTML.

    Fetching the detail page here pre-warms the page-URL mapping cache so the
    first /stream request after opening a gallery skips one upstream round
    trip (fast reader entry). Zero gdata.
    """
    service = _service(request)
    builder = _builder(request)

    detail = await service.get_detail_page(gid, token, 0)
    clean_title, authors = parse_title_authors(detail.title, detail.category)
    modified = _detail_modified(detail.publish_time)
    tags = apply_status_filter(list(detail.tags), builder.settings.tag_status_filter)
    tags = _sort_tags(tags)
    subjects = _flatten_subjects(tags)
    pub = builder.publication(
        gid=gid,
        token=token,
        title=clean_title,
        modified=modified,
        authors=authors if authors else None,
        language=detail.language or None,
        page_count=detail.image_count,
        published=modified,
        subjects=subjects,
        number_of_pages=detail.image_count,
        extensions=_detail_extensions(detail),
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
