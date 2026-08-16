"""HTML / JSON parsers for E-Hentai pages.

Both the old (pre-2024-10-15) and new (datatags=1) detail-page thumbnail
structures are supported.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from lxml import html

from .exceptions import ParseError
from .languages import map_language
from .models import (
    DetailPageInfo,
    GalleryComment,
    GalleryListItem,
    GalleryMetadata,
    GalleryPageInfo,
    GalleryTag,
    GalleryThumbnail,
    GalleryUrl,
    ImagePageInfo,
    TAG_STATUS_CONFIDENCE,
    TAG_STATUS_INCORRECT,
    TAG_STATUS_SKEPTICISM,
    TagStyle,
    tag_status_from_class,
)

# /g/{gid}/{token}/  (also matches /mpv/{gid}/{token}/)
_GALLERY_HREF_RE = re.compile(r"/g/(\d+)/([0-9a-fA-F]+)/")
# /s/{imageToken}/{gid}-{pageNo}
_S_PAGE_HREF_RE = re.compile(r"/s/([^/]+)/(\d+)-(\d+)$")
_MPV_HREF_RE = re.compile(r"/mpv/(\d+)/([0-9a-fA-F]+)/")
_URL_RE = re.compile(r'url\(["\']?(.+?)["\']?\)')
_SHOWING_RE = re.compile(r"Showing\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+images")
_509_URLS = {
    "https://ehgt.org/g/509.gif",
    "https://exhentai.org/img/509.gif",
}
# Comment posted-time line, e.g. "Posted on 12 August 2026, 13:11 by: user"
_COMMENT_POSTED_RE = re.compile(r"Posted\s+on\s+(.+)")
_COMMENT_SHOWUSER_RE = re.compile(r"showuser=(\d+)")

# --- Tag status filter (global strategy) ---
# E-Hentai tags carry a community-trust class (gt=confidence, gtl=skepticism,
# gtw=incorrect). The configured level decides which statuses are considered
# reliable enough to enter subject/mytags; the status itself is never
# transmitted to clients (see AGENTS.md "字段分层约定").
_TAG_STATUS_KEEP: dict[str, frozenset[str]] = {
    "strict": frozenset({TAG_STATUS_CONFIDENCE}),
    "balanced": frozenset({TAG_STATUS_CONFIDENCE, TAG_STATUS_SKEPTICISM}),
    "off": frozenset({TAG_STATUS_CONFIDENCE, TAG_STATUS_SKEPTICISM, TAG_STATUS_INCORRECT}),
}


def apply_status_filter(tags: list[GalleryTag], level: str = "balanced") -> list[GalleryTag]:
    """Drop tags below the configured community-trust level.

    ``strict`` keeps confidence only; ``balanced`` (default) also keeps
    skepticism; ``off`` keeps everything. Unknown levels fall back to
    ``balanced``. Applied uniformly to list feeds and detail documents so
    subject stays a subset relationship across both.
    """
    keep = _TAG_STATUS_KEEP.get(level, _TAG_STATUS_KEEP["balanced"])
    return [t for t in tags if t.status in keep]

# CSS containers for the four list views
# tr-based selectors are descendant-style: real pages have no <tbody> and lxml
# does not insert one.
_LIST_CONTAINERS = [
    ".itg.gld > div",       # Thumbnail (wrapper divs directly in table)
    ".itg.gltc tr",         # Compact
    ".itg.glte tr",         # Extended
    ".itg.gltm tr",         # Minimal
]
_LIST_VIEWS = ["thumbnail", "compact", "extended", "minimal"]
# Cover selectors per view, tried in order (data-src preferred, src fallback)
_COVER_SELECTORS = [
    ".gl3t > a > img",          # Thumbnail
    ".gl2c > .glthumb > div > img",  # Compact
    ".gl1e > div > a > img",    # Extended
    ".gl2m > .glthumb > div > img",  # Minimal
]
# Page-count text selectors per view
_PAGECOUNT_SELECTORS = [
    ".gl5t > div:nth-child(1) > div",  # Thumbnail
    ".gl4c.glhide > div",              # Compact
    ".gl3e > div",                     # Extended
]
# Index of the "N pages" div within each page-count selector's matches:
# thumbnail/compact use the 2nd div, extended the 5th (.gl3e children:
# category/date/rating/uploader/N pages/torrents).
_PAGECOUNT_INDEX = {"thumbnail": 1, "compact": 1, "extended": 4}

# Publish-time text selectors per view: the element
# carrying the posted timestamp inside each gallery row. Format on real pages:
# `2026-08-12 13:11` (site-local wall clock).
_PUBLISH_SELECTORS = {
    "thumbnail": ".gl5t > div > div[id]",
    "compact": ".gl2c > div:nth-child(2) > [id]",
    "extended": ".gl3e > div[id]",
    "minimal": ".gl2m > div:nth-child(2)",
}

# Recognised publish-time formats; the first match wins.
_PUBLISH_DT_FORMATS = ("%Y-%m-%d %H:%M", "%d %B %Y, %H:%M", "%d %b %Y, %H:%M")


def _el(root: Any, css: str) -> Any | None:
    try:
        return root.cssselect(css)
    except Exception:
        return []


def _first(root: Any, css: str) -> Any | None:
    found = _el(root, css)
    return found[0] if found else None


def _text(el: Any | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _attr(el: Any | None, name: str, default: str = "") -> str:
    if el is None:
        return default
    value = el.get(name)
    return value.strip() if value else default


def parse_gallery_href(href: str) -> GalleryUrl | None:
    """Parse /g/{gid}/{token}/ hrefs."""
    m = _GALLERY_HREF_RE.search(href or "")
    if not m:
        return None
    return GalleryUrl(gid=int(m.group(1)), token=m.group(2))


def _parse_tag_style(style: str) -> TagStyle | None:
    """Extract color/border-color/background from an inline style string.

    Featured (voted-up) tags carry e.g.
      `color:#f1f1f1;border-color:#048751;background:radial-gradient(#048751,#24A771) !important`
    Values are passed through verbatim minus `!important`; a style with none of
    the three keys yields None (plain tag).
    """
    if not style:
        return None
    st = TagStyle()
    m = re.search(r"color:\s*([^;]+)", style)
    if m:
        st.color = m.group(1).strip()
    m = re.search(r"border-color:\s*([^;]+)", style)
    if m:
        st.border_color = m.group(1).strip()
    m = re.search(r"background:\s*([^;]+)", style)
    if m:
        st.background = m.group(1).strip().removesuffix(" !important").strip()
    if not (st.color or st.border_color or st.background):
        return None
    return st


def parse_publish_time_iso(text: str) -> str:
    """Parse a list-page publish-time string into an ISO-8601 UTC timestamp.

    Real pages render ``2026-08-12 13:11`` (site-local wall clock); treated as
    UTC for stability so the same gallery always yields the same ``updated``
    across requests. Returns "" when the format is unrecognised.
    """
    if not text:
        return ""
    for fmt in _PUBLISH_DT_FORMATS:
        try:
            dt = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def _parse_list_rating(row: Any) -> float:
    """Parse the 0-5 star rating from the `.ir` sprite background-position.

    Rating sprite math: x offset -16px per star, y offset -21px marks a
    half star. e.g. ``background-position:-32px -21px`` → 2.5.
    """
    style = _attr(_first(row, ".ir"), "style")
    if not style:
        return 0.0
    offsets = re.findall(r"-?\d+px", style)
    if len(offsets) < 2:
        return 0.0
    try:
        x = int(offsets[0].removesuffix("px"))
        y = int(offsets[1].removesuffix("px"))
    except ValueError:
        return 0.0
    rating = 5 - (-x) / 16 - (0.5 if y == -21 else 0)
    return round(max(0.0, rating), 2)


def _parse_list_publish_time(row: Any, view: str) -> str:
    """Publish-time text from the row's posted element (layout dependent)."""
    sel = _PUBLISH_SELECTORS.get(view)
    if not sel:
        return ""
    return _text(_first(row, sel))


