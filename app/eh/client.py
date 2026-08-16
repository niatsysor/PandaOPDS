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
from pathlib import Path

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

        When ``eh_profile`` is configured, a dedicated uconfig profile is
        created (once) and made active so the service's list-layout preference
        is isolated from the user's web-browser profile.
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

                # 3) create / switch to the dedicated uconfig profile (optional)
                if self.settings.eh_profile:
                    await self._ensure_profile(self.settings.eh_profile)

                self._session_ready = True
                logger.info(
                    "E-Hentai session established (site=%s)",
                    self.settings.eh_site,
                )
            except EHException:
                # leave _session_ready False; caller maps to HTTP status and
                # the circuit breaker trips on hard failures
                raise

    async def _ensure_profile(self, profile_name: str) -> None:
        """Create (if not exists) and switch to a named uconfig profile.

        Strategy (covers all three edge cases):

        1. **Doesn't exist yet** → POST ``profile_action=create`` to create
           and switch in one step.
        2. **Already exists** → parse the uconfig page for the existing
           profile's numeric ID, then POST ``profile_set=<id>`` to switch.
        3. **Slots full / creation rejected** → fall back silently; the
           per-request ``inline_set`` override is always active regardless.

        Profile IDs and names are read from ``#profile_form > select > option``
        elements on the uconfig page.
        """
        import re

        # -- locate existing profile (if any) ------------------------------
        profile_id: int | None = None
        try:
            html_text = await self.get_html("/uconfig.php")
            # Option values are numeric profile IDs; text is the profile name.
            for m in re.finditer(
                r'<option\s+value="(\d+)"[^>]*>\s*'
                + re.escape(profile_name)
                + r'\s*</option>',
                html_text,
            ):
                profile_id = int(m.group(1))
                break
        except EHException:
            logger.debug("could not read uconfig page for profile check")

        # -- Phase 2a: profile already exists — just switch -----------------
        if profile_id is not None:
            try:
                resp = await self._request(
                    "POST",
                    f"{self.settings.http_origin}/uconfig.php",
                    form_data={"profile_set": str(profile_id)},
                    referer=f"{self.settings.http_origin}/uconfig.php",
                )
                logger.info(
                    "uconfig switched to existing profile %r (id=%s, status %s)",
                    profile_name, profile_id, resp.status_code,
                )
                return
            except EHException:
                logger.warning(
                    "uconfig switch to existing profile %r failed; "
                    "list pages will still use inline_set override",
                    profile_name,
                )
                return

        # -- Phase 2b: profile does not exist — create + switch -------------
        try:
            resp = await self._request(
                "POST",
                f"{self.settings.http_origin}/uconfig.php",
                form_data={
                    "profile_action": "create",
                    "profile_name": profile_name,
                    "profile_set": "616",  # switch after create
                },
                referer=f"{self.settings.http_origin}/uconfig.php",
            )
            logger.info(
                "uconfig profile %r created (status %s)",
                profile_name, resp.status_code,
            )
        except EHException:
            logger.warning(
                "uconfig profile create failed (profile=%r, slots may be full); "
                "list pages will still use inline_set override",
                profile_name,
            )

    # -- low-level --------------------------------------------------------

    def _check_failure(self, response: httpx.Response) -> None:
        """Map E-Hentai failure signatures to internal exceptions."""
        host = response.url.host
        if host not in (self.settings.site_host, self.settings.ehentai_host):
            return

        if response.status_code == 403:
            raise CloudflareError(
                "403 from E-Hentai host (Cloudflare challenge)"
            )

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
        form_data: dict[str, str] | None = None,
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
                if form_data is not None:
                    kwargs["data"] = form_data

                if stream:
                    resp = await self._client.stream(method, url, **kwargs)
                    # check headers/status before returning; body inspection done
                    # by callers for streaming (images don't carry failure text).
                else:
                    resp = await self._client.request(method, url, **kwargs)
                    self._check_failure(resp)
                logger.debug("EH outbound: %s %s%s", method, url,
                             ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""))
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
                # exhausted: normalize to EHException so the service layer and
                # the global handler see one error family (502), never a raw
                # httpx exception escaping to the client as an opaque 500.
                raise EHException(
                    f"upstream transport error after {attempt} attempts: {exc}"
                ) from exc
            except EHException as exc:
                # retryable E-Hentai failures (fatal error page / Cloudflare
                # 403): same backoff loop as network errors, fixed attempts.
                if exc.retryable and attempt <= self.retries:
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

    # -- archiver (GP-purchased archives) ---------------------------------

    async def get_archiver_page(self, gid: int, token: str) -> str:
        """GET /archiver.php for a gallery (tier list / status page).

        Requires a logged-in Star-member session (established via
        ``establish_session``). Returns the page HTML for parsing.
        """
        params = {"gid": str(gid), "token": token}
        resp = await self._request(
            "GET",
            f"{self.settings.http_origin}/archiver.php",
            params=params,
            referer=f"{self.settings.http_origin}/g/{gid}/{token}/",
        )
        return resp.text

    async def submit_archiver(
        self, gid: int, token: str, dltype: str, dlcheck: str
    ) -> str:
        """POST /archiver.php to trigger an archive (tier ``dltype``).

        Mirrors the real form: gid/token travel in the URL query (the form
        action), the body carries ``dltype`` + ``dlcheck`` (the submit button
        value). Free tiers (already unlocked) cost no GP; paid tiers debit GP
        here. Returns the resulting page (preparing/ready/error).
        """
        resp = await self._request(
            "POST",
            f"{self.settings.http_origin}/archiver.php",
            params={"gid": str(gid), "token": token},
            form_data={"dltype": dltype, "dlcheck": dlcheck},
            referer=(
                f"{self.settings.http_origin}/archiver.php?gid={gid}&token={token}"
            ),
        )
        return resp.text

    async def stream_archive(
        self,
        url: str,
        dest: Path,
        *,
        progress_cb=None,
        offset: int = 0,
    ) -> int:
        """Stream-download an archive file (7z/zip) to ``dest``.

        Overrides the default 6s timeout: GB-scale downloads need a generous
        idle-read timeout (progress keeps the connection alive). Bytes are
        written via a thread so the event loop never blocks. When ``offset``
        is non-zero a ``Range: bytes={offset}-`` header resumes a partial
        download (hath.network serves 206); a 200 reply means the server
        ignored the range and the file restarts from scratch. Returns the
        total bytes written; propagates mapped E-Hentai exceptions and
        normalizes transport failures to ``EHException``.
        """
        timeout = httpx.Timeout(
            connect=self.settings.timeout_seconds,
            read=600.0,  # 10 min idle-read; active progress resets it
            write=60.0,
            pool=self.settings.timeout_seconds,
        )
        headers = {"Referer": f"{self.settings.http_origin}/archiver.php"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        total = 0
        try:
            async with self._client.stream(
                "GET", url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status_code == 403:
                    raise CloudflareError("403 from archive host")
                if resp.status_code == 404:
                    raise EHException(
                        "archive download 404 (link expired; re-trigger the archive)"
                    )
                if resp.status_code >= 400:
                    raise EHServerError(
                        f"archive host returned HTTP {resp.status_code}",
                        retryable=True,
                    )
                # 206 with an offset resumes; 200 (server ignored Range)
                # restarts from scratch; a bare 206 at offset=0 is fine too.
                resumed = resp.status_code == 206 and offset > 0
                mode = "ab" if resumed else "wb"
                with dest.open(mode) as fh:
                    async for chunk in resp.aiter_bytes():
                        await asyncio.to_thread(fh.write, chunk)
                        total += len(chunk)
                        if progress_cb:
                            progress_cb(total)
            return offset + total if resumed else total
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise EHException(
                f"archive download failed (transport): {exc}"
            ) from exc

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
                    if resp.status_code >= 400:
                        # CDN 5xx (or any other error page) must not be
                        # buffered and disk-cached as if it were image bytes.
                        raise EHServerError(
                            f"image host returned HTTP {resp.status_code}",
                            retryable=True,
                        )
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
                raise EHException(
                    f"image fetch failed after {attempt} attempts: {exc}"
                ) from exc
            except EHException as exc:
                # retryable upstream image failures (CDN 5xx / Cloudflare)
                if exc.retryable and attempt <= self.retries:
                    logger.warning(
                        "image fetch %s failed (%s), retry %d/%d",
                        url, type(exc).__name__, attempt, self.retries,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 3.0))
                    continue
                raise
