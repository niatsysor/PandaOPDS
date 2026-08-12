"""Home page configuration loaded from a TOML file.

Declarative layout: users define ``[[group]]`` and ``[[navigation]]`` sections
with ``(type, query)`` pairs.  The server dispatches these to E-Hentai service
calls and builds OPDS 2.0 ``groups[]`` / ``navigation[]`` accordingly.

``type`` ∈ {"preset", "search"}
  - preset: query is a built-in key (latest, popular, watched, favorites, toplist:*)
  - search: query is an arbitrary E-Hentai search expression

Everything is flat — no nesting.  Order in the TOML equals order in the OPDS
output.  The client derives layout from position and publication count.

TOML example::

    [[group]]
    title = "昨日最佳"
    type = "preset"
    query = "toplist:yesterday"
    publications = 5

    [[group]]
    title = "月度精选"
    type = "preset"
    query = "toplist:month"
    publications = 10

    [[navigation]]
    title = "历史总榜"
    type = "preset"
    query = "toplist:alltime"
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
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
    """A single section — group (publications > 0) or nav link (publications == 0)."""
    title: str
    type: str          # "preset" | "search"
    query: str
    publications: int = 0   # 0 = navigation-only


@dataclass
class HomeConfig:
    groups: list[Section] = field(default_factory=list)
    navigation: list[Section] = field(default_factory=list)


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

    groups: list[Section] = []
    for item in raw.get("group", []):
        pubs = item.get("publications", 0)
        if not pubs:
            raise ValueError(
                f"Group {item.get('title', '?')!r} is missing required 'publications' field"
            )
        groups.append(Section(
            title=item["title"],
            type=item["type"],
            query=item["query"],
            publications=pubs,
        ))

    navigation: list[Section] = []
    for item in raw.get("navigation", []):
        navigation.append(Section(
            title=item["title"],
            type=item["type"],
            query=item["query"],
        ))

    return HomeConfig(groups=groups, navigation=navigation)


# ---------------------------------------------------------------------------
# Default built-in config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = HomeConfig(
    groups=[
        Section(title="昨日最佳",   type="preset",  query="toplist:yesterday",  publications=5),
        Section(title="月度精选",   type="preset",  query="toplist:month",      publications=10),
        Section(title="年度佳作",   type="preset",  query="toplist:year",       publications=10),
        Section(title="本周热门",   type="preset",  query="popular",            publications=10),
        Section(title="最新上传",   type="preset",  query="latest",             publications=20),
        Section(title="中文同人",   type="search",  query="language:chinese",   publications=15),
    ],
    navigation=[
        Section(title="历史总榜", type="preset", query="toplist:alltime"),
        Section(title="我的收藏", type="preset", query="favorites"),
        Section(title="日文原版", type="search", query="language:japanese"),
    ],
)


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
    return DEFAULT_CONFIG