def _list_language(tags: list[GalleryTag]) -> str:
    """First language tag mapped to BCP 47 (RFC 5646); "" when none maps.

    Marker pseudo-tags (translated/rewrite/raw) and unknown keys are
    dropped — the raw tag text stays in the detail document's `subject`.
    Mirrors GalleryMetadata.language (same map, same fallback).
    """
    for t in tags:
        if t.namespace == "language":
            mapped = map_language(t.key)
            if mapped:
                return mapped
    return ""


def _parse_list_tags(row: Any, view: str) -> list[GalleryTag]:
    """Parse tag divs from one list row.

    Tag divs carry `title="namespace:key"` and class `gt`/`gtl`/`gtw`; featured
    (voted-up) tags additionally carry an inline style. Layout dependent:
    compact/extended show the full tag set, thumbnail shows only featured tags,
    minimal shows none (verified on the real compact and extended fixtures).
    """
    if view == "compact":
        divs = _el(row, "div.gt[title], div.gtl[title], div.gtw[title]")
    elif view == "extended":
        # selector minus <tbody> (real pages have none; lxml adds none). The
        # tag table sits in the 2nd div of .gl4e.glname (1st is .glink);
        # verified against tests/fixtures/list_page_extended.html.
        divs = _el(
            row,
            ".gl2e > div > a > div > div:nth-child(2) > table > tr > td > div[title]",
        )
    else:
        return []
    out: list[GalleryTag] = []
    for div in divs:
        title = _attr(div, "title")
        if not title:
            continue
        parts = title.split(":", 1)
        namespace = parts[0].strip() if len(parts) == 2 and parts[0].strip() else "temp"
        key = parts[1].strip() if len(parts) == 2 else title.strip()
        out.append(
            GalleryTag(
                namespace=namespace,
                key=key,
                status=tag_status_from_class(_attr(div, "class")),
                style=_parse_tag_style(_attr(div, "style")),
            )
        )
    return out


