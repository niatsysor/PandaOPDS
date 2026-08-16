"""HTTP Basic Auth for the whole app (optional, opt-in via env vars).

- Enabled only when BOTH ``AUTH_USERNAME`` and ``AUTH_PASSWORD`` are set
  (see ``Settings.auth_enabled``); a one-sided config fails open.
- Passwords are compared with constant-time ``hmac.compare_digest``.
- ``/health`` is always exempt (probes carry no credentials); additional
  exact paths may be exempted via ``AUTH_EXEMPT_PATHS`` (comma-separated).
- Failed attempts are logged at INFO with the path only — never credentials.
"""

from __future__ import annotations

import base64
import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import Settings

logger = logging.getLogger(__name__)

AUTH_REALM = "PandaOPDS"
WWW_AUTHENTICATE = f'Basic realm="{AUTH_REALM}"'

# The health probe must stay reachable by orchestrators / load balancers that
# do not carry credentials. Merged with the configurable AUTH_EXEMPT_PATHS.
HEALTH_PATH = "/health"


def public_paths(settings: Settings) -> frozenset[str]:
    """Exact paths that never require authentication."""
    return frozenset({HEALTH_PATH, *settings.auth_exempt_paths})


def parse_basic_credentials(header: str) -> tuple[str, str] | None:
    """Decode ``(username, password)`` from a Basic Authorization header.

    Returns ``None`` on any malformed input (wrong scheme, invalid base64,
    non-UTF-8 payload) so the caller replies 401. Basic scheme does not allow
    colons in usernames, so the first ``:`` separates user from password —
    passwords may contain colons.
    """
    if not header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, _, password = raw.partition(":")
    return username, password


def _constant_eq(a: str, b: str) -> bool:
    """Constant-time string comparison.

    ``hmac.compare_digest`` only accepts ASCII strings; encoding both sides to
    UTF-8 bytes keeps non-ASCII usernames/passwords working while preserving
    the timing-safe comparison.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_credentials(username: str, password: str, settings: Settings) -> bool:
    """Constant-time comparison against the configured credentials."""
    return bool(
        settings.auth_username
        and settings.auth_password
        and _constant_eq(username, settings.auth_username)
        and _constant_eq(password, settings.auth_password)
    )


def unauthorized_response() -> JSONResponse:
    """401 with the challenge header clients need to prompt for credentials."""
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "detail": "valid PandaOPDS credentials required"},
        headers={"WWW-Authenticate": WWW_AUTHENTICATE},
    )


def auth_request_ok(request: Request) -> bool:
    """Check whether an incoming request passes Basic Auth (or is exempt).

    Central decision point used by the app-level middleware.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None or not settings.auth_enabled:
        return True
    if request.url.path in public_paths(settings):
        return True
    header = request.headers.get("Authorization", "")
    creds = parse_basic_credentials(header)
    return creds is not None and verify_credentials(*creds, settings)
