"""Application configuration loaded from environment variables.

See AGENTS.md "鉴权 Cookie（环境变量注入）" for the meaning of each variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Allow a .env file at the project root for local development.
load_dotenv()

EH_SITE_EHENTAI = "e-hentai"
EH_SITE_EXHENTAI = "exhentai"

_SITE_HOSTS = {
    EH_SITE_EHENTAI: "e-hentai.org",
    EH_SITE_EXHENTAI: "exhentai.org",
}


class ConfigError(RuntimeError):
    """Raised when the configuration is invalid (e.g. missing required cookies)."""


@dataclass(frozen=True)
class Settings:
    # --- E-Hentai identity ---
    ipb_member_id: str = ""
    ipb_pass_hash: str = ""
    # Optional session seed: exhentai normally sets igneous itself when the
    # paired IPB session authenticates (see EHClient.establish_session); a
    # user-provided value is only used as an initial seed, never required.
    igneous: str = ""
    eh_site: str = EH_SITE_EHENTAI  # "e-hentai" | "exhentai"

    # --- Site behaviour flags (cookies) ---
    nw: str = "1"          # bypass "Offensive For Everyone" warning
    datatags: str = "1"    # enable new thumbnail structure (data-orghash)

    # --- HTTP ---
    timeout_seconds: float = 6.0      # JHenTai-style default timeout
    retries: int = 3                  # network-error retries
    html_interval_seconds: float = 0.3  # min delay between HTML page requests
    max_concurrency: int = 5          # global outbound concurrency (EH API: 4-5 safe)

    # --- URLs ---
    public_base_url: str = ""  # when set, feeds emit absolute URLs

    # --- PSE page numbering ---
    # OPDS-PSE spec says pages are 0-based, but LANraragi (the de-facto PSE
    # server reference) and clients built against it (Kasane) use 1-based.
    # Default 1 for client compatibility; set PSE_PAGE_BASE=0 for spec-strict.
    pse_page_base: int = 1

    # --- Cache ---
    cache_dir: Path = field(default_factory=lambda: Path("./cache"))
    cache_max_gb: float = 4.0
    image_cache_enabled: bool = True
    metadata_ttl_seconds: int = 3600
    page_url_ttl_seconds: int = 3600
    list_cache_ttl_seconds: int = 600  # list-page parse results (search/popular/toplist...)

    # --- List page display mode ---
    # E-Hentai list pages support four views (Thumbnail / Extended / Compact /
    # Minimal). Compact and Extended carry the full tag set; Thumbnail shows
    # only featured tags; Minimal shows none. The server forces this layout on
    # every list request via ``inline_set`` so it always receives the richest
    # parseable content, regardless of the user's web browser default.
    # Accepted values: "extended" (default), "compact", "minimal", "thumbnail".
    # Mapped to inline_set keys: dm_e / dm_c / dm_m / dm_t.
    list_layout: str = "extended"

    # --- uconfig profile isolation ---
    # Optional: name of a dedicated E-Hentai settings profile created on
    # uconfig.php. When set, the service creates (once) and switches to this
    # profile during ``establish_session``, isolating the service's uconfig
    # preferences (layout, language, exclusions) from the user's browser
    # profile. Leave empty to only use per-request ``inline_set`` overrides.
    eh_profile: str = ""

    # --- Home navigation (v2.0 server-driven layout) ---
    # Sections rendered as ``groups[]`` on the root OPDS 2.0 document, each
    # carrying an inline ``publications[]`` preview (OPDS 2.0 §2.5). Keys:
    # ``latest``, ``popular``, ``watched``, ``favorites``,
    # ``toplist:yesterday``, ``toplist:month``, ``toplist:year``,
    # ``toplist:alltime``. Sections not listed here become plain
    # ``navigation[]`` links (except Watched/Favorites, which are omitted
    # entirely when no IPB cookie is configured).
    home_groups: list[str] = field(
        default_factory=lambda: ["latest", "popular", "toplist:yesterday", "toplist:month"]
    )
    # How many publications each home group embeds. E-Hentai pages hold 25
    # items (list) / 20 items (detail thumbs), so a value ≤ 25 costs one
    # upstream page per group.
    home_publications: int = 20

    # --- OPDS 2.0 facets (category filter) ---
    # Customisable facet entries: each is (display_name, f_cats_exclude_mask).
    # f_cats is E-Hentai's exclude bitmask (bit 0=1=Misc, bit 1=2=Doujinshi,
    # bit 2=4=Manga, bit 3=8=Artist CG, bit 4=16=Game CG, bit 5=32=Western,
    # bit 6=64=Non-H, bit 7=128=Image Set, bit 8=256=Cosplay, bit 9=512=Asian
    # Porn). To show ONLY Doujinshi, exclude all others: f_cats=1+4+8+16+32+
    # 64+128+256+512=1021. Users may define custom names with arbitrary masks
    # (including category combinations) via the FACETS env var.
    # Format: FACETS=Name1:mask1,Name2:mask2,...
    facets: list[tuple[str, int]] = field(
        default_factory=lambda: [
            ("Doujinshi", 1021),
            ("Manga", 1019),
            ("Artist CG", 1015),
            ("Game CG", 1007),
            ("Western", 991),
            ("Non-H", 959),
            ("Image Set", 895),
            ("Cosplay", 767),
            ("Asian Porn", 511),
            ("Misc", 1022),
        ]
    )

    # --- derived ---
    @property
    def site_host(self) -> str:
        return _SITE_HOSTS.get(self.eh_site, _SITE_HOSTS[EH_SITE_EHENTAI])

    @property
    def is_exhentai(self) -> bool:
        return self.eh_site == EH_SITE_EXHENTAI

    @property
    def http_origin(self) -> str:
        """Scheme+host for HTML pages and the API endpoint."""
        return f"https://{self.site_host}"

    @property
    def api_url(self) -> str:
        """gdata API endpoint. exhentai has no `api.` subdomain — its API lives
        at exhentai.org/api.php (see AGENTS.md 端点表)."""
        if self.is_exhentai:
            return f"https://{self.site_host}/api.php"
        return f"https://api.{self.site_host}/api.php"

    @property
    def cookies(self) -> dict[str, str]:
        c: dict[str, str] = {
            "nw": self.nw,
            "datatags": self.datatags,
        }
        if self.ipb_member_id:
            c["ipb_member_id"] = self.ipb_member_id
        if self.ipb_pass_hash:
            c["ipb_pass_hash"] = self.ipb_pass_hash
        if self.igneous and self.igneous.lower() != "mystery":
            c["igneous"] = self.igneous
        return c

    @property
    def ehentai_host(self) -> str:
        """The other site's host (used for 509.gif detection on both sites)."""
        return _SITE_HOSTS[EH_SITE_EHENTAI] if self.is_exhentai else _SITE_HOSTS[EH_SITE_EXHENTAI]

    # --- validation ---
    @property
    def inline_set_key(self) -> str:
        """The ``inline_set`` query parameter value matching ``list_layout``."""
        _map = {"extended": "dm_e", "compact": "dm_c", "minimal": "dm_m", "thumbnail": "dm_t"}
        return _map.get(self.list_layout, "dm_e")

    def validate(self) -> None:
        if self.eh_site not in _SITE_HOSTS:
            raise ConfigError(
                f"EH_SITE must be one of {list(_SITE_HOSTS)}, got {self.eh_site!r}"
            )
        if self.list_layout not in ("extended", "compact", "minimal", "thumbnail"):
            raise ConfigError(
                f"LIST_LAYOUT must be one of extended/compact/minimal/thumbnail, "
                f"got {self.list_layout!r}"
            )
        # IPB cookies are optional: without them the server still serves public
        # content (Latest/Popular/Toplist/Search) and simply omits the auth-only
        # nav items (Watched/Favorites).


