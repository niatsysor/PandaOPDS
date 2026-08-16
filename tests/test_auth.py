"""HTTP Basic Auth tests (offline).

Covers:
- auth disabled by default: every path serves 200 (backward compatible)
- one-sided AUTH_* config never enables auth (fail open)
- enabled: 401 + WWW-Authenticate challenge + JSON body without credentials
- wrong / empty / malformed credentials rejected
- correct credentials accepted (password may contain colons)
- /health always exempt; AUTH_EXEMPT_PATHS honored
- WebUI /api/config masks the password and reports auth state
"""

import base64

import httpx
import pytest

from app.config import Settings
from app.eh.service import EHService


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc")
    base.update(kw)
    return Settings(**base)


def _install_app_state(settings: Settings, service: EHService) -> None:
    from app.main import app

    app.state.settings = settings
    app.state.service = service
    app.state.config_ok = True
    app.state.config_error = None


async def _request(path: str, headers: dict | None = None):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------
# disabled by default / fail-open
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_disabled_by_default():
    _install_app_state(_settings(), EHService(_settings()))
    for path in ("/", "/api/status", "/opds/v1.2", "/opds/v2.0", "/health"):
        resp = await _request(path)
        assert resp.status_code == 200, f"{path}: {resp.status_code}"


@pytest.mark.asyncio
async def test_one_sided_config_fails_open():
    # username without password must NOT enable auth (never lock the server out)
    _install_app_state(_settings(auth_username="reader"), EHService(_settings(auth_username="reader")))
    resp = await _request("/")
    assert resp.status_code == 200
    assert _settings(auth_username="reader").auth_enabled is False


@pytest.mark.asyncio
async def test_auth_enabled_requires_both():
    s = _settings(auth_username="reader", auth_password="secret")
    assert s.auth_enabled is True
    assert _settings(auth_username="reader").auth_enabled is False
    assert _settings(auth_password="secret").auth_enabled is False


# --------------------------------------------------------------------------
# enabled: challenge / rejection
# --------------------------------------------------------------------------

def _enabled_settings(**kw) -> Settings:
    base = dict(auth_username="reader", auth_password="secret")
    base.update(kw)
    return _settings(**base)


@pytest.mark.asyncio
async def test_missing_credentials_401_with_challenge():
    s = _enabled_settings()
    _install_app_state(s, EHService(s))
    for path in ("/", "/api/status", "/opds/v1.2", "/opds/v2.0", "/stream/1/abc/page/1"):
        resp = await _request(path)
        assert resp.status_code == 401, f"{path}: {resp.status_code}"
        assert resp.headers["www-authenticate"] == 'Basic realm="PandaOPDS"'
        body = resp.json()
        assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_wrong_credentials_401():
    s = _enabled_settings()
    _install_app_state(s, EHService(s))
    resp = await _request("/api/status", headers=_basic("reader", "wrong"))
    assert resp.status_code == 401
    resp = await _request("/api/status", headers=_basic("other", "secret"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_malformed_headers_401():
    s = _enabled_settings()
    _install_app_state(s, EHService(s))
    # not base64 at all
    resp = await _request("/", headers={"Authorization": "Basic !!!not-base64!!!"})
    assert resp.status_code == 401
    # wrong scheme
    resp = await _request("/", headers={"Authorization": "Bearer token"})
    assert resp.status_code == 401
    # empty credentials ("Basic Og==" == ":")
    resp = await _request("/", headers=_basic("", ""))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_correct_credentials_200():
    s = _enabled_settings()
    _install_app_state(s, EHService(s))
    resp = await _request("/api/status", headers=_basic("reader", "secret"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_password_with_colon():
    s = _enabled_settings(auth_username="reader", auth_password="pa:ss:word")
    _install_app_state(s, EHService(s))
    resp = await _request("/api/status", headers=_basic("reader", "pa:ss:word"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unicode_credentials():
    s = _enabled_settings(auth_username="读者", auth_password="密码")
    _install_app_state(s, EHService(s))
    resp = await _request("/api/status", headers=_basic("读者", "密码"))
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# exemptions
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_always_exempt():
    s = _enabled_settings()
    _install_app_state(s, EHService(s))
    resp = await _request("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_exempt_paths_public():
    s = _enabled_settings(auth_exempt_paths=("/opds/v2.0/search.xml",))
    _install_app_state(s, EHService(s))
    # exempt path: no credentials needed
    resp = await _request("/opds/v2.0/search.xml")
    assert resp.status_code == 200
    # sibling path still protected
    resp = await _request("/opds/v2.0")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_exempt_paths_parse_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "reader")
    monkeypatch.setenv("AUTH_PASSWORD", "secret")
    monkeypatch.setenv("AUTH_EXEMPT_PATHS", "/a, /b ,/c")
    from app.config import load_settings

    s = load_settings()
    assert s.auth_enabled is True
    assert s.auth_exempt_paths == ("/a", "/b", "/c")


# --------------------------------------------------------------------------
# WebUI config surface
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webui_config_masks_auth_password():
    s = _enabled_settings(auth_exempt_paths=("/opds/v2.0/search.xml",))
    _install_app_state(s, EHService(s))
    resp = await _request("/api/config", headers=_basic("reader", "secret"))
    assert resp.status_code == 200
    data = resp.json()

    assert data["derived"]["auth_enabled"] is True
    assert data["derived"]["auth_exempt_paths"] == ["/opds/v2.0/search.xml"]

    auth_group = next(g for g in data["groups"] if g["id"] == "auth")
    fields = {f["key"]: f for f in auth_group["fields"]}
    assert fields["auth_username"]["value"] == "reader"
    assert fields["auth_password"]["masked"] is True
    assert fields["auth_password"]["set"] is True
    assert "secret" not in str(data)
    assert fields["auth_enabled"]["value"] == "开（全部路由需凭据，/health 除外）"
