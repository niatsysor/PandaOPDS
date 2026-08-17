"""WebUI JSON API + page endpoint.

Routes (all under the root):

- ``GET /``                           → single-page HTML frontend (page.html)
- ``GET /api/status``                 → runtime status (circuit breaker,
                                        request counters, cache stats, home src)
- ``GET /api/config``                 → grouped environment config, credentials
                                        masked server-side
- ``GET /api/home``                   → home.toml layout (groups + sections,
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

router = APIRouter(tags=["webui"])

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
            "id": "auth",
            "title": "访问认证（Basic Auth）",
            "fields": [
                _field(
                    "auth_username", "认证用户名",
                    s.auth_username or "（未设置）",
                    note="两者都设置才启用；未启用时服务公开可访问",
                ),
                _field(
                    "auth_password", "认证密码",
                    masked=True, set_=bool(s.auth_password),
                    note="明文环境变量，脱敏显示；仅建议 HTTPS 反代下启用",
                ),
                _field(
                    "auth_enabled", "认证启用",
                    "开（全部路由需凭据，/health 除外）" if s.auth_enabled else "关（未配置）",
                    note="由 AUTH_USERNAME + AUTH_PASSWORD 同时设置推导",
                ),
                _field(
                    "auth_exempt_paths", "豁免路径",
                    ", ".join(sorted(s.auth_exempt_paths)) if s.auth_exempt_paths else "（无）",
                    note="AUTH_EXEMPT_PATHS：逗号分隔的精确路径，认证下仍公开；/health 恒豁免",
                ),
            ],
        },
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
                _field("max_concurrency", "HTML/API 并发上限", s.max_concurrency),
                _field("image_max_concurrency", "全图并发上限", s.image_max_concurrency),
                _field(
                    "thumb_max_concurrency", "封面图并发上限",
                    s.thumb_max_concurrency,
                    note="对齐浏览器在源站 25 缩略图并发",
                ),
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
        {
            "id": "archive",
            "title": "归档（archiver）",
            "fields": [
                _field(
                    "archive_dir", "归档数据目录", str(s.archive_dir),
                    note="持久目录：zip 母本 + meta.json；不受磁盘缓存 LRU/7 天 TTL 约束",
                ),
                _field(
                    "archive_quality", "默认画质档位", s.archive_quality,
                    note="start API 未传 quality 时的默认档（匹配 archiver 页面选项，默认 original 无损）",
                ),
                _field(
                    "archive_download_concurrency", "归档任务并发", s.archive_download_concurrency,
                    note="下载/7z 转换并发上限，其余排队；不受阅读限流约束（仍过熔断）",
                ),
                _field(
                    "archive_enabled", "启用条件",
                    "开（已配置 IPB 登录态）" if bool(s.ipb_member_id and s.ipb_pass_hash) else "关（需 IPB_MEMBER_ID + IPB_PASS_HASH）",
                    note="archiver 为会员服务（星会员 + GP）；无登录态时归档 API 返回 403",
                ),
            ],
        },
        {
            "id": "favorites",
            "title": "收藏夹（操作与同步）",
            "fields": [
                _field(
                    "favorites_enabled", "启用条件",
                    "开（已配置 IPB 登录态）" if bool(s.ipb_member_id and s.ipb_pass_hash) else "关（需 IPB_MEMBER_ID + IPB_PASS_HASH）",
                    note="写操作（add/move/remove）与目录列表需要登录态；无登录态时 API 返回 403",
                ),
                _field(
                    "favorites_sync_interval_seconds", "同步周期（秒）", s.favorites_sync_interval_seconds,
                    note="0 = 关闭周期扫描；手动 POST /api/favorites/sync/run 始终可用",
                ),
                _field(
                    "favorites_sync_archive", "自动归档新增项",
                    "开（消耗 GP！）" if s.favorites_sync_archive else "关（仅扫描记录）",
                    note="必须显式开启；对每次扫描发现的新增收藏项调用 archiver start（首次运行仅建档不归档，自第二次起生效）",
                ),
                _field(
                    "favorites_sync_categories", "扫描范围（favcat 白名单）",
                    ", ".join(str(c) for c in s.favorites_sync_categories) if s.favorites_sync_categories else "全部",
                    note="FAVORITES_SYNC_CATEGORIES：逗号分隔的收藏夹 ID；留空 = 全扫",
                ),
                _field(
                    "favorites_sync_state", "同步状态文件", str(s.favorites_sync_state),
                    note="记录已扫描/已归档 gid；删除可重置增量扫描基线",
                ),
                _field(
                    "favorites_sync_match_threshold", "增量停止阈值", s.favorites_sync_match_threshold,
                    note="连续 N 个已知 gid 即停止翻页",
                ),
                _field(
                    "favorites_sync_max_pages", "单轮扫描页数上限", s.favorites_sync_max_pages,
                    note="硬上限，防止异常时无限翻页",
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
        "auth_enabled": s.auth_enabled,
        "auth_exempt_paths": sorted(s.auth_exempt_paths),
        "archive_enabled": bool(s.ipb_member_id and s.ipb_pass_hash),
        "favorites_sync_interval_seconds": s.favorites_sync_interval_seconds,
        "favorites_sync_archive": s.favorites_sync_archive,
        "favorites_sync_categories": list(s.favorites_sync_categories),
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
            "thumb_requests": throttle["thumb_requests"],
            "max_concurrency": s.max_concurrency,
            "image_max_concurrency": s.image_max_concurrency,
            "thumb_max_concurrency": s.thumb_max_concurrency,
            "html_interval_seconds": s.html_interval_seconds,
        },
        "circuit": service.throttle.circuit.state,
        "memory_cache": {**mem, "max_entries": service.mem.max_entries},
        "disk_cache": disk,
        "archive": (
            request.app.state.archive.stats()
            if getattr(request.app.state, "archive", None) is not None
            else None
        ),
        "favorites": (
            request.app.state.favorites.status()
            if getattr(request.app.state, "favorites", None) is not None
            else None
        ),
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


@router.post("/api/cache/clear")
async def api_cache_clear(request: Request):
    """Purge the on-disk image cache.

    Disk entries live up to 7 days, so stale bytes (e.g. thumbnails cached
    before a cover-proxy fix) need an explicit clear. Memory caches are left
    untouched — they hold URLs/metadata and expire within minutes/hours.
    Route is covered by Basic Auth when enabled (same as the rest of WebUI).
    """
    service: EHService = request.app.state.service
    removed = await service.disk.clear()
    return {"cleared": removed, "enabled": service.disk.enabled}


@router.get("/")
async def webui_page():
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))
