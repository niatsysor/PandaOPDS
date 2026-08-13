"""Boundary-case tests (offline): 0-page galleries, expunged metadata,
long galleries (1000+ pages), out-of-range pages, 509 -> 429 mapping.
"""

import pytest

from app.config import Settings
from app.eh.exceptions import (
    BannedError,
    EHServerError,
    ExceedLimitError,
    PageNotFoundError,
)
from app.eh.models import DetailPageInfo
from app.eh.parser import (
    parse_detail_page,
    parse_gdata_response,
    parse_image_page,
)
from app.eh.service import EHService


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc")
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------------
# 0-page gallery
# --------------------------------------------------------------------------

EMPTY_GDT_HTML = """
<html><body>
<div class="gtb"><div class="gpc">Showing 0 - 0 of 0 images</div></div>
<div id="gdt"></div>
</body></html>
"""


# --------------------------------------------------------------------------
# watched / favorites list endpoints
# --------------------------------------------------------------------------


async def test_watched_and_favorites_reuse_list_parser(tmp_path, monkeypatch):
    """watched/favorites galleries reuse the standard list parser (offline)."""
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "list_page.html"
    if not fixture.exists():
        pytest.skip("real HTML fixture not present")

    service = EHService(_settings(cache_dir=tmp_path))
    html = fixture.read_text(encoding="utf-8")

    async def fake_html_get(path: str, params: dict | None = None) -> str:
        assert path in ("/watched", "/favorites.php")
        if params:
            # inline_set is always injected (server-forced list layout);
            # lastGid pagination rides alongside when requested.
            assert params.get("inline_set") == "dm_e"
            if params.get("next"):
                assert params["next"] == "42"
        return html

    monkeypatch.setattr(service, "_html_get", fake_html_get)

    watched = await service.watched_galleries()
    assert len(watched.galleries) == 25
    first = watched.galleries[0]
    assert first.gid and first.token and first.title

    favorites = await service.favorites_galleries(last_gid=42)
    assert len(favorites.galleries) == 25
    assert favorites.next_gid


def test_zero_page_gallery_parses_empty():
    info = parse_detail_page(EMPTY_GDT_HTML, "e-hentai.org", 0)
    assert isinstance(info, DetailPageInfo)
    assert info.thumbnails == []
    assert info.image_count == 0


# --------------------------------------------------------------------------
# expunged gallery metadata
# --------------------------------------------------------------------------

EXPUNGED_GDATA = """
{"gmetadata": [{
  "gid": "1", "token": "t", "title": "Removed", "title_jpn": "",
  "category": "Manga", "thumb": "", "rating": "0",
  "tags": [], "filecount": "0", "filesize": "0", "posted": "0",
  "uploader": "", "torrentcount": "0", "expunged": true
}]}
"""


def test_expunged_gallery_metadata():
    metas = parse_gdata_response(EXPUNGED_GDATA)
    assert len(metas) == 1
    assert metas[0].expunged is True
    assert metas[0].filecount == 0


# --------------------------------------------------------------------------
# long gallery: page numbering across many thumbnail pages
# --------------------------------------------------------------------------

def _detail_html(thumb_count: int, total: int) -> str:
    # MPV-style hrefs (no page number inside) force index-based numbering,
    # exercising the long-gallery fallback path.
    thumbs = "".join(
        f'<a href="/mpv/1/tok/"><div style="width:100px;height:150px;'
        f'background:url(https://ehgt.org/t/{i}.jpg) 0px 0px no-repeat transparent;" '
        f'data-orghash="{"0"*40}"></div></a>'
        for i in range(thumb_count)
    )
    return (
        '<html><body><div id="gdt" class="gdt">' + thumbs + "</div></body></html>"
    )


def test_long_gallery_page_numbering():
    """1000-page gallery: last thumbnail page (index 49) numbers 981..1000."""
    info = parse_detail_page(_detail_html(20, 1000), "e-hentai.org", page_index=49)
    assert len(info.thumbnails) == 20
    assert info.thumbnails[0].page_no == 981
    assert info.thumbnails[-1].page_no == 1000


# --------------------------------------------------------------------------
# service-level: out-of-range page -> PageNotFoundError (404)
# --------------------------------------------------------------------------

def _detail_html_s(thumb_count: int) -> str:
    """Detail page with proper /s/ hrefs (for service-level fakes)."""
    thumbs = "".join(
        f'<a href="/s/tok{i}/{1}-{i+1}"><div style="width:100px;height:150px;'
        f'background:url(https://ehgt.org/t/{i}.jpg) 0px 0px no-repeat transparent;" '
        f'data-orghash="{"0"*40}"></div></a>'
        for i in range(thumb_count)
    )
    return '<html><body><div id="gdt" class="gdt">' + thumbs + "</div></body></html>"


