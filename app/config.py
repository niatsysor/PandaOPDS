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
    html_interval_seconds: float = 1.5  # min delay between HTML page requests
    max_concurrency: int = 2          # global outbound concurrency

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

    # --- Home navigation (v2.0 server-driven layout) ---
    # Nav items that carry `extensions.layout=showcase` (expanded into a grid
    # preview block by the first-party client). None = all items; a list = only
    # the named keys (`watched`, `favorites`, `popular`, `toplist:yesterday`,
    # `toplist:month`, `toplist:year`, `toplist:alltime`). v1.2 never carries
    # this flag (documented constraint in AGENTS.md).
    showcase_nav: list[str] | None = None
    # How many Latest publications the v2.0 home document embeds in its top
    # level `publications[]` array (the universal-client fallback grid).
    home_publications: int = 10

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
    def validate(self) -> None:
        if self.eh_site not in _SITE_HOSTS:
            raise ConfigError(
                f"EH_SITE must be one of {list(_SITE_HOSTS)}, got {self.eh_site!r}"
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

    def _nav_list(value: str | None) -> list[str] | None:
        """Parse SHOWCASE_NAV: comma-separated keys, empty/None = all."""
        if value is None or not value.strip():
            return None
        return [k.strip().lower() for k in value.split(",") if k.strip()]

    settings = Settings(
        ipb_member_id=os.getenv("IPB_MEMBER_ID", "").strip(),
        ipb_pass_hash=os.getenv("IPB_PASS_HASH", "").strip(),
        igneous=os.getenv("IGNEOUS", "").strip(),
        eh_site=(os.getenv("EH_SITE", EH_SITE_EHENTAI).strip().lower()),
        nw=os.getenv("NW", "1").strip() or "1",
        datatags=os.getenv("DATATAGS", "1").strip() or "1",
        timeout_seconds=_float(os.getenv("TIMEOUT_SECONDS"), 6.0),
        retries=_int(os.getenv("RETRIES"), 3),
        html_interval_seconds=_float(os.getenv("HTML_INTERVAL_SECONDS"), 1.5),
        max_concurrency=_int(os.getenv("MAX_CONCURRENCY"), 2),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        pse_page_base=_int(os.getenv("PSE_PAGE_BASE"), 1),
        cache_dir=Path(os.getenv("CACHE_DIR", "./cache")),
        cache_max_gb=_gb(os.getenv("CACHE_MAX_GB"), 4.0),
        image_cache_enabled=_bool(os.getenv("IMAGE_CACHE_ENABLED"), True),
        metadata_ttl_seconds=_int(os.getenv("METADATA_TTL_SECONDS"), 3600),
        page_url_ttl_seconds=_int(os.getenv("PAGE_URL_TTL_SECONDS"), 3600),
        list_cache_ttl_seconds=_int(os.getenv("LIST_CACHE_TTL_SECONDS"), 600),
        showcase_nav=_nav_list(os.getenv("SHOWCASE_NAV")),
        home_publications=_int(os.getenv("HOME_PUBLICATIONS"), 10),
    )
    return settings
