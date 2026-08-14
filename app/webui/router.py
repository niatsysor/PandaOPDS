"""WebUI JSON API + page endpoint.

Routes (all under ``/webui``):

- ``GET /webui``                      → single-page HTML frontend (page.html)
- ``GET /webui/api/status``           → runtime status (circuit breaker,
                                        request counters, cache stats, home src)
- ``GET /webui/api/config``           → grouped environment config, credentials
                                        masked server-side
- ``GET /webui/api/home``             → home.toml layout (groups + sections,
                                        source flag / parse error)

Nothing here touches E-Hentai: it only reads ``app.state.settings`` /
``app.state.service``, so the page works even when the upstream is unreachable
or the config is invalid.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import Settings
from ..eh.service import EHService
from ..home_config import DEFAULT_CONFIG, build_href, is_auth_required, parse_home_toml

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webui", tags=["webui"])

_PAGE = Path(__file__).parent / "page.html"

# Never returned verbatim, even to the owner (page screenshots / unauthed
# reverse-proxy exposure shouldn't leak session credentials).
_MASK = "••••••••"


# ---------------------------------------------------------------------------
# config assembly
# ---------------------------------------------------------------------------

def _field(
    key: str,
    label: str,
    value=None,
    *,
    note: str | None = None,
    masked: bool = False,
    set_: bool = True,
) -> dict:
    """One config row. ``masked`` rows carry a placeholder instead of the real
    value, plus a ``set`` flag so the page can hint configured/unset."""
    if masked:
        value = _MASK
    return {
        "key": key,
        "label": label,
        "value": value,
        "note": note,
        "masked": masked,
        "set": set_,
    }


def _settings_groups(s: Settings) -> list[dict]:
    return [
        {
            "id": "identity",
            "title": "E-Hentai 身份与站点",
            "fields": [
                _field("eh_site", "站点", s.eh_site, note=f"页面 host: {s.site_host}"),
                _field(
                    "ipb_member_id", "IPB Member ID",
                    s.ipb_member_id or "（未设置）",
                    note="登录标识；未设置时 Watched/Favorites 导航不输出",
                ),
                _field(
                    "ipb_pass_hash", "IPB Pass Hash",
                    masked=True, set_=bool(s.ipb_pass_hash),
                    note="登录凭据，脱敏显示",
                ),
                _field(
                    "igneous", "Ignéous 种子",
                    masked=True,
                    set_=bool(s.igneous and s.igneous.lower() != "mystery"),
                    note="可选；exhentai 会话建立时自动下发，无需提供",
                ),
                _field("nw", "NW（绕过 Offensive 警告）", s.nw),
                _field("datatags", "DATATAGS（新缩略图结构）", s.datatags),
                _field("eh_profile", "uconfig 独立 Profile", s.eh_profile or "（关闭）"),
            ],
        },
        {
            "id": "http",
            "title": "HTTP 与节流",
            "fields": [
                _field("timeout_seconds", "出站超时（秒）", s.timeout_seconds),
                _field("retries", "网络错误重试次数", s.retries),
                _field(
                    "html_interval_seconds", "HTML 请求最小间隔（秒）",
                    s.html_interval_seconds, note="防封关键参数",
                ),
                _field("max_concurrency", "出站并发上限", s.max_concurrency),
            ],
        },
        {
            "id": "opds",
            "title": "OPDS / PSE",
            "fields": [
                _field(
                    "public_base_url", "对外基础 URL",
                    s.public_base_url or "（相对路径）",
                    note="设置后 feed 输出绝对 URL（Stump 等客户端必需）",
                ),
                _field(
                    "pse_page_base", "PSE 页码基数", s.pse_page_base,
                    note="1 = LANraragi/Kasane 兼容（默认）；0 = 规范原文",
                ),
                _field(
                    "opds_acq_detail", "acquisition 指向详情文档",
                    "是（二次请求流程）" if s.opds_acq_detail else "否（直接指向图片流）",
                ),
                _field(
                    "list_layout", "列表布局", s.list_layout,
                    note="固定 extended（元数据最全），不可配置",
                ),
            ],
        },
        {
            "id": "breaker",
            "title": "熔断与缓存",
            "fields": [
                _field("banned_cooldown_seconds", "IP 封禁熔断冷却（秒）", s.banned_cooldown_seconds),
                _field("exceed_cooldown_seconds", "图片限额熔断冷却（秒）", s.exceed_cooldown_seconds),
                _field("cache_dir", "磁盘缓存目录", str(s.cache_dir)),
                _field("cache_max_gb", "磁盘缓存上限（GB）", s.cache_max_gb),
                _field("image_cache_enabled", "磁盘缓存开关", "开" if s.image_cache_enabled else "关"),
                _field(
                    "metadata_ttl_seconds", "元数据缓存 TTL（秒）", s.metadata_ttl_seconds,
                    note="gdata 结果；主链路零 gdata",
                ),
                _field("page_url_ttl_seconds", "页面 URL 映射 TTL（秒）", s.page_url_ttl_seconds),
                _field("list_cache_ttl_seconds", "列表解析缓存 TTL（秒）", s.list_cache_ttl_seconds),
            ],
        },
        {
            "id": "tags",
            "title": "标签与评论",
            "fields": [
                _field(
                    "tag_status_filter", "标签可信度过滤", s.tag_status_filter,
                    note="strict（仅 confidence）/ balanced（默认）/ off",
                ),
                _field(
                    "comments_enabled", "评论区输出（extensions.reviews）",
                    "开" if s.comments_enabled else "关",
                ),
            ],
        },
        {
            "id": "home",
            "title": "首页与分类",
            "fields": [
                _field(
                    "home_config_path", "首页布局配置文件", str(s.home_config_path),
                    note="仅 OPDS 2.0 消费；缺失/损坏时回退内置默认布局",
                ),
                _field(
                    "facets", "分类筛选（facets）",
                    ", ".join(f"{name}（掩码 {mask}）" for name, mask in s.facets),
                ),
            ],
        },
    ]


def _derived(s: Settings) -> dict:
    return {
        "site_host": s.site_host,
        "http_origin": s.http_origin,
        "api_url": s.api_url,
        "ehentai_host": s.ehentai_host,
        "is_exhentai": s.is_exhentai,
        "has_ipb": bool(s.ipb_member_id and s.ipb_pass_hash),
    }


# ---------------------------------------------------------------------------
# home.toml assembly
# ---------------------------------------------------------------------------

def _home_config(s: Settings) -> tuple:
    """Load the home layout: (config, using_file, error). Re-parses here so a
    parse failure is surfaced to the page instead of being swallowed by
    ``load_home_config`` (which falls back silently for the OPDS feed)."""
    path = s.home_config_path
    error = None
    if path and path.exists():
        try:
            return parse_home_toml(path), True, None
        except Exception as exc:  # tomllib.TOMLDecodeError, OSError, ...
            logger.warning("home.toml parse failed (%s): %s", path, exc)
            error = str(exc)
    return DEFAULT_CONFIG, False, error


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@router.get("/api/status")
async def api_status(request: Request):
    service: EHService = request.app.state.service
    s: Settings = request.app.state.settings
    stats = await service.stats()
    mem = stats["memory_cache"]
    disk = stats["disk_cache"]
    throttle = stats["throttle"]
    config, using_file, error = _home_config(s)
    return {
        "app": {
            "config_ok": bool(getattr(request.app.state, "config_ok", True)),
            "config_error": getattr(request.app.state, "config_error", None),
            "version": getattr(request.app, "version", "0.1.0"),
        },
        "site": {
            "site": s.eh_site,
            "host": s.site_host,
            "http_origin": s.http_origin,
            "api_url": s.api_url,
            "public_base_url": s.public_base_url or "（相对路径）",
            "is_exhentai": s.is_exhentai,
            "has_ipb": bool(s.ipb_member_id and s.ipb_pass_hash),
        },
        "throttle": {
            "html_requests": throttle["html_requests"],
            "api_requests": throttle["api_requests"],
            "image_requests": throttle["image_requests"],
            "max_concurrency": s.max_concurrency,
            "html_interval_seconds": s.html_interval_seconds,
        },
        "circuit": service.throttle.circuit.state,
        "memory_cache": {**mem, "max_entries": service.mem.max_entries},
        "disk_cache": disk,
        "home": {
            "path": str(s.home_config_path),
            "using_file": using_file,
            "error": error,
            "groups": len(config.groups),
            "sections": len(config.sections),
        },
    }


@router.get("/api/config")
async def api_config(request: Request):
    s: Settings = request.app.state.settings
    return {"derived": _derived(s), "groups": _settings_groups(s)}


@router.get("/api/home")
async def api_home(request: Request):
    s: Settings = request.app.state.settings
    config, using_file, error = _home_config(s)
    return {
        "path": str(s.home_config_path),
        "using_file": using_file,
        "error": error,
        "groups": [{"id": g.id, "title": g.title} for g in config.groups],
        "sections": [
            {
                "kind": sec.kind,
                "title": sec.title or sec.query,
                "type": sec.type,
                "query": sec.query,
                "count": sec.count,
                "group": sec.group or None,
                "href": build_href(type=sec.type, query=sec.query),
                "auth_required": is_auth_required(sec.type, sec.query),
            }
            for sec in config.sections
        ],
    }


@router.get("")
@router.get("/")
async def webui_page():
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))
