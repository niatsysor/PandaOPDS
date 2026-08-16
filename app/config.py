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
    # --- HTTP Basic Auth (optional, opt-in) ---
    # The whole app (except exempt paths) requires Basic credentials when
    # BOTH auth_username and auth_password are set — a one-sided config never
    # enables auth (never lock the server out). Plain text is fine for private
    # single-user deployments behind HTTPS; comparison uses a constant-time
    # hmac.compare_digest. AUTH_EXEMPT_PATHS: comma-separated exact paths that
    # stay public regardless (e.g. client endpoints that cannot carry headers).
    auth_username: str = ""
    auth_password: str = ""
    auth_exempt_paths: tuple[str, ...] = ()

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
    timeout_seconds: float = 6.0      # default outbound timeout (6s)
    retries: int = 3                  # network-error retries
    html_interval_seconds: float = 0.3  # min delay between HTML page requests
    # HTML/API share one pool (EH API: 4-5 safe; HTML is the ban-risk traffic
    # that the interval protects). Image fetches are split out:
    #   - image_max_concurrency: full-res /stream bytes are 509-quota traffic
    #     -> conservative (5)
    #   - thumb_max_concurrency: cover/thumbnail CDN fetches (ehgt.org) match
    #     a browser's 25-thumbnail tile load on the source site -> 25
    max_concurrency: int = 5          # HTML/API outbound concurrency
    image_max_concurrency: int = 5    # full-res image bytes (/stream)
    thumb_max_concurrency: int = 25   # cover / thumbnail CDN fetches

    # --- URLs ---
    public_base_url: str = ""  # when set, feeds emit absolute URLs

    # --- PSE page numbering ---
    # OPDS-PSE spec says pages are 0-based, but LANraragi (the de-facto PSE
    # server reference) and clients built against it (Kasane) use 1-based.
    # Default 1 for client compatibility; set PSE_PAGE_BASE=0 for spec-strict.
    pse_page_base: int = 1

    # --- OPDS 2.0 acquisition mode (OPDS_ACQ_DETAIL, bool) ---
    # Whether list/root publications' acquisition link targets the detail
    # document instead of the image stream:
    #   false (default) -> acquisition points at the image stream directly
    #       (/stream/.../page/{pageNumber}, image/jpeg): clients read with
    #       zero round-trips; no acquisition link when page_count is unknown.
    #   true -> acquisition points at the detail document
    #       (/opds/v2.0/gallery/{gid}/{token}): clients perform a second
    #       request for full metadata before reading.
    # The detail document itself always exposes a direct image-stream
    # acquisition link (never a self-referencing one) in both modes.
    # Legacy OPDS_ACQ_MODE=detail|direct (string) is still honored when
    # OPDS_ACQ_DETAIL is unset.
    opds_acq_detail: bool = False

    # --- Circuit breaker cooldowns (graded by recovery horizon) ---
    # An IP ban lasts hours: long cooldown means few (safe) probe attempts.
    # The image quota rolls over within minutes: short cooldown means fast
    # recovery once the quota frees up. Override via env vars.
    banned_cooldown_seconds: float = 1800.0
    exceed_cooldown_seconds: float = 300.0

    # --- Cache ---
    cache_dir: Path = field(default_factory=lambda: Path("./cache"))
    cache_max_gb: float = 4.0
    image_cache_enabled: bool = True
    metadata_ttl_seconds: int = 600  # gdata results; browsing never touches gdata
    page_url_ttl_seconds: int = 3600
    list_cache_ttl_seconds: int = 600  # list-page parse results (search/popular/toplist...)

    # --- List page display mode ---
    # Fixed to the Extended layout (the richest parseable view: full tag set,
    # rating sprite, publish time, uploader, page count). Extended is forced on
    # every list request via ``inline_set=dm_e``; LIST_LAYOUT is intentionally
    # no longer configurable so browsing metadata never degrades.
    list_layout: str = "extended"

    # --- Tag status filter (global strategy) ---
    # E-Hentai tags carry a community-trust status (gt=confidence,
    # gtl=skepticism, gtw=incorrect). Tags below the configured level are
    # dropped from every feed (subject + mytags) so ambiguous tags never enter
    # the catalog; the status itself is never transmitted to clients.
    #   strict   -> confidence only
    #   balanced -> confidence + skepticism (default)
    #   off      -> keep everything
    tag_status_filter: str = "balanced"

    # --- Detail-page comments ---
    # Expose the gallery comment block in the OPDS 2.0 detail document
    # (extensions.reviews). The parser always extracts; this only gates the
    # output, so the shared 1h detail-page cache stays unaffected. Set
    # COMMENTS_ENABLED=0 to stop shipping comments entirely.
    comments_enabled: bool = True

    # --- uconfig profile isolation ---
    # The service defaults to a dedicated E-Hentai settings profile named
    # "PandaOPDS" (created once, then switched to during ``establish_session``),
    # isolating the service's uconfig preferences from the user's browser
    # profile. Override the name via EH_PROFILE, or set EH_PROFILE="" to
    # disable profiles and rely on the per-request ``inline_set`` override.
    eh_profile: str = "PandaOPDS"

    # --- Home navigation (TOML-driven layout) ---
    # Path to a TOML file declaring ``[[group]]`` and ``[[navigation]]``
    # sections.  Each section is a ``(type, query)`` pair:
    #   type="preset" → built-in key (latest/popular/watched/favorites/toplist:*)
    #   type="search" → arbitrary E-Hentai search expression
    # Groups carry an inline ``publications[]`` preview; navigation entries
    # are plain links.  When unset or the file is missing, a built-in default
    # layout is used.
    home_config_path: Path = field(default_factory=lambda: Path("./config/home.toml"))

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

    @property
    def auth_enabled(self) -> bool:
        """Basic Auth is active only when BOTH username and password are set.

        A one-sided config (username without password, or vice versa) is
        treated as disabled — failing open is safer than locking the server
        out with credentials nobody can supply.
        """
        return bool(self.auth_username and self.auth_password)

    # --- validation ---
    @property
    def inline_set_key(self) -> str:
        """The ``inline_set`` query parameter value for the list layout.

        Fixed to Extended (dm_e): it exposes the full tag set, rating sprite,
        publish time and page count — the metadata browsing relies on.
        """
        return "dm_e"

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

    def _acq_detail(value: str | None, legacy: str | None) -> bool:
        """Parse OPDS_ACQ_DETAIL (bool): true -> acquisition targets the
        detail document (second-request flow); false (default) -> acquisition
        points at the image stream. Legacy OPDS_ACQ_MODE=detail|direct
        (string) is honored when OPDS_ACQ_DETAIL is unset."""
        if value is not None and value.strip():
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if legacy:
            return legacy.strip().lower() == "detail"
        return False

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
        auth_username=os.getenv("AUTH_USERNAME", "").strip(),
        auth_password=os.getenv("AUTH_PASSWORD", "").strip(),
        auth_exempt_paths=tuple(
            p.strip()
            for p in os.getenv("AUTH_EXEMPT_PATHS", "").split(",")
            if p.strip()
        ),
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
        image_max_concurrency=_int(os.getenv("IMAGE_MAX_CONCURRENCY"), 5),
        thumb_max_concurrency=_int(os.getenv("THUMB_MAX_CONCURRENCY"), 25),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        pse_page_base=_int(os.getenv("PSE_PAGE_BASE"), 1),
        opds_acq_detail=_acq_detail(
            os.getenv("OPDS_ACQ_DETAIL"), os.getenv("OPDS_ACQ_MODE")
        ),
        cache_dir=Path(os.getenv("CACHE_DIR", "./cache")),
        cache_max_gb=_gb(os.getenv("CACHE_MAX_GB"), 4.0),
        image_cache_enabled=_bool(os.getenv("IMAGE_CACHE_ENABLED"), True),
        metadata_ttl_seconds=_int(os.getenv("METADATA_TTL_SECONDS"), 600),
        page_url_ttl_seconds=_int(os.getenv("PAGE_URL_TTL_SECONDS"), 3600),
        list_cache_ttl_seconds=_int(os.getenv("LIST_CACHE_TTL_SECONDS"), 600),
        banned_cooldown_seconds=_float(os.getenv("BANNED_COOLDOWN_SECONDS"), 1800.0),
        exceed_cooldown_seconds=_float(os.getenv("EXCEED_COOLDOWN_SECONDS"), 300.0),
        tag_status_filter=(
            os.getenv("TAG_STATUS_FILTER", "balanced").strip().lower() or "balanced"
        ),
        comments_enabled=_bool(os.getenv("COMMENTS_ENABLED"), True),
        eh_profile=os.getenv("EH_PROFILE", "PandaOPDS").strip(),
        home_config_path=Path(os.getenv("HOME_CONFIG", "./config/home.toml")),
        facets=_facets(os.getenv("FACETS")),
    )
    return settings
