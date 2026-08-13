"""Home page configuration loaded from a TOML file.

``[[group]]`` declares named groups (title, order).  ``[[section]]`` references
a group via ``group`` field — publication and navigation sections can co-exist
in the same group.

TOML example::

    [[group]]
    id = "rankings"
    title = "排行榜"

    [[section]]
    group = "rankings"
    kind = "publication"
    title = "昨日最佳"
    type = "preset"
    query = "toplist:yesterday"
    count = 5

    [[section]]
    group = "rankings"
    kind = "navigation"
    title = "月度精选"
    type = "preset"
    query = "toplist:month"

    [[section]]
    kind = "navigation"
    title = "关注"
    type = "preset"
    query = "watched"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # backport for 3.9/3.10

if TYPE_CHECKING:
    from .eh.models import GalleryPageInfo
    from .eh.service import EHService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GroupDef:
    """A named group declaration from TOML."""
    id: str
    title: str


@dataclass
class Section:
    """A single section in the flat ``[[section]]`` array."""
    kind: str          # "publication" | "navigation"
    title: str
    type: str          # "preset" | "search"
    query: str
    count: int = 0     # publication preview count (kind="publication" only);
                       # TOML omission → DEFAULT_PUBLICATION_PREVIEW_COUNT;
                       # explicit 0 → disabled (no fetch, no render)
    group: str = ""    # GroupDef id; "" → standalone pub or root nav


@dataclass
class HomeConfig:
    groups: list[GroupDef] = field(default_factory=list)
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

# A publication section that omits `count` previews this many galleries by
# default (instead of silently rendering nothing). Explicit `count = 0`
# disables the preview and skips the upstream list fetch.
DEFAULT_PUBLICATION_PREVIEW_COUNT = 10

def parse_home_toml(path: Path) -> HomeConfig:
    """Parse a home.toml file into a HomeConfig."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    groups: list[GroupDef] = []
    for item in raw.get("group", []):
        groups.append(GroupDef(
            id=item["id"],
            title=item.get("title", item["id"]),
        ))

    sections: list[Section] = []
    for item in raw.get("section", []):
        kind = item.get("kind", "publication")
        if "count" in item:
            count = item["count"]
        else:
            # Missing `count` on a publication section previews a default
            # number of galleries; only an explicit `count = 0` disables.
            count = (
                DEFAULT_PUBLICATION_PREVIEW_COUNT
                if kind == "publication"
                else 0
            )
        sections.append(Section(
            kind=kind,
            title=item.get("title", ""),
            type=item.get("type", "preset"),
            query=item.get("query", "latest"),
            count=count,
            group=item.get("group", ""),
        ))

    return HomeConfig(groups=groups, sections=sections)


# ---------------------------------------------------------------------------
# Default built-in config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = HomeConfig(
    groups=[
        GroupDef(id="rankings", title="排行榜"),
        GroupDef(id="browse", title="浏览"),
    ],
    sections=[
        Section(group="rankings", kind="publication", title="昨日最佳", type="preset", query="toplist:yesterday", count=5),
        Section(group="rankings", kind="navigation",  title="月度精选", type="preset", query="toplist:month"),
        Section(group="rankings", kind="navigation",  title="年度佳作", type="preset", query="toplist:year"),
        Section(group="browse",   kind="publication", title="本周热门", type="preset", query="popular",          count=10),
        Section(group="browse",   kind="navigation",  title="最新上传", type="preset", query="latest"),
        Section(                  kind="publication", title="中文同人", type="search", query="language:chinese", count=15),
        Section(                  kind="navigation",  title="历史总榜", type="preset", query="toplist:alltime"),
        Section(                  kind="navigation",  title="我的收藏", type="preset", query="favorites"),
        Section(                  kind="navigation",  title="日文原版", type="search", query="language:japanese"),
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