def load_settings() -> Settings:
    def _gb(value: str | None, default: float) -> float:
        try:
            return float(value) if value else default
        except ValueError:
            return default

    def _int(value: str | None, default: int) -> int:
        try:
            return int(value) if value else default
        except ValueError:
            return default

    def _float(value: str | None, default: float) -> float:
        try:
            return float(value) if value else default
        except ValueError:
            return default

    def _bool(value: str | None, default: bool) -> bool:
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _home_groups(value: str | None) -> list[str]:
        """Parse HOME_GROUPS: comma-separated keys, defaults if empty."""
        defaults = ["latest", "popular", "toplist:yesterday", "toplist:month"]
        if value is None or not value.strip():
            return defaults
        parsed = [k.strip().lower() for k in value.split(",") if k.strip()]
        return parsed or defaults

    def _facets(value: str | None) -> list[tuple[str, int]]:
        """Parse FACETS: comma-separated Name:mask entries.

        Default: all 10 E-Hentai categories with their exclude masks.
        Format: "Name1:1021,Name2:1019,..."
        Empty or unset → defaults.
        """
        defaults: list[tuple[str, int]] = [
            ("Doujinshi", 1021),
            ("Manga", 1019),
            ("Artist CG", 1015),
            ("Game CG", 1007),
            ("Western", 991),
            ("Non-H", 959),
            ("Image Set", 895),
            ("Cosplay", 767),
            ("Asian Porn", 511),
            ("Misc", 1022),
        ]
        if value is None or not value.strip():
            return defaults
        result: list[tuple[str, int]] = []
        for entry in value.split(","):
            entry = entry.strip()
            if ":" in entry:
                name, val = entry.rsplit(":", 1)
                try:
                    result.append((name.strip(), int(val.strip())))
                except ValueError:
                    pass  # skip malformed entries
        return result or defaults

    settings = Settings(
        ipb_member_id=os.getenv("IPB_MEMBER_ID", "").strip(),
        ipb_pass_hash=os.getenv("IPB_PASS_HASH", "").strip(),
        igneous=os.getenv("IGNEOUS", "").strip(),
        eh_site=(os.getenv("EH_SITE", EH_SITE_EHENTAI).strip().lower()),
        nw=os.getenv("NW", "1").strip() or "1",
        datatags=os.getenv("DATATAGS", "1").strip() or "1",
        timeout_seconds=_float(os.getenv("TIMEOUT_SECONDS"), 6.0),
        retries=_int(os.getenv("RETRIES"), 3),
        html_interval_seconds=_float(os.getenv("HTML_INTERVAL_SECONDS"), 0.3),
        max_concurrency=_int(os.getenv("MAX_CONCURRENCY"), 5),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        pse_page_base=_int(os.getenv("PSE_PAGE_BASE"), 1),
        cache_dir=Path(os.getenv("CACHE_DIR", "./cache")),
        cache_max_gb=_gb(os.getenv("CACHE_MAX_GB"), 4.0),
        image_cache_enabled=_bool(os.getenv("IMAGE_CACHE_ENABLED"), True),
        metadata_ttl_seconds=_int(os.getenv("METADATA_TTL_SECONDS"), 3600),
        page_url_ttl_seconds=_int(os.getenv("PAGE_URL_TTL_SECONDS"), 3600),
        list_cache_ttl_seconds=_int(os.getenv("LIST_CACHE_TTL_SECONDS"), 600),
        list_layout=os.getenv("LIST_LAYOUT", "extended").strip().lower(),
        eh_profile=os.getenv("EH_PROFILE", "").strip(),
        home_groups=_home_groups(os.getenv("HOME_GROUPS")),
        home_publications=_int(os.getenv("HOME_PUBLICATIONS"), 20),
        facets=_facets(os.getenv("FACETS")),
    )
    return settings
