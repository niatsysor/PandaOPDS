"""OPDS 2.0 routes: navigation document, OpenSearch, gallery feeds, single publication.

Versioned under /opds/v2.0 (JSON); the v1.2 Atom feeds live under /opds/v1.2.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.models import (
    DetailPageInfo,
    GalleryComment,
    GalleryListItem,
    GalleryPageInfo,
    GalleryTag,
)
from ..eh.parser import _parse_size_text, apply_status_filter, parse_publish_time_iso
from ..eh.service import EHService
from ..eh.title_parser import parse_detail_title, parse_title_authors
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
    MIME_PUBLICATION,
    REL_SUBSECTION,
    Opds2Builder,
    _iso,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opds/v2.0", tags=["opds2"])

# Gallery URLs inside comment HTML: https://(e-hentai|exhentai).org/g/{gid}/{token}/
# (optionally with ?p= / #anchors; /mpv/ viewer links map to the same detail doc).
# The trailing `[^"']*` eats any query/fragment so the rewritten href always
# points at the OPDS 2.0 detail document for that gallery.
_CONTENT_GALLERY_LINK_RE = re.compile(
    r'(href=["\'])(https?://(?:e-hentai|exhentai)\.org/'
    r'(?:g|mpv)/(\d+)/([0-9a-fA-F]+)/[^"\']*)(["\'])'
)


def _rewrite_gallery_links(content: str, href: Callable[[str], str]) -> str:
    """Rewrite E-Hentai gallery links inside comment HTML to OPDS links.

    Lets the first-party app open galleries referenced in comments in-app
    (``/opds/v2.0/gallery/{gid}/{token}``) instead of leaving the site. The
    anchor text is left untouched — clients may keep showing the original URL
    while navigating via the rewritten href. Non-gallery links (uploader
    pages, forums, external sites) are left verbatim.
    """

    def _repl(m: re.Match) -> str:
        gid, token = m.group(3), m.group(4)
        return f"{m.group(1)}{href(f'/opds/v2.0/gallery/{gid}/{token}')}{m.group(5)}"

    return _CONTENT_GALLERY_LINK_RE.sub(_repl, content)

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


def _comment_payload(c: GalleryComment, href: Callable[[str], str] | None = None) -> dict:
    """extensions.reviews entry: display-relevant subset only.

    Mirrors the JHenTai GalleryComment fields that matter for display:
    id/username/userId/time/lastEditTime/content (raw HTML). Interactive
    flags (fromMe/votedUp/votedDown) and score details are deliberately
    omitted (MVP); empty optional fields are dropped. When ``href`` is given
    (the feed's href() helper), gallery links inside the content are rewritten
    to OPDS 2.0 detail links for in-app navigation.
    """
    item: dict = {"id": c.id, "username": c.username, "time": c.time}
    if c.user_id is not None:
        item["userId"] = c.user_id
    if c.last_edit_time:
        item["lastEditTime"] = c.last_edit_time
    if c.content_html:
        content = c.content_html
        if href is not None:
            content = _rewrite_gallery_links(content, href)
        item["content"] = content
    return item


def _detail_extensions(
    detail: DetailPageInfo,
    comments_enabled: bool = True,
    href: Callable[[str], str] | None = None,
) -> dict:
    """Single private-extension bucket consumed by the first-party client.

    Scraped from the detail page (gdata-equivalent): rating, Japanese title,
    uploader, size, expunged, category, and — when enabled — the gallery
    comment block (``reviews``, raw HTML content with gallery links rewritten
    to OPDS detail links via ``href``). Tags never appear here: mytags is a
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
    if comments_enabled and detail.comments:
        ext["reviews"] = [_comment_payload(c, href) for c in detail.comments]
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
            if s.kind == "publication":
                if id(s) in fetched:
                    items = fetched[id(s)].galleries[: s.count]
                    for item in items:
                        pubs.append(_publication(builder, item))
                else:
                    # Fetch skipped (count=0 opt-out) or failed: surface the
                    # silent drop instead of vanishing without a trace.
                    logger.warning(
                        "publication section %r (group=%r) rendered no "
                        "preview: fetch skipped or failed",
                        s.title,
                        s.group,
                    )
            elif s.kind == "navigation":
                navs.append({
                    "title": s.title,
                    "href": builder.href(build_href(type=s.type, query=s.query)),
                    "rel": REL_SUBSECTION,
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
                    "href": builder.href(build_href(type=s.type, query=s.query)),
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
    # Period facets (OPDS 2.0): pick the ranklist period inside the feed;
    # the active period link carries "active": true.
    facets = [{
        "metadata": {"title": "Period"},
        "links": [
            {
                "href": builder.href(f"/opds/v2.0/toplist?period={p}"),
                "title": label,
                "active": p == period,
            }
            for p, label in _TOPLIST_PERIODS.items()
        ],
    }]
    content = builder.acquisition_document(
        title=title,
        identifier=f"urn:ehentai:toplist:{period}",
        publications=publications,
        self_href=f"/opds/v2.0/toplist?period={period}",
        next_href=next_href,
        facets=facets,
    )
    return Response(
        content=content,
        media_type=MIME_ACQ,
        headers={"Cache-Control": "public, max-age=300"},
    )


async def _detail_publication(
    service: EHService, builder: Opds2Builder, gid: int, token: str
) -> dict:
    """Fetch the detail page and render its single publication object.

    Shared by the acquisition detail document and the single-publication
    endpoint: both render from the same cached detail-page HTML (1h) and
    pre-warm the page-URL mapping, so the first /stream request after
    opening a gallery skips one upstream round trip. Zero gdata.
    """
    detail = await service.get_detail_page(gid, token, 0)
    clean_title, authors = parse_detail_title(
        detail.title, detail.title_jpn, detail.category
    )
    modified = _detail_modified(detail.publish_time)
    tags = apply_status_filter(list(detail.tags), builder.settings.tag_status_filter)
    tags = _sort_tags(tags)
    subjects = _flatten_subjects(tags)
    return builder.publication(
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
        extensions=_detail_extensions(
            detail, builder.settings.comments_enabled, builder.href
        ),
        detail_document=True,
    )


@router.get("/gallery/{gid}/{token}", response_class=Response)
async def gallery_detail(request: Request, gid: int, token: str):
    """Single-publication acquisition document rendered from the detail-page HTML.

    Fetching the detail page here pre-warms the page-URL mapping cache so the
    first /stream request after opening a gallery skips one upstream round
    trip (fast reader entry). Zero gdata.
    """
    service = _service(request)
    builder = _builder(request)

    pub = await _detail_publication(service, builder, gid, token)
    clean_title = pub["metadata"]["title"]
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


@router.get("/gallery/{gid}/{token}/publication", response_class=Response)
async def gallery_publication(request: Request, gid: int, token: str):
    """Single-publication document: a top-level RWPM/OPDS publication object.

    This is the target of every publication's `rel="self"` link. Clients
    like Stump follow `self` to open details and read through the embedded
    `readingOrder` (per-page image URLs); the response shape matches what
    their parser expects (a publication object, not an acquisition feed).
    """
    service = _service(request)
    builder = _builder(request)

    pub = await _detail_publication(service, builder, gid, token)
    return Response(
        content=builder.serialize(pub),
        media_type=MIME_PUBLICATION,
        headers={"Cache-Control": "public, max-age=300"},
    )
