"""Unit tests for the EHClient failure detection (mock transport, no network)."""

import httpx
import pytest

from app.config import Settings
from app.eh.client import EHClient
from app.eh.exceptions import (
    BannedError,
    CookieInvalidError,
    EHException,
    EHServerError,
    ExceedLimitError,
    GalleryDeletedError,
)


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc", retries=1)
    base.update(kw)
    return Settings(**base)


def _client(handler) -> EHClient:
    client = EHClient(_settings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=_settings().cookies,
    )
    return client


@pytest.mark.asyncio
async def test_banned_detection():
    def handler(request):
        return httpx.Response(200, text="Your IP address has been banned")

    c = _client(handler)
    with pytest.raises(BannedError):
        await c.get_html("/")
    await c.close()


@pytest.mark.asyncio
async def test_exceed_limit_detection():
    def handler(request):
        return httpx.Response(200, text="You have exceeded your image limits")

    c = _client(handler)
    with pytest.raises(ExceedLimitError):
        await c.get_html("/")
    await c.close()


@pytest.mark.asyncio
async def test_fatal_error_detection():
    def handler(request):
        return httpx.Response(200, text="Page load has been aborted due to a fatal error")

    c = _client(handler)
    with pytest.raises(EHServerError):
        await c.get_html("/")
    await c.close()


@pytest.mark.asyncio
async def test_empty_body_detection():
    c = _client(lambda r: httpx.Response(200, text=""))
    with pytest.raises(CookieInvalidError):
        await c.get_html("/")
    await c.close()


@pytest.mark.asyncio
async def test_404_gallery_deleted():
    c = _client(lambda r: httpx.Response(404, text="Gallery not found"))
    with pytest.raises(GalleryDeletedError):
        await c.fetch_image_bytes("https://ehgt.org/x.jpg")
    await c.close()


@pytest.mark.asyncio
async def test_gallery_not_found_detection():
    c = _client(lambda r: httpx.Response(200, text="Gallery not found. If you just added this gallery"))
    with pytest.raises(GalleryDeletedError):
        await c.get_html("/g/1/tok/")
    await c.close()


@pytest.mark.asyncio
async def test_retry_on_transport_error_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="ok")

    c = _client(handler)
    assert await c.get_html("/") == "ok"
    assert calls["n"] == 2
    await c.close()


@pytest.mark.asyncio
async def test_retryable_eh_server_error_retried_then_success():
    """EHServerError (fatal error page) is retryable: retried with backoff."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, text="Page load has been aborted due to a fatal error"
            )
        return httpx.Response(200, text="ok")

    c = _client(handler)
    assert await c.get_html("/") == "ok"
    assert calls["n"] == 2
    await c.close()


@pytest.mark.asyncio
async def test_cloudflare_403_retried_then_success():
    """403 on an E-Hentai host raises CloudflareError which is retryable."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, text="challenge")
        return httpx.Response(200, text="ok")

    c = _client(handler)
    assert await c.get_html("/") == "ok"
    assert calls["n"] == 2
    await c.close()


@pytest.mark.asyncio
async def test_retry_exhausted_wraps_transport_error():
    """Exhausted transport retries surface as EHException (502), not a raw
    httpx error that would escape to the client as an opaque 500."""
    def handler(request):
        raise httpx.ConnectError("boom")

    c = _client(handler)
    with pytest.raises(EHException):
        await c.get_html("/")
    await c.close()


@pytest.mark.asyncio
async def test_image_cdn_5xx_retried_then_success():
    """CDN 5xx raises retryable EHServerError; error-page bytes are never
    returned as image data."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="<html>bad gateway</html>")
        return httpx.Response(200, content=b"\xff\xd8\xffok")

    c = _client(handler)
    data = await c.fetch_image_bytes("https://ehgt.org/x.jpg")
    assert calls["n"] == 2
    assert data == b"\xff\xd8\xffok"
    await c.close()


@pytest.mark.asyncio
async def test_image_fetch_exhausted_wraps_transport_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    c = _client(handler)
    with pytest.raises(EHException):
        await c.fetch_image_bytes("https://ehgt.org/x.jpg")
    await c.close()


@pytest.mark.asyncio
async def test_non_eh_host_not_checked():
    """Failure detection only applies to e-hentai hosts (e.g. image CDNs)."""
    c = _client(lambda r: httpx.Response(200, text="Your IP address has been banned"))
    # ehgt.org is not an e-hentai host -> passes through
    resp = await c._request("GET", "https://ehgt.org/some/image.jpg")
    assert resp.status_code == 200
    await c.close()
