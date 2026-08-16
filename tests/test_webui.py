"""WebUI tests (offline).

Covers:
- / HTML page (200, single-page shell)
- /api/config: grouped fields, credential masking, set flags
- /api/status: app/site/circuit/cache/home summary shape
- /api/home: TOML source flag, default fallback, parse-error surfacing
- no-IPB-cookie scenario still serves all endpoints
"""

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.eh.service import EHService


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc", home_config_path=None)
    base.update(kw)
    return Settings(**base)


def _install_app_state(settings: Settings, service: EHService) -> None:
    from app.main import app

    app.state.settings = settings
    app.state.service = service
    app.state.config_ok = True
    app.state.config_error = None


async def _get(path: str):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _find_field(groups: list[dict], key: str) -> dict:
    for g in groups:
        for f in g["fields"]:
            if f["key"] == key:
                return f
    raise AssertionError(f"field {key!r} not found in config groups")


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_page_served(tmp_path):
    s = _settings(cache_dir=tmp_path)
    _install_app_state(s, EHService(s))
    resp = await _get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Panda" in resp.text and "仪表盘" in resp.text


def _read_page() -> str:
    return (
        Path(__file__).resolve().parent.parent / "app" / "webui" / "page.html"
    ).read_text(encoding="utf-8")


def test_page_nav_ids_match_views():
    """Every nav item must have a matching id so switchView() can toggle the
    active state without null derefs (regression: nav items used to lack
    id="nav-<view>", breaking the config/home views)."""
    import re

    html = _read_page()
    nav_ids = set(re.findall(r'class="nav-item[^"]*" id="(nav-\w+)"', html))
    view_ids = set(re.findall(r'<section id="(view-\w+)"', html))
    assert nav_ids, "no nav items found"
    # every nav item has a corresponding view section and vice versa
    assert {v.replace("nav-", "view-") for v in nav_ids} == view_ids


def test_page_switchview_null_safe():
    """switchView() guards against missing elements (no hard failure)."""
    html = _read_page()
    assert "if (navEl) navEl.classList.toggle" in html
    assert "if (view) view.hidden = v !== name" in html


@pytest.mark.asyncio
async def test_page_without_ipb(tmp_path):
    """Public-only deployment: page + APIs still work."""
    s = _settings(ipb_member_id="", ipb_pass_hash="", cache_dir=tmp_path)
    _install_app_state(s, EHService(s))
    assert (await _get("/")).status_code == 200
    assert (await _get("/api/config")).status_code == 200
    assert (await _get("/api/home")).status_code == 200


# --------------------------------------------------------------------------
# /api/config
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_groups_and_masking(tmp_path):
    s = _settings(cache_dir=tmp_path)
    _install_app_state(s, EHService(s))
    data = (await _get("/api/config")).json()

    groups = data["groups"]
    ids = [g["id"] for g in groups]
    assert ids[:4] == ["auth", "identity", "http", "opds"]

    pass_hash = _find_field(groups, "ipb_pass_hash")
    assert pass_hash["masked"] is True
    assert pass_hash["set"] is True
    assert pass_hash["value"] != "abc"
    assert "abc" not in str(data)  # credential never serialized verbatim

    igneous = _find_field(groups, "igneous")
    assert igneous["masked"] is True and igneous["set"] is False

    # non-sensitive fields carry real values
    assert _find_field(groups, "eh_site")["value"] == "e-hentai"
    assert _find_field(groups, "pse_page_base")["value"] == 1
    assert _find_field(groups, "facets")["value"] != ""

    # derived info present
    assert data["derived"]["has_ipb"] is True
    assert data["derived"]["api_url"] == "https://api.e-hentai.org/api.php"


@pytest.mark.asyncio
async def test_config_masks_unset_credential(tmp_path):
    s = _settings(ipb_member_id="", ipb_pass_hash="", cache_dir=tmp_path)
    _install_app_state(s, EHService(s))
    data = (await _get("/api/config")).json()
    assert _find_field(data["groups"], "ipb_pass_hash")["set"] is False
    assert data["derived"]["has_ipb"] is False


