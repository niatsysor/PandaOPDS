"""Internal exception types mapping E-Hentai failure modes.

Each maps to an HTTP status for the OPDS/proxy layer:
- EHException          -> 502 (generic upstream failure)
- BannedError          -> 503 (global circuit breaker trips)
- ExceedLimitError     -> 429 (image limit reached)
- CookieInvalidError   -> 503
- GalleryDeletedError  -> 404
- CloudflareError      -> 503 (with backoff)
- EHServerError        -> 502 (retry with backoff)
"""

from __future__ import annotations


class EHException(Exception):
    """Base class for all E-Hentai upstream errors."""

    status_code: int = 502

    def __init__(self, message: str = "E-Hentai upstream error", *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BannedError(EHException):
    """IP address banned by E-Hentai (body starts with 'Your IP address')."""

    status_code = 503


class ExceedLimitError(EHException):
    """Image limit exceeded (body starts with 'You have exceeded your image')."""

    status_code = 429


class CookieInvalidError(EHException):
    """Empty body -> login required (sadPanda)."""

    status_code = 503


class GalleryDeletedError(EHException):
    """Gallery removed (404 on e-hentai host)."""

    status_code = 404


class PageNotFoundError(EHException):
    """Requested page index is out of range for the gallery."""

    status_code = 404


class CloudflareError(EHException):
    """403 from Cloudflare challenge."""

    status_code = 503


class EHServerError(EHException):
    """E-Hentai internal error (fatal error page)."""

    status_code = 502


class ParseError(EHException):
    """HTML structure changed / unexpected content."""

    status_code = 502

    def __init__(self, message: str, *, raw_html: str | None = None):
        super().__init__(message, retryable=False)
        self.raw_html = raw_html
