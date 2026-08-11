"""HTML / JSON parsers for E-Hentai pages.

Selector logic mirrors `example/JHenTai/lib/src/utils/eh_spider_parser.dart`.
Both the old (pre-2024-10-15) and new (datatags=1) detail-page thumbnail
structures are supported.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lxml import html

from .exceptions import ParseError
from .models import (
    DetailPageInfo,
    GalleryListItem,
    GalleryMetadata,
    GalleryPageInfo,
    GalleryTag,
    GalleryThumbnail,
    GalleryUrl,
    ImagePageInfo,
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

# CSS containers for the four list views (JHenTai `_*GalleryPageDocument2...`)
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


def _parse_list_tags(row: Any, view: str) -> list[GalleryTag]:
    """Parse tag divs from one list row.

    Tag divs carry `title="namespace:key"` and class `gt`/`gtl`/`gtw`; featured
    (voted-up) tags additionally carry an inline style. Layout dependent:
    compact/extended show the full tag set, thumbnail shows only featured tags,
    minimal shows none. Aligns with JHenTai `_parseCompactGalleryTags` /
    `_parseExtendedGalleryTags` (verified on real compact pages).
    """
    if view == "compact":
        divs = _el(row, "div.gt[title], div.gtl[title], div.gtw[title]")
    elif view == "extended":
        # JHenTai selector minus <tbody> (real pages have none; lxml adds none)
        divs = _el(
            row,
            ".gl2e > div > a > div > div:nth-child(1) > table > tr > td > div[title]",
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
        if len(divs) >= 2:
            m = re.match(r"(\d+)", _text(divs[1]))
            if m:
                page_count = int(m.group(1))
                break

    is_expunged = _first(row, ".glink s") is not None

    return GalleryListItem(
        gid=gallery_url.gid,
        token=gallery_url.token,
        title=_text(_first(row, ".glink")),
        category=category,
        cover_url=cover_url,
        page_count=page_count,
        is_expunged=is_expunged,
        tags=_parse_list_tags(row, view),
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
        and have no `#unext` lastGid link. Aligns with JHenTai
        `_ranklistPageDocument2NextPageIndex`: next page number comes from the
        last `<td>` of the `.ptt` row.
        """
        tr = _first(doc, ".ptt tr")
        if tr is None:
            return None
        tds = list(tr)  # direct children of the row are the <td>s
        if not tds:
            return None
        a = _first(tds[-1], "a")
        m = re.search(r"p(?:age)?=(\d+)", _attr(a, "href") or "")
        return int(m.group(1)) if m else None

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

    return DetailPageInfo(
        image_no_from=image_no_from,
        image_no_to=image_no_to,
        image_count=image_count,
        current_page_no=current_page_no,
        page_count=page_count,
        thumbnails=thumbnails,
        tags=_parse_detail_tags(doc),
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