# --------------------------------------------------------------------------
# /api/status
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_shape(tmp_path):
    s = _settings(cache_dir=tmp_path)
    service = EHService(s)
    _install_app_state(s, service)
    data = (await _get("/api/status")).json()

    assert data["app"]["config_ok"] is True
    assert data["app"]["version"]
    assert data["site"]["host"] == "e-hentai.org"
    assert data["site"]["has_ipb"] is True

    assert data["circuit"]["state"] == "closed"
    assert data["circuit"]["reason"] is None
    assert data["circuit"]["remaining"] == 0.0

    assert data["throttle"]["html_requests"] == 0
    assert data["memory_cache"]["size"] == 0
    assert data["disk_cache"]["enabled"] is True
    assert data["home"]["using_file"] is False
    assert data["home"]["groups"] >= 1


@pytest.mark.asyncio
async def test_status_circuit_open(tmp_path):
    """An open circuit breaker is surfaced with reason + remaining cooldown."""
    s = _settings(cache_dir=tmp_path)
    service = EHService(s)
    await service.throttle.trip("BannedError: IP banned", cooldown=600)
    _install_app_state(s, service)
    data = (await _get("/api/status")).json()
    assert data["circuit"]["state"] == "open"
    assert "banned" in (data["circuit"]["reason"] or "").lower()
    assert 0 < data["circuit"]["remaining"] <= 600


# --------------------------------------------------------------------------
# /api/home
# --------------------------------------------------------------------------

_GOOD_TOML = """\
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
kind = "navigation"
title = "关注"
type = "preset"
query = "watched"
"""

_BAD_TOML = "this is not [ valid toml"


@pytest.mark.asyncio
async def test_home_uses_file(tmp_path):
    home = tmp_path / "home.toml"
    home.write_text(_GOOD_TOML, encoding="utf-8")
    s = _settings(cache_dir=tmp_path, home_config_path=home)
    _install_app_state(s, EHService(s))
    data = (await _get("/api/home")).json()

    assert data["using_file"] is True
    assert data["error"] is None
    assert [g["id"] for g in data["groups"]] == ["rankings"]
    sections = data["sections"]
    assert sections[0]["kind"] == "publication"
    assert sections[0]["query"] == "toplist:yesterday"
    assert sections[0]["href"] == "/opds/v2.0/toplist?period=yesterday"
    # auth-gated nav section flagged
    watched = next(x for x in sections if x["query"] == "watched")
    assert watched["auth_required"] is True
    assert watched["group"] is None  # ungrouped → root navigation


@pytest.mark.asyncio
async def test_home_default_fallback_when_missing(tmp_path):
    missing = tmp_path / "nope.toml"
    s = _settings(cache_dir=tmp_path, home_config_path=missing)
    _install_app_state(s, EHService(s))
    data = (await _get("/api/home")).json()
    assert data["using_file"] is False
    assert data["error"] is None
    # built-in default layout has both groups and sections
    assert data["groups"] and data["sections"]


@pytest.mark.asyncio
async def test_home_surfaces_parse_error(tmp_path):
    bad = tmp_path / "home.toml"
    bad.write_text(_BAD_TOML, encoding="utf-8")
    s = _settings(cache_dir=tmp_path, home_config_path=bad)
    _install_app_state(s, EHService(s))
    data = (await _get("/api/home")).json()
    assert data["using_file"] is False
    assert data["error"]  # parse error surfaced to the page
    assert data["sections"]  # fallback layout still usable


# --------------------------------------------------------------------------
# /api/cache/clear
# --------------------------------------------------------------------------

async def _post(path: str):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path)


@pytest.mark.asyncio
async def test_cache_clear_endpoint(tmp_path):
    """POST /api/cache/clear purges the disk image cache (returns count)."""
    s = _settings(cache_dir=tmp_path)
    service = EHService(s)
    _install_app_state(s, service)
    await service.disk.put(1, "tok", 1, b"page-bytes")
    await service.disk.put(1, "tok", -1, b"cover-bytes")
    assert service.disk.stats["entries"] == 2

    resp = await _post("/api/cache/clear")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleared"] == 2
    assert data["enabled"] is True
    assert service.disk.stats["entries"] == 0
    assert service.disk.stats["bytes"] == 0
    assert await service.disk.get(1, "tok", 1) is None
    await service.close()
