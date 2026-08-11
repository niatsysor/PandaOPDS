"""Integration test: real E-Hentai closed loop.

Skipped unless IPB_MEMBER_ID/IPB_PASS_HASH are set (or RUN_EH_INTEGRATION=1
with a saved nw=1 cookie, used for public galleries). Saves real HTML fixtures
under tests/fixtures/ on first run for offline regression tests.
"""

import os
from pathlib import Path

import pytest

from app.cache.disk import detect_image_type
from app.config import load_settings
from app.eh.parser import (
    parse_detail_page,
    parse_gdata_response,
    parse_image_page,
    parse_list_page,
)
from app.eh.service import EHService

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _ensure_fixture_dir() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)


def _integration_enabled() -> bool:
    return bool(
        os.getenv("IPB_MEMBER_ID") and os.getenv("IPB_PASS_HASH")
    ) or os.getenv("RUN_EH_INTEGRATION") == "1"


@pytest.mark.skipif(not _integration_enabled(), reason="requires E-Hentai cookies")
@pytest.mark.asyncio
async def test_real_closed_loop():
    settings = load_settings()
    service = EHService(settings)
    _ensure_fixture_dir()
    try:
        # 1) list page
        info = await service.search_galleries()
        assert info.galleries, "no galleries on list page"
        g = info.galleries[0]
        (FIXTURE_DIR / "list_page.html").write_text(await service._html_get("/"), encoding="utf-8")

        # 2) gdata metadata
        meta = await service.get_metadata(g.gid, g.token)
        assert meta is not None and meta.filecount > 0
        (FIXTURE_DIR / "gdata.json").write_text(
            await service.client.post_api(
                {"method": "gdata", "gidlist": [[g.gid, g.token]], "namespace": 1}
            ),
            encoding="utf-8",
        )

        # 3) detail page -> page URLs
        detail = await service.get_detail_page(g.gid, g.token, 0)
        assert detail.thumbnails, "no thumbnails in detail page"
        (FIXTURE_DIR / "detail_page.html").write_text(
            await service._html_get(f"/g/{g.gid}/{g.token}/"), encoding="utf-8"
        )

        # 4) /s/ page -> real image URL
        img_info = await service.resolve_image_page(g.gid, g.token, 1)
        assert not img_info.is_509, "509 placeholder hit during integration test"
        assert img_info.image_url.startswith("http")
        thumb_href = detail.thumbnails[0].href
        save_url = thumb_href if thumb_href.startswith("http") else (
            f"/s/{thumb_href.split('/')[2]}/{g.gid}-1"
        )
        (FIXTURE_DIR / "image_page.html").write_text(
            await service._html_get(save_url),
            encoding="utf-8",
        )

        # 5) image bytes
        data, mime = await service.get_image(g.gid, g.token, 1)
        assert data and detect_image_type(data) == mime
        assert mime in ("image/jpeg", "image/png", "image/gif", "image/webp")
        (FIXTURE_DIR / "image_0.bin").write_bytes(data)

        # 6) cache hit: same request again -> zero upstream (assert via stats)
        html_before = service.throttle.html_requests
        api_before = service.throttle.api_requests
        data2, _ = await service.get_image(g.gid, g.token, 1)
        assert data2 == data
        assert service.throttle.html_requests == html_before
        assert service.throttle.api_requests == api_before
    finally:
        await service.close()


@pytest.mark.skipif(not _integration_enabled(), reason="requires E-Hentai cookies")
@pytest.mark.asyncio
async def test_parser_on_real_fixtures():
    """Parse the saved real fixtures (works offline once fixtures exist)."""
    if not (FIXTURE_DIR / "list_page.html").exists():
        pytest.skip("fixtures not saved yet")
    info = parse_list_page((FIXTURE_DIR / "list_page.html").read_text(encoding="utf-8"))
    assert info.galleries
    g = info.galleries[0]

    detail = parse_detail_page(
        (FIXTURE_DIR / "detail_page.html").read_text(encoding="utf-8"),
        "e-hentai.org",
        0,
    )
    assert detail.thumbnails

    img = parse_image_page((FIXTURE_DIR / "image_page.html").read_text(encoding="utf-8"))
    assert not img.is_509 and img.image_url.startswith("http")

    metas = parse_gdata_response((FIXTURE_DIR / "gdata.json").read_text(encoding="utf-8"))
    assert metas and metas[0].gid == g.gid
