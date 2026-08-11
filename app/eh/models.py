"""Typed models for E-Hentai entities.

Field names mirror the JHenTai reference (`example/JHenTai`) so the mapping
between the reference and this implementation stays obvious.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
class GalleryTag:
    namespace: str
    key: str

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
        tags = self.tags.get("language") or []
        for tag in tags:
            if tag.key != "translated":
                return tag.key
        return "Japanese"


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
    is_expunged: bool = False


@dataclass
class GalleryPageInfo:
    """Parsed list page: entries + pagination info."""

    galleries: list[GalleryListItem] = field(default_factory=list)
    next_gid: int | None = None
    prev_gid: int | None = None
    total_count: int | None = None


@dataclass
class DetailPageInfo:
    """Parsed detail page: thumbnail URLs + page range info."""

    image_no_from: int  # 0-based inclusive
    image_no_to: int    # 0-based inclusive
    image_count: int
    current_page_no: int
    page_count: int  # number of thumbnail pages (ceil(filecount/20))
    thumbnails: list[GalleryThumbnail] = field(default_factory=list)


@dataclass
class ImagePageInfo:
    """Parsed /s/ image page."""

    image_url: str
    width: float | None = None
    height: float | None = None
    reload_key: str | None = None
    is_509: bool = False
