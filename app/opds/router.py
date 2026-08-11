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

router = APIRouter(prefix="/opds", tags=["opds"])


def _service(request: Request) -> EHService:
    return request.app.state.service


def _builder(request: Request) -> FeedBuilder:
    return FeedBuilder(request.app.state.settings)


def _entry_href(builder: FeedBuilder, gid: int, token: str) -> str:
    return builder.href(f"/opds/gallery/{gid}/{token}/chapters")


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
    return Response(
        content=builder.root_feed(),
        media_type=MIME_NAV,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/search.xml", response_class=Response)
async def open_search(request: Request):
    builder = _builder(request)
    return Response(
        content=builder.open_search(),
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
        next_href = builder.href(f"/opds/gallery?next={info.next_gid}{q}")

    title = f"E-Hentai: {query}" if query else "E-Hentai: Latest"
    if query == "popular":
        title = "E-Hentai: Popular"

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