class _FakeClient:
    """Minimal fake upstream client returning a fixed 2-thumbnail detail page."""

    def __init__(self):
        self.calls: list[str] = []

    async def get_html(self, path, params=None, referer=None):
        self.calls.append(path)
        if path.startswith("/g/"):
            return _detail_html_s(2)
        # /s/ page -> image page
        return (
            '<html><body><img id="img" src="https://ehgt.org/x/1.jpg" '
            'style="width:10px;height:10px;"><div id="loadfail" '
            'onclick="return nl(\'rl1\')"></div></body></html>'
        )

    async def post_api(self, payload):
        return '{"gmetadata": []}'

    async def fetch_image_bytes(self, url, referer=None):
        return b"\xff\xd8\xff\xe0 fakejpeg"

    async def establish_session(self):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_out_of_range_page_404(tmp_path):
    svc = EHService(_settings(cache_dir=str(tmp_path)), client=_FakeClient())
    with pytest.raises(PageNotFoundError):
        await svc.get_image(1, "tok", 5)  # only 2 pages exist
    await svc.close()


@pytest.mark.asyncio
async def test_zero_based_mode_rejects_page_0(tmp_path):
    svc = EHService(
        _settings(cache_dir=str(tmp_path), pse_page_base=0),
        client=_FakeClient(),
    )
    # page 0 invalid when 1-based default... here base=0 so page 0 is valid,
    # but page -1 is invalid
    with pytest.raises(PageNotFoundError):
        await svc.get_image(1, "tok", -1)
    await svc.close()


@pytest.mark.asyncio
async def test_default_one_based_rejects_page_0(tmp_path):
    svc = EHService(_settings(cache_dir=str(tmp_path)), client=_FakeClient())
    with pytest.raises(PageNotFoundError):
        await svc.get_image(1, "tok", 0)
    await svc.close()


# --------------------------------------------------------------------------
# 509 placeholder -> ExceedLimitError -> route 429
# --------------------------------------------------------------------------

def test_509_placeholder_detected():
    html = (
        '<html><body><img id="img" src="https://ehgt.org/g/509.gif" '
        'style="width:10px;height:10px;"></body></html>'
    )
    info = parse_image_page(html)
    assert info.is_509 is True


class _FakeClient509(_FakeClient):
    async def get_html(self, path, params=None, referer=None):
        if path.startswith("/g/"):
            return _detail_html_s(2)
        return (
            '<html><body><img id="img" src="https://ehgt.org/g/509.gif" '
            'style="width:10px;height:10px;"></body></html>'
        )


@pytest.mark.asyncio
async def test_509_raises_exceed_limit(tmp_path):
    svc = EHService(_settings(cache_dir=str(tmp_path)), client=_FakeClient509())
    with pytest.raises(ExceedLimitError):
        await svc.get_image(1, "tok", 1)
    await svc.close()


@pytest.mark.asyncio
async def test_509_drops_imgpage_cache(tmp_path):
    """A 509 hit evicts the cached page mapping so a post-quota retry
    re-resolves instead of serving a stale 429 for up to 1h."""
    svc = EHService(_settings(cache_dir=str(tmp_path)), client=_FakeClient509())
    with pytest.raises(ExceedLimitError):
        await svc.get_image(1, "tok", 1)
    # the failed 509 result must not stay cached
    assert await svc.mem.get("imgpage:1:tok:1") is None
    await svc.close()


class _FakeClientNlRetry(_FakeClient):
    """First image fetch fails with a transient EHServerError; the second
    (nl reload-key) attempt succeeds."""

    def __init__(self):
        super().__init__()
        self.image_calls = 0
        self.urls: list[str] = []

    async def fetch_image_bytes(self, url, referer=None):
        self.image_calls += 1
        self.urls.append(url)
        if self.image_calls == 1:
            raise EHServerError("transient image host failure")
        return b"\xff\xd8\xff\xe0 fakejpeg"


@pytest.mark.asyncio
async def test_nl_reload_retry_on_transient_image_failure(tmp_path):
    svc = EHService(_settings(cache_dir=str(tmp_path)), client=_FakeClientNlRetry())
    data, mime = await svc.get_image(1, "tok", 1)
    assert mime == "image/jpeg"
    assert svc.client.image_calls == 2
    # second attempt rides the nl() reload key parsed from the /s/ page
    assert svc.client.urls[1] == "https://ehgt.org/x/1.jpg?nl=rl1"
    await svc.close()


class _FakeClientBanned(_FakeClient):
    """Image host reports a ban; must not trigger the nl reload-key retry."""

    def __init__(self):
        super().__init__()
        self.image_calls = 0

    async def fetch_image_bytes(self, url, referer=None):
        self.image_calls += 1
        raise BannedError("Your IP address has been banned")


@pytest.mark.asyncio
async def test_nl_retry_skipped_for_hard_failures(tmp_path):
    svc = EHService(_settings(cache_dir=str(tmp_path)), client=_FakeClientBanned())
    with pytest.raises(BannedError):
        await svc.get_image(1, "tok", 1)
    assert svc.client.image_calls == 1  # no nl retry for banned
    await svc.close()