def _detail_gdd_map(doc: Any) -> dict[str, str]:
    """Label → value map from the `#gdd` metadata table.

    Matches by the `.gdt1` label text instead of fixed row indices: real pages
    vary (Posted/Parent/Visible/Language/File Size/Length/...), expunged
    galleries drop rows, and categories add/remove Artist rows.
    """
    out: dict[str, str] = {}
    for tr in _el(doc, "#gdd table tr"):
        label = _text(_first(tr, ".gdt1")).strip().rstrip(":")
        value = _text(_first(tr, ".gdt2")).strip()
        if label:
            out[label] = value
    return out


_SIZE_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _parse_size_text(text: str) -> int:
    """Parse a human file size ('189.3 MiB' / '12.34 MB' / '1.5 GB') → bytes.

    Returns 0 when the text is empty or unparsable.
    """
    m = re.match(r"\s*([\d.]+)\s*([KMGTP]?)[iI]?B", text or "", re.I)
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    unit = m.group(2).upper()
    return int(value * _SIZE_UNITS.get(unit, 1))


def _parse_torrent_count(doc: Any) -> int:
    """Torrent count from the #gd5 footer ('Torrent Download (0)')."""
    text = _text(_first(doc, "#gd5"))
    m = re.search(r"Torrent[^)]*\((\d+)\)", text or "")
    return int(m.group(1)) if m else 0


