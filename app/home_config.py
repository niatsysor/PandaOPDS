"""Home page configuration loaded from a TOML file.

Declarative layout: a single flat ``[[section]]`` array.  ``kind`` + ``root``
determine where each section lands in the OPDS 2.0 document.

``type`` ∈ {"preset", "search"}
  - preset: query is a built-in key (latest, popular, watched, favorites, toplist:*)
  - search: query is an arbitrary E-Hentai search expression

TOML example::

    [[section]]
    kind = "publication"
    title = "Trending"
    type = "preset"
    query = "toplist:yesterday"
    count = 20

    [[section]]
    kind = "navigation"
    title = "历史总榜"
    type = "preset"
    query = "toplist:alltime"

    [[section]]
    kind = "navigation"
    root = true
    title = "关注"
    type = "preset"
    query = "watched"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # backport for 3.9/3.10
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from .eh.models import GalleryPageInfo
    from .eh.service import EHService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Section:
    """A single section in the flat TOML array."""
    kind: str          # "publication" | "navigation"
    title: str
    type: str          # "preset" | "search"
    query: str
    count: int = 0     # publication count (kind="publication" only)
    root: bool = False # True → root navigation[]; False/absent → groups[]


@dataclass
class HomeConfig:
    sections: list[Section] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Preset dispatch
# ---------------------------------------------------------------------------

def _resolve_preset(query: str) -> tuple[str, str | None]:
    """Resolve a preset query to (method, arg)."""
    if query == "latest":
        return ("search", "")
    elif query == "popular":
        return ("popular", None)
    elif query == "watched":
        return ("watched", None)
    elif query == "favorites":
        return ("favorites", None)
    elif query.startswith("toplist:"):
        period = query.split(":", 1)[1]
        if period not in ("yesterday", "month", "year", "alltime"):
            raise ValueError(f"Unknown toplist period: {period!r}")
        return ("toplist", period)
    else:
        raise ValueError(f"Unknown preset: {query!r}")


async def fetch_section(service: EHService, section: Section) -> GalleryPageInfo:
    """Fetch a single section's list page from E-Hentai."""
    if section.type == "preset":
        method, arg = _resolve_preset(section.query)
        if method == "search":
            return await service.search_galleries(query=arg or "")
        elif method == "popular":
            return await service.popular_galleries()
        elif method == "watched":
            return await service.watched_galleries()
        elif method == "favorites":
            return await service.favorites_galleries()
        elif method == "toplist":
            return await service.toplist_galleries(period=arg or "yesterday")
        else:
            raise ValueError(f"Unknown preset method: {method!r}")
    elif section.type == "search":
        return await service.search_galleries(query=section.query)
    else:
        raise ValueError(f"Unknown section type: {section.type!r}")


def build_href(*, type: str, query: str, base: str = "/opds/v2.0") -> str:
    """Build the OPDS href for a (type, query) pair."""
    if type == "preset":
        method, arg = _resolve_preset(query)
        if method == "search":
            href = f"{base}/gallery"
            return href if not arg else f"{href}?query={arg}"
        elif method == "popular":
            return f"{base}/gallery?query=popular"
        elif method == "watched":
            return f"{base}/gallery?query=watched"
        elif method == "favorites":
            return f"{base}/gallery?query=favorites"
        elif method == "toplist":
            return f"{base}/toplist?period={arg}"
    elif type == "search":
        return f"{base}/gallery?query={quote(query)}"
    return f"{base}/gallery"


def is_auth_required(type: str, query: str) -> bool:
    """Return True if this section needs IPB authentication."""
    return type == "preset" and query in ("watched", "favorites")


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------

def parse_home_toml(path: Path) -> HomeConfig:
    """Parse a home.toml file into a HomeConfig."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    sections: list[Section] = []
    for item in raw.get("section", []):
        kind = item.get("kind", "publication")
        sections.append(Section(
            kind=kind,
            title=item.get("title", ""),
            type=item.get("type", "preset"),
            query=item.get("query", "latest"),
            count=item.get("count", 0),
            root=item.get("root", False),
        ))

    return HomeConfig(sections=sections)


# ---------------------------------------------------------------------------
# Default built-in config
# ---------------------------------------------------------------------------

DEFAULT_SECTIONS: list[Section] = [
    Section(kind="publication", title="昨日最佳",   type="preset", query="toplist:yesterday", count=5),
    Section(kind="navigation",  title="月度精选",   type="preset", query="toplist:month"),
    Section(kind="navigation",  title="年度佳作",   type="preset", query="toplist:year"),
    Section(kind="publication", title="本周热门",   type="preset", query="popular",          count=10),
    Section(kind="navigation",  title="最新上传",   type="preset", query="latest"),
    Section(kind="publication", title="中文同人",   type="search", query="language:chinese", count=15),
    Section(kind="navigation",  title="历史总榜",   type="preset", query="toplist:alltime",  root=True),
    Section(kind="navigation",  title="我的收藏",   type="preset", query="favorites",        root=True),
    Section(kind="navigation",  title="日文原版",   type="search", query="language:japanese",root=True),
]


def load_home_config(home_config_path: Path | None) -> HomeConfig:
    """Load home config from a TOML file, falling back to built-in defaults."""
    if home_config_path and home_config_path.exists():
        try:
            logger.info("Loading home config from %s", home_config_path)
            return parse_home_toml(home_config_path)
        except Exception as exc:
            logger.warning(
                "Failed to parse %s, falling back to defaults: %s",
                home_config_path, exc,
            )
    return HomeConfig(sections=list(DEFAULT_SECTIONS))
