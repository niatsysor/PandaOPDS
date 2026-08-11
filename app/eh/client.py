"""Unified outbound HTTP client for E-Hentai.

Every outbound request must go through this client (project rule #3):
- fixed 6s default timeout
- cookies injected automatically
- 3 retries on network errors only
- E-Hentai failure detection (banned / exceedLimit / fatal error / empty body)
  mapped to internal exception types
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import Settings
from .exceptions import (
    BannedError,
    CloudflareError,
    CookieInvalidError,
    EHException,
    EHServerError,
    ExceedLimitError,
    GalleryDeletedError,
)

logger = logging.getLogger(__name__)

_BANNED_PREFIXES = ("Your IP address", "This IP address")
_EXCEED_PREFIX = "You have exceeded your image"
_FATAL_MARKER = "Page load has been aborted due to a fatal error"
_GALLERY_NOT_FOUND_PREFIX = "Gallery not found"


class EHClient:
    def __init__(self, settings: Settings, *, retries: int | None = None):
        self.settings = settings
        self.retries = retries if retries is not None else settings.retries
        self._session_ready = False
        self._session_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=True,
            cookies=settings.cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def establish_session(self) -> None:
        """Establish / refresh the authenticated session cookie jar.

        IPB cookies (`ipb_member_id` + `ipb_pass_hash`) authenticate on
        e-hentai; the same paired session is what unlocks exhentai. `igneous`
        is NOT a user-supplied long-lived credential: exhentai sets its own
        session-scoped igneous via Set-Cookie when the session is valid, which
        httpx keeps in the client cookie jar automatically for the process
        lifetime.

        e-hentai: one GET to the home page refreshes the session.
        exhentai: authenticate on e-hentai first, then touch exhentai once so
        its response seeds igneous for the exhentai.org domain in the jar.
        """
        async with self._session_lock:
            if self._session_ready:
                return
            try:
                if self.settings.is_exhentai:
                    # 1) validate/refresh the paired session on e-hentai
                    await self._request(
                        "GET", f"https://{self.settings.ehentai_host}/"
                    )
                # 2) touch the target site (exhentai sets igneous here)
                await self._request("GET", self.settings.http_origin + "/")
                self._session_ready = True
                logger.info(
                    "E-Hentai session established (site=%s)",
                    self.settings.eh_site,
                )
            except EHException:
                # leave _session_ready False; caller maps to HTTP status and
                # the circuit breaker trips on hard failures
                raise

    # -- low-level --------------------------------------------------------

    def _check_failure(self, response: httpx.Response) -> None:
        """Map E-Hentai failure signatures to internal exceptions."""
        host = response.url.host
        if host not in (self.settings.site_host, self.settings.ehentai_host):
            return

        body = response.text
        if body == "":
            raise CookieInvalidError("empty response body (login required?)")
        if body.startswith(_GALLERY_NOT_FOUND_PREFIX):
            raise GalleryDeletedError(body[:200])
        if body.startswith(_BANNED_PREFIXES):
            raise BannedError(body[:200])
        if body.startswith(_EXCEED_PREFIX):
            raise ExceedLimitError("image limit exceeded")
        if _FATAL_MARKER in body:
            raise EHServerError("E-Hentai internal error", retryable=True)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        referer: str | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer

        attempt = 0
        while True:
            attempt += 1
            try:
                kwargs: dict = {
                    "headers": headers,
                }
                if params:
                    kwargs["params"] = params
                if json_body is not None:
                    kwargs["json"] = json_body
                    kwargs.setdefault("headers", {})["Content-Type"] = "application/json"

                if stream:
                    resp = await self._client.stream(method, url, **kwargs)
                    # check headers/status before returning; body inspection done
                    # by callers for streaming (images don't carry failure text).
                else:
                    resp = await self._client.request(method, url, **kwargs)
                    self._check_failure(resp)
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # network-level error: retry (idempotent GET/POST to api)
                if attempt <= self.retries:
                    logger.warning(
                        "request %s %s failed (%s), retry %d/%d",
                        method, url, type(exc).__name__, attempt, self.retries,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 3.0))
                    continue
                raise

    # -- public helpers ---------------------------------------------------

    async def get_html(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> str:
        """GET an HTML page, raising mapped exceptions on failure signatures."""
        resp = await self._request("GET", f"{self.settings.http_origin}{path}", params=params, referer=referer)
        return resp.text

    async def get_absolute_html(
        self, url: str, params: dict[str, str] | None = None, referer: str | None = None
    ) -> str:
        """GET an absolute URL (e.g. /s/ hrefs returned absolute by the site)."""
        resp = await self._request("GET", url, params=params, referer=referer)
        return resp.text

    async def post_api(self, payload: dict) -> str:
        """POST to api.{site}/api.php and return the raw JSON body."""
        resp = await self._request("POST", self.settings.api_url, json_body=payload)
        self._check_failure(resp)
        return resp.text

    async def fetch_image_bytes(
        self,
        url: str,
        *,
        referer: str | None = None,
    ) -> bytes:
        """GET an image and return its full bytes (retries on network errors).

        Images are buffered here because every fetched image is written to the
        disk cache anyway; the OPDS proxy layer streams from cache/memory.
        """
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer

        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 404:
                        raise GalleryDeletedError("image not found (404)")
                    if resp.status_code == 403:
                        raise CloudflareError("403 from image host")
                    chunks = [c async for c in resp.aiter_bytes()]
                    return b"".join(chunks)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt <= self.retries:
                    logger.warning(
                        "image fetch %s failed (%s), retry %d/%d",
                        url, type(exc).__name__, attempt, self.retries,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 3.0))
                    continue
                raise