def _detail_language_key(raw: str) -> str:
    """Normalize the #gdd Language row value to an EH language key.

    The row carries the translated marker in both spacings — "Chinese \xa0TR"
    (real pages, non-breaking space) and "Chinese TR" (test HTML). ``re``
    treats \xa0 as whitespace in Python 3, so the marker suffix always
    matches: both → "chinese"; "Chinese (Simplified) TR" →
    "chinese (simplified)".
    """
    text = re.sub(r"\s+TR\s*$", "", raw or "", flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _parse_detail_metadata(doc: Any) -> dict:
    """Scrape gallery metadata from the detail page.

    The detail page already carries everything gdata offers (title, Japanese
    title, category, cover, rating, uploader, publish time, language, file
    size, torrents, expunged); parsing it means the detail OPDS document
    never needs the ehapi.
    """
    gdd = _detail_gdd_map(doc)
    cover_url = ""
    cover_style = _attr(_first(doc, "#gd1 > div"), "style")
    m = _URL_RE.search(cover_style)
    if m:
        cover_url = m.group(1)
    language = _detail_language_key(gdd.get("Language", ""))
    language = map_language(language) or ""
    return {
        "title": _text(_first(doc, "#gn")),
        "title_jpn": _text(_first(doc, "#gj")),
        "category": _text(_first(doc, "#gdc > .cs")) or _text(_first(doc, "#gdc .cs")),
        "cover_url": cover_url,
        "rating": _parse_list_rating(doc),  # #rating_image.ir shares the sprite
        "uploader": _text(_first(doc, "#gdn > a")),
        "publish_time": gdd.get("Posted", ""),
        "language": language,
        "filesize_text": gdd.get("File Size", ""),
        "torrent_count": _parse_torrent_count(doc),
        "expunged": any("Expunged" in v for v in gdd.values()),
    }


def _parse_detail_tags(root: Any) -> list[GalleryTag]:
    """Parse the #taglist block (full tag set, status + inline style).

    Verified structure on real pages:
      <div id="taglist"><table><tr>
        <td class="tc">parody:</td>
        <td><div id="td_parody:zenless_zone_zero" class="gtl" style="...">
              <a id="ta_parody:zenless_zone_zero" ...>zenless zone zero</a>
            </div></td></tr>...</table></div>
    id forms: `td_<namespace>:<key>` (underscores in key) or `td_<key>` (temp).
    """
    out: list[GalleryTag] = []
    for div in _el(root, "#taglist table tr > td:nth-child(2) > div[id]"):
        tag_id = _attr(div, "id")
        if not tag_id:
            continue
        parts = tag_id.split(":", 1)
        if len(parts) == 2:
            namespace = parts[0].removeprefix("td_").strip() or "temp"
            key = parts[1].replace("_", " ").strip()
        else:
            namespace = "temp"
            key = tag_id.removeprefix("td_").replace("_", " ").strip()
        if not key:
            continue
        out.append(
            GalleryTag(
                namespace=namespace,
                key=key,
                status=tag_status_from_class(_attr(div, "class")),
                style=_parse_tag_style(_attr(div, "style")),
            )
        )
    return out


def _parse_comment_time(text: str) -> str:
    """Parse the posted-time line into a site-local ``yyyy-MM-dd HH:mm`` string.

    Real pages render ``Posted on 12 August 2026, 13:11 by: user``; output is
    site-local wall-clock ``yyyy-MM-dd HH:mm``. Returns "" when the format is
    unrecognised so a malformed comment
    never breaks the whole detail document.
    """
    m = _COMMENT_POSTED_RE.search(text or "")
    if not m:
        return ""
    raw = m.group(1).split(" by:", 1)[0].strip()
    try:
        dt = datetime.strptime(raw, "%d %B %Y, %H:%M")
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def _parse_detail_comments(root: Any) -> list[GalleryComment]:
    """Parse the #cdiv comment block.

    Structure on real pages (one ``.c1`` per comment):
      <div id="cdiv" class="gm"><a name="c0"></a>
        <div class="c1">
          <div class="c2">
            <div class="c3">Posted on ... by: <a href=...>user</a> ...</div>
            <div class="c4 nosel">...</div>
          </div>
          <div class="c6" id="comment_0">HTML content<br><a href=...>...</a></div>
          <div class="c7" id="cvotes_0">...</div>
        </div>...
      </div>
    Content is preserved as raw HTML (clients render it). A missing #cdiv
    yields [].
    """
    out: list[GalleryComment] = []
    for el in _el(root, "#cdiv > .c1"):
        c3 = _first(el, ".c2 > .c3")
        c6 = _first(el, ".c6")

        cid = 0
        m = re.search(r"comment_(\d+)", _attr(c6, "id"))
        if m:
            cid = int(m.group(1))

        # username: first anchor in .c3 (the uploader/poster link)
        username = _text(_first(c3, "a"))
        # userId from the forums showuser= link (any later anchor)
        user_id: int | None = None
        for a in _el(c3, "a")[1:]:
            m = _COMMENT_SHOWUSER_RE.search(_attr(a, "href"))
            if m:
                user_id = int(m.group(1))
                break

        content_html = ""
        if c6 is not None:
            # keep the whole .c6 node (with its id attribute) so clients can
            # render it directly
            content_html = html.tostring(c6, encoding="unicode")

        out.append(
            GalleryComment(
                id=cid,
                username=username,
                user_id=user_id,
                time=_parse_comment_time(_text(c3)),
                last_edit_time=_text(_first(el, ".c8 > strong")),
                content_html=content_html,
            )
        )
    return out


def _parse_cover_url(img: Any | None) -> str:
    if img is None:
        return ""
    return _attr(img, "data-src") or _attr(img, "src")


def _parse_list_item(row: Any, view: str = "compact") -> GalleryListItem | None:
    """Extract a gallery entry from one list-page row (any of the 4 views)."""
    # First anchor whose href points at a gallery page.
    gallery_url: GalleryUrl | None = None
    for a in _el(row, "a[href]"):
        url = parse_gallery_href(_attr(a, "href"))
        if url is not None:
            gallery_url = url
            break
    if gallery_url is None:
        return None

    cover_url = ""
    for sel in _COVER_SELECTORS:
        cover_url = _parse_cover_url(_first(row, sel))
        if cover_url:
            break

    category = _text(_first(row, ".cs")) or _text(_first(row, ".cn")) or _text(
        _first(row, ".gl1m.glcat > div")
    )

    page_count: int | None = None
    for sel in _PAGECOUNT_SELECTORS:
        divs = _el(row, sel)
        idx = _PAGECOUNT_INDEX.get(view, 1)
        if len(divs) > idx:
            m = re.match(r"(\d+)", _text(divs[idx]))
            if m:
                page_count = int(m.group(1))
                break

    is_expunged = _first(row, ".glink s") is not None

    tags = _parse_list_tags(row, view)
    return GalleryListItem(
        gid=gallery_url.gid,
        token=gallery_url.token,
        title=_text(_first(row, ".glink")),
        category=category,
        cover_url=cover_url,
        page_count=page_count,
        rating=_parse_list_rating(row),
        publish_time=_parse_list_publish_time(row, view),
        language=_list_language(tags),
        is_expunged=is_expunged,
        tags=tags,
    )


def _parse_generic_galleries(doc: Any) -> list[GalleryListItem]:
    """Fallback: scan all gallery anchors, extract fields from the nearest row.

    Used when none of the four known list layouts match (structure drift).
    """
    out: list[GalleryListItem] = []
    seen: set[int] = set()
    for a in _el(doc, "a[href]"):
        url = parse_gallery_href(_attr(a, "href"))
        if url is None or url.gid in seen:
            continue
        # walk up to the nearest row container (tr for table layouts,
        # a div carrying gl-* classes for div layouts)
        row: Any = a
        for _ in range(8):
            parent = row.getparent()
            if parent is None:
                break
            row = parent
            cls = (row.get("class") or "") if hasattr(row, "get") else ""
            if row.tag in ("tr", "li"):
                break
            if row.tag == "div" and len(row) >= 2 and "gl" in cls:
                break

        title = _text(_first(row, ".glink")) or _text(a)
        category = (
            _text(_first(row, ".cn"))
            or _text(_first(row, ".cs"))
            or _text(_first(row, ".glcat > div"))
        )
        img = _first(row, "img")
        cover = _attr(img, "data-src") or _attr(img, "src")
        page_count: int | None = None
        m = re.search(r"(\d+)\s+pages?", _text(row))
        if m:
            page_count = int(m.group(1))
        seen.add(url.gid)
        out.append(
            GalleryListItem(
                gid=url.gid,
                token=url.token,
                title=title,
                category=category,
                cover_url=cover,
                page_count=page_count,
            )
        )
    return out


def parse_list_page(html_text: str) -> GalleryPageInfo:
    """Parse a list/search page (all four front-page views)."""
    doc = html.fromstring(html_text)

    galleries: list[GalleryListItem] = []
    for view, container in zip(_LIST_VIEWS, _LIST_CONTAINERS):
        rows = _el(doc, container)
        if not rows:
            continue
        for row in rows:
            # skip ad rows / header rows (only 1 child or contains <th>)
            if len(row) == 1 or _first(row, "th") is not None:
                continue
            item = _parse_list_item(row, view)
            if item is not None:
                galleries.append(item)
        break  # only the first matching view layout is used

    if not galleries:
        galleries = _parse_generic_galleries(doc)

    def _nav_gid(sel: str) -> int | None:
        href = _attr(_first(doc, sel), "href")
        m = re.search(r"(?:next|prev)=([\d-]+)", href or "")
        return int(m.group(1)) if m else None

    def _nav_page() -> int | None:
        """Ranklist/toplist pages use `.ptt` page-number pagination (`?p=`)
        and have no `#unext` lastGid link. The next page's existence comes
        from the last `<td>` of the `.ptt` row (the last page renders the
        disabled "Next ›" arrow as a plain `<td>` without a link).

        E-Hentai toplist `p` is 0-based: displayed page N maps to `p=N-1` and
        the "Next ›" link on displayed N points at `p=N`. The OPDS `page` API
        is 1-based (the displayed page number), so the next page is `p + 1`.
        """
        tr = _first(doc, ".ptt tr")
        if tr is None:
            return None
        tds = list(tr)  # direct children of the row are the <td>s
        if not tds:
            return None
        a = _first(tds[-1], "a")
        m = re.search(r"p(?:age)?=(\d+)", _attr(a, "href") or "")
        return int(m.group(1)) + 1 if m else None

    total_count: int | None = None
    search_text = _text(_first(doc, ".searchtext"))
    m = re.search(r"([\d,]+)\+?", search_text or "")
    if m and "hundreds" not in search_text and "thousands" not in search_text:
        total_count = int(m.group(1).replace(",", ""))

    next_gid = _nav_gid("#unext")
    return GalleryPageInfo(
        galleries=galleries,
        next_gid=next_gid,
        prev_gid=_nav_gid("#uprev"),
        total_count=total_count,
        # page-number pagination only when lastGid pagination is absent
        next_page=None if next_gid is not None else _nav_page(),
    )


# --------------------------------------------------------------------------
# Detail page (#gdt thumbnail block)
# --------------------------------------------------------------------------

def _parse_new_thumbnails(root: Any, site_host: str) -> list[GalleryThumbnail]:
    """New structure (datatags=1): #gdt has classes; children are <a href> + div[style]."""
    out: list[GalleryThumbnail] = []
    for a in _el(root, "a"):
        href = _attr(a, "href")
        div = _first(a, "div[style]")
        style = _attr(div, "style")
        m = _URL_RE.search(style)
        if not m:
            continue
        thumb_url = m.group(1)
        if thumb_url.startswith("/"):
            thumb_url = f"https://{site_host}{thumb_url}"

        origin_hash = _attr(div, "data-orghash") or None
        is_large = re.search(r"\)\s*-?\d+px", style) is None
        wm = re.search(r"width:\s*(\d+)px", style)
        hm = re.search(r"height:\s*(\d+)px", style)

        page_no: int | None = None
        sm = _S_PAGE_HREF_RE.search(href)
        if sm:
            page_no = int(sm.group(3))
        # MPV href (no page number inside) is rewritten to an /s/ URL after
        # page numbers are assigned in parse_detail_page.

        out.append(
            GalleryThumbnail(
                href=href,
                thumb_url=thumb_url,
                page_no=page_no,
                is_large=is_large,
                origin_image_hash=origin_hash,
                width=float(wm.group(1)) if wm else 0.0,
                height=float(hm.group(1)) if hm else 0.0,
            )
        )
    return out


def _parse_old_small_thumbnails(root: Any, site_host: str) -> list[GalleryThumbnail]:
    """Old small thumbs: #gdt > .gdtm, href in div > a, thumb in div[style] url()."""
    out: list[GalleryThumbnail] = []
    for el in _el(root, ".gdtm"):
        # styled div is a direct child of .gdtm (cssselect would match self first)
        div = next((c for c in el if getattr(c, "tag", None) == "div"), None)
        if div is None:
            continue
        href = _attr(_first(el, "div > a"), "href")
        style = _attr(div, "style")
        m = _URL_RE.search(style)
        if not m:
            continue
        thumb_url = m.group(1)
        if thumb_url.startswith("/"):
            thumb_url = f"https://{site_host}{thumb_url}"
        page_no = None
        sm = _S_PAGE_HREF_RE.search(href)
        if sm:
            page_no = int(sm.group(3))
        wm = re.search(r"width:\s*(\d+)px", style)
        hm = re.search(r"height:\s*(\d+)px", style)
        out.append(
            GalleryThumbnail(
                href=href,
                thumb_url=thumb_url,
                page_no=page_no,
                width=float(wm.group(1)) if wm else 0.0,
                height=float(hm.group(1)) if hm else 0.0,
            )
        )
    return out


def _parse_old_large_thumbnails(root: Any, site_host: str) -> list[GalleryThumbnail]:
    """Old large thumbs: #gdt > .gdtl, href in a, thumb in a > img src."""
    out: list[GalleryThumbnail] = []
    for el in _el(root, ".gdtl"):
        a = _first(el, "a")
        img = _first(el, "a > img")
        thumb_url = _attr(img, "src")
        if thumb_url.startswith("/"):
            thumb_url = f"https://{site_host}{thumb_url}"
        href = _attr(a, "href")
        page_no = None
        sm = _S_PAGE_HREF_RE.search(href)
        if sm:
            page_no = int(sm.group(3))
        out.append(
            GalleryThumbnail(href=href, thumb_url=thumb_url, page_no=page_no, is_large=True)
        )
    return out


def parse_detail_page(html_text: str, site_host: str, page_index: int = 0) -> DetailPageInfo:
    """Parse a gallery detail page (thumbnail page `page_index`, 0-based).

    `page_index` is used to assign 1-based page numbers when the href does not
    carry them (MPV links in the new structure).
    """
    doc = html.fromstring(html_text)
    gdt = _first(doc, "#gdt")
    if gdt is None:
        raise ParseError("detail page has no #gdt block", raw_html=html_text)

    classes = (gdt.get("class") or "").strip()
    if classes:
        thumbnails = _parse_new_thumbnails(gdt, site_host)
    else:
        small = _el(gdt, ".gdtm")
        if small:
            thumbnails = _parse_old_small_thumbnails(gdt, site_host)
        else:
            thumbnails = _parse_old_large_thumbnails(gdt, site_host)

    # Assign page numbers for any entry missing one (MPV/new-structure case)
    # and rewrite MPV hrefs to /s/ URLs using orghash[:10].
    for i, t in enumerate(thumbnails):
        if t.page_no is None:
            t.page_no = page_index * 20 + i + 1
        mpv = _MPV_HREF_RE.search(t.href)
        if mpv and t.origin_image_hash:
            t.href = f"/s/{t.origin_image_hash[:10]}/{mpv.group(1)}-{t.page_no}"

    # "Showing X - Y of Z images"
    image_no_from = image_no_to = image_count = 0
    m = _SHOWING_RE.search(_text(_first(doc, ".gtb > .gpc")))
    if m:
        image_no_from = int(m.group(1)) - 1
        image_no_to = int(m.group(2)) - 1
        image_count = int(m.group(3))

    # page count from .ptt pagination
    page_count = 0
    tr = _first(doc, ".ptt > tbody > tr")
    if tr is not None:
        tds = _el(tr, "td")
        if len(tds) >= 2:
            a = _first(tds[-2], "a")
            if a is not None and _text(a).isdigit():
                page_count = int(_text(a))

    current_page_no = 1
    cur = _first(doc, ".ptds > a")
    if cur is not None and _text(cur).isdigit():
        current_page_no = int(_text(cur))

    meta = _parse_detail_metadata(doc)
    return DetailPageInfo(
        image_no_from=image_no_from,
        image_no_to=image_no_to,
        image_count=image_count,
        current_page_no=current_page_no,
        page_count=page_count,
        thumbnails=thumbnails,
        tags=_parse_detail_tags(doc),
        title=meta["title"],
        title_jpn=meta["title_jpn"],
        category=meta["category"],
        cover_url=meta["cover_url"],
        rating=meta["rating"],
        uploader=meta["uploader"],
        publish_time=meta["publish_time"],
        language=meta["language"],
        filesize_text=meta["filesize_text"],
        torrent_count=meta["torrent_count"],
        expunged=meta["expunged"],
        comments=_parse_detail_comments(doc),
    )


# --------------------------------------------------------------------------
# Image page (/s/{imageToken}/{gid}-{pageNo})
# --------------------------------------------------------------------------

def parse_image_page(html_text: str) -> ImagePageInfo:
    """Parse an /s/ image page: #img src, 509 placeholder, nl() reload key."""
    doc = html.fromstring(html_text)
    img = _first(doc, "#img")
    if img is None:
        if _first(doc, "#pane_images") is not None:
            raise ParseError("unsupported image page style (#pane_images)")
        raise ParseError("image page has no #img element", raw_html=html_text)

    url = _attr(img, "src")
    if url in _509_URLS:
        return ImagePageInfo(image_url=url, is_509=True)

    width = height = None
    style = _attr(img, "style")
    wm = re.search(r"width:\s*(\d+)px", style)
    hm = re.search(r"height:\s*(\d+)px", style)
    if wm:
        width = float(wm.group(1))
    if hm:
        height = float(hm.group(1))

    reload_key: str | None = None
    loadfail = _first(doc, "#loadfail")
    if loadfail is not None:
        m = re.search(r"return\s+nl\('([^']*)'\)", _attr(loadfail, "onclick"))
        if m:
            reload_key = m.group(1)

    return ImagePageInfo(image_url=url, width=width, height=height, reload_key=reload_key)


# --------------------------------------------------------------------------
# gdata API
# --------------------------------------------------------------------------

def _parse_gdata_tags(raw_tags: list[str]) -> dict[str, list[GalleryTag]]:
    tags: dict[str, list[GalleryTag]] = {}
    for raw in raw_tags:
        parts = raw.split(":", 1)
        namespace = parts[0] if len(parts) == 2 and parts[0] else "temp"
        key = parts[1] if len(parts) == 2 else raw
        tags.setdefault(namespace, []).append(GalleryTag(namespace=namespace, key=key))
    return tags


def parse_gdata_response(body: str) -> list[GalleryMetadata]:
    """Parse the JSON body of a gdata API response into metadata entries."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError(f"gdata returned invalid JSON: {exc}", raw_html=body[:2000]) from exc

    gmetadata = data.get("gmetadata") if isinstance(data, dict) else None
    if not isinstance(gmetadata, list) or not gmetadata:
        raise ParseError("gdata returned no gmetadata", raw_html=body[:2000])

    out: list[GalleryMetadata] = []
    for item in gmetadata:
        if not isinstance(item, dict):
            continue
        if "error" in item:
            # gdata returns an error entry for missing/expunged galleries
            continue
        raw_tags = item.get("tags") or []
        out.append(
            GalleryMetadata(
                gid=int(item["gid"]),
                token=item["token"],
                title=item.get("title", ""),
                title_jpn=item.get("title_jpn", ""),
                category=item.get("category", ""),
                thumb=item.get("thumb", ""),
                rating=float(item.get("rating", 0) or 0),
                tags=_parse_gdata_tags(raw_tags),
                filecount=int(item.get("filecount", 0)),
                filesize=int(item.get("filesize", 0)),
                posted=int(item.get("posted", 0)),
                uploader=item.get("uploader", ""),
                torrentcount=int(item.get("torrentcount", 0)),
                expunged=bool(item.get("expunged", False)),
            )
        )
    return out
