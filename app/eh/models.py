"""Typed models for E-Hentai entities.

Field names mirror the JHenTai reference (`example/JHenTai`) so the mapping
between the reference and this implementation stays obvious.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .languages import map_language


@dataclass(frozen=True)
class GalleryUrl:
    """A gallery identified by gid + token (and which site hosts it)."""

    gid: int
    token: str
    is_eh: bool = True

    @property
    def id_str(self) -> str:
        return f"{self.gid}:{self.token}"


@dataclass
class TagStyle:
    """Inline style of a featured (voted-up) tag, from the upstream HTML.

    Values are passed through verbatim (minus `!important`); empty keys are
    omitted when serialized to OPDS.
    """

    color: str = ""          # e.g. #f1f1f1
    border_color: str = ""   # e.g. #048751
    background: str = ""     # e.g. radial-gradient(#048751,#24A771)

    def as_dict(self) -> dict:
        out: dict = {}
        if self.color:
            out["color"] = self.color
        if self.border_color:
            out["borderColor"] = self.border_color
        if self.background:
            out["background"] = self.background
        return out


# HTML classes on tag divs: gt=confidence, gtl=skepticism, gtw=incorrect
TAG_STATUS_CONFIDENCE = "confidence"
TAG_STATUS_SKEPTICISM = "skepticism"
TAG_STATUS_INCORRECT = "incorrect"

_TAG_STATUS_BY_CLASS = {
    "gt": TAG_STATUS_CONFIDENCE,
    "gtl": TAG_STATUS_SKEPTICISM,
    "gtw": TAG_STATUS_INCORRECT,
}


def tag_status_from_class(class_name: str) -> str:
    return _TAG_STATUS_BY_CLASS.get(class_name, TAG_STATUS_CONFIDENCE)


@dataclass
class GalleryTag:
    namespace: str
    key: str
    status: str = TAG_STATUS_CONFIDENCE  # confidence | skepticism | incorrect
    style: TagStyle | None = None  # featured-tag inline style (if parsed)

    def __str__(self) -> str:
        return f"{self.namespace}:{self.key}"


@dataclass
class GalleryImage:
    """An image URL plus optional geometry (used for covers, thumbs, pages)."""

    url: str
    height: float | None = None
    width: float | None = None
    reload_key: str | None = None  # nl() reloadKey for failed image loads


@dataclass
class GalleryThumbnail:
    """One entry in a detail page's #gdt thumbnail block."""

    href: str  # /s/{imageToken}/{gid}-{pageNo} or /mpv/... URL
    thumb_url: str
    page_no: int | None  # 1-based page number, derived from href when possible
    is_large: bool = False
    origin_image_hash: str | None = None  # 40-char orghash from new structure
    width: float = 0.0
    height: float = 0.0


@dataclass
class GalleryMetadata:
    """Result of the gdata API (`gmetadata` entry)."""

    gid: int
    token: str
    title: str
    title_jpn: str
    category: str
    thumb: str
    rating: float
    tags: dict[str, list[GalleryTag]]
    filecount: int
    filesize: int
    posted: int  # unix seconds
    uploader: str
    torrentcount: int
    expunged: bool

    @property
    def language(self) -> str:
        """First language tag mapped to BCP 47 (RFC 5646); "" when none maps.

        Marker pseudo-tags and unknown keys are dropped — the raw tag text
        stays in the detail document's `subject`.
        """
        for tag in self.tags.get("language") or []:
            mapped = map_language(tag.key)
            if mapped:
                return mapped
        return ""

    @property
    def size_human(self) -> str:
        """Human-readable file size (JHenTai byte2String): B/KB/MB/GB."""
        size = float(self.filesize)
        if size < 1024:
            return f"{int(size)}B"
        size /= 1024
        if size < 1024:
            return f"{size:.2f}KB"
        size /= 1024
        if size < 1024:
            return f"{size:.2f}MB"
        size /= 1024
        return f"{size:.2f}GB"


@dataclass
class GalleryComment:
    """One gallery comment parsed from the detail page's `#cdiv` block.

    Field names mirror the JHenTai reference (`GalleryComment`); only the
    display-relevant subset is kept — interactive flags (fromMe/votedUp/
    votedDown) and score details are deliberately dropped (MVP). Content is
    preserved as raw HTML (JHenTai keeps the Element; clients render it).
    """

    id: int
    username: str
    user_id: int | None = None  # from the forums showuser= link
    time: str = ""  # site-local "yyyy-MM-dd HH:mm" (JHenTai-aligned)
    last_edit_time: str = ""  # from .c8 > strong (empty when unedited)
    content_html: str = ""  # raw HTML of .c6 (client renders)


@dataclass
class GalleryListItem:
    """A gallery entry parsed from a list page (gid/token/title/cover/category)."""

    gid: int
    token: str
    title: str
    category: str
    cover_url: str
    page_count: int | None = None
    rating: float = 0.0
    publish_time: str = ""
    language: str = ""  # from list-page tags (empty when layout lacks tags)
    is_expunged: bool = False
    # Tags parsed from the list page (layout dependent: compact/extended carry
    # the full set, thumbnail only featured tags, minimal none). Featured tags
    # carry `style`; non-featured tags have style=None.
    tags: list[GalleryTag] = field(default_factory=list)


@dataclass
class GalleryPageInfo:
    """Parsed list page: entries + pagination info.

    Two pagination styles coexist: `next_gid` (front-page `next=` lastGid
    pagination) and `next_page` (`.ptt` page-number pagination, used by
    ranklist/toplist pages which have no `#unext`). Only one is set for a
    given page.
    """

    galleries: list[GalleryListItem] = field(default_factory=list)
    next_gid: int | None = None
    prev_gid: int | None = None
    total_count: int | None = None
    next_page: int | None = None  # .ptt page-number pagination (toplist)


@dataclass
class DetailPageInfo:
    """Parsed detail page: thumbnail URLs + page range info.

    Also carries the gallery metadata scraped from the same HTML page
    (#gn/#gj/#gdd/#gdn/#grt2...) so the detail OPDS document needs no gdata:
    the detail page is already fetched by /stream anyway (1 req serves 20).
    """

    image_no_from: int  # 0-based inclusive
    image_no_to: int    # 0-based inclusive
    image_count: int
    current_page_no: int
    page_count: int  # number of thumbnail pages (ceil(filecount/20))
    thumbnails: list[GalleryThumbnail] = field(default_factory=list)
    # Full tag list from the #taglist block (all tags, with status + style).
    tags: list[GalleryTag] = field(default_factory=list)
    # --- metadata scraped from the same page (JHenTai detailPage2Gallery...) ---
    title: str = ""          # #gn
    title_jpn: str = ""      # #gj (empty when no Japanese title)
    category: str = ""       # #gdc > .cs
    cover_url: str = ""      # #gd1 > div style url(...)
    rating: float = 0.0      # #rating_image.ir sprite
    uploader: str = ""       # #gdn > a
    publish_time: str = ""   # #gdd Posted row
    language: str = ""       # #gdd Language row (mapped to BCP 47, RFC 5646)
    filesize_text: str = ""  # #gdd File Size row (e.g. "189.3 MiB")
    torrent_count: int = 0   # #gd5 torrent link count
    expunged: bool = False   # any #gdd value contains "Expunged"
    # Comments from the #cdiv block (latest batch visible on this page).
    comments: list[GalleryComment] = field(default_factory=list)


@dataclass
class ImagePageInfo:
    """Parsed /s/ image page."""

    image_url: str
    width: float | None = None
    height: float | None = None
    reload_key: str | None = None
    is_509: bool = False
