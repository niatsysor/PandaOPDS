"""Home feed + Toplist tests (offline).

Covers:
- ranklist page parsing (`.ptt` page-number pagination fallback)
- EHService.toplist_galleries (period -> `?tl=`, `?p=` pagination)
- v2.0 home: auth-gated nav, showcase flag + whitelist, top-level Latest
  publications fallback (no Home nav item)
- v1.2 home: pure navigation, auth-gated, no extensions
- /toplist routes (v1.2 + v2.0)
"""

import re
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.eh.exceptions import EHException
from app.eh.models import GalleryListItem, GalleryPageInfo
from app.eh.parser import parse_list_page
from app.eh.service import EHService

FIXTURE = Path(__file__).parent / "fixtures" / "list_page.html"


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc")
    base.update(kw)
    return Settings(**base)


def _item(gid: int, title: str) -> GalleryListItem:
    return GalleryListItem(
        gid=gid,
        token=f"tok{gid}",
        title=title,
        category="Manga",
        cover_url=f"https://s.exhentai.org/t/{gid}.jpg",
        page_count=10,
    )


# --------------------------------------------------------------------------
# ranklist page parsing (`.ptt` pagination fallback)
# --------------------------------------------------------------------------

def _compact_rows(n: int) -> str:
    html = FIXTURE.read_text(encoding="utf-8")
    m = re.search(r'<table class="itg gltc".*?</table>', html, re.S)
    assert m, "fixture has no compact table"
    rows = [r for r in re.findall(r"<tr>.*?</tr>", m.group(0), re.S) if "<th" not in r]
    return "".join(rows[:n])


def ranklist_html(n: int = 2) -> str:
    """Ranklist page: compact rows + `.ptt` pagination, no `#unext`."""
    return (
        "<html><body>"
        f'<table class="itg gltc">{_compact_rows(n)}</table>'
        '<div class="ptt"><table><tr>'
        '<td><a href="/toplist.php?tl=15">First</a></td>'
        "<td>1</td><td>2</td>"
        '<td><a href="/toplist.php?tl=15&amp;p=2">Next &gt;</a></td>'
        "</tr></table></div>"
        "</body></html>"
    )


def test_ranklist_page_reuses_compact_parser_and_ptt_pagination():
    """Toplist rows parse through the compact view; pagination comes from the
    `.ptt` next link (`?p=`), not `#unext` lastGid."""
    info = parse_list_page(ranklist_html(2))
    assert len(info.galleries) == 2
    assert info.galleries[0].gid and info.galleries[0].title
    assert info.next_gid is None
    assert info.next_page == 2


def test_ranklist_last_page_has_no_next_page():
    html = (
        "<html><body>"
        f'<table class="itg gltc">{_compact_rows(1)}</table>'
        '<div class="ptt"><table><tr>'
        "<td>1</td><td><b>2</b></td>"
        "</tr></table></div>"
        "</body></html>"
    )
    info = parse_list_page(html)
    assert info.next_gid is None
    assert info.next_page is None


# --------------------------------------------------------------------------
# EHService.toplist_galleries
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_toplist_galleries_params_and_period(tmp_path, monkeypatch):
    service = EHService(_settings(cache_dir=tmp_path))
    html = ranklist_html(1)
    seen: list[tuple[str, dict | None]] = []

    async def fake_html_get(path: str, params: dict | None = None) -> str:
        seen.append((path, params))
        return html

    monkeypatch.setattr(service, "_html_get", fake_html_get)

    info = await service.toplist_galleries(period="month", page=2)
    assert seen == [("/toplist.php", {"tl": "13", "p": "2"})]
    assert len(info.galleries) == 1

    # page 1 omits the `p` param
    await service.toplist_galleries(period="yesterday")
    assert seen[-1] == ("/toplist.php", {"tl": "15"})

    # invalid period
    with pytest.raises(EHException):
        await service.toplist_galleries(period="bogus")


@pytest.mark.asyncio
async def test_toplist_galleries_cached(tmp_path, monkeypatch):
    """List results are cached: two calls hit upstream only once."""
    service = EHService(_settings(cache_dir=tmp_path))
    html = ranklist_html(1)
    calls = 0

    async def fake_html_get(path: str, params: dict | None = None) -> str:
        nonlocal calls
        calls += 1
        return html

    monkeypatch.setattr(service, "_html_get", fake_html_get)

    await service.toplist_galleries(period="year")
    await service.toplist_galleries(period="year")
    assert calls == 1


# --------------------------------------------------------------------------
# router-level helpers
# --------------------------------------------------------------------------

def _install_app_state(settings: Settings, service: EHService) -> None:
    from app.main import app

    app.state.settings = settings
    app.state.service = service


async def _get(path: str):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_opds2_home_no_auth(tmp_path, monkeypatch):
    """Without IPB cookies: Watched/Favorites omitted, everything else present
    and flagged showcase (default whitelist = all)."""
    settings = _settings(ipb_member_id="", ipb_pass_hash="")
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "search_galleries",
        _async_value(
            GalleryPageInfo(galleries=[_item(1, "One"), _item(2, "Two")], next_gid=999)
        ),
    )
    monkeypatch.setattr(service, "get_metadatas", _async_value([]))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    assert r.status_code == 200
    doc = r.json()

    titles = [n["metadata"]["title"] for n in doc["navigation"]]
    assert "Watched" not in titles
    assert "Favorites" not in titles
    assert "Popular" in titles
    assert [t for t in titles if t.startswith("Toplist")] == [
        "Toplist: Yesterday",
        "Toplist: Past Month",
        "Toplist: Past Year",
        "Toplist: All Time",
    ]
    # default: every present nav item carries the showcase flag
    for n in doc["navigation"]:
        assert n["metadata"]["extensions"] == {"layout": "showcase"}

    # top-level Latest publications fallback + rel=next
    assert len(doc["publications"]) == 2
    rels = {l["rel"] for l in doc["links"]}
    assert "next" in rels
    next_link = next(l for l in doc["links"] if l["rel"] == "next")
    assert next_link["href"] == "/opds/v2.0/gallery?next=999"


def _async_value(v):
    import asyncio

    async def _f(*a, **k):
        return v

    return _f


@pytest.mark.asyncio
async def test_opds2_home_with_auth(tmp_path, monkeypatch):
    """With IPB cookies: Watched/Favorites present and flagged."""
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(service, "search_galleries", _async_value(GalleryPageInfo()))
    monkeypatch.setattr(service, "get_metadatas", _async_value([]))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    doc = r.json()
    titles = [n["metadata"]["title"] for n in doc["navigation"]]
    assert "Watched" in titles and "Favorites" in titles


@pytest.mark.asyncio
async def test_opds2_home_showcase_whitelist(tmp_path, monkeypatch):
    """SHOWCASE_NAV whitelist: only listed items carry the flag."""
    settings = _settings(showcase_nav=["popular"])
    service = EHService(settings)
    monkeypatch.setattr(service, "search_galleries", _async_value(GalleryPageInfo()))
    monkeypatch.setattr(service, "get_metadatas", _async_value([]))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    doc = r.json()
    by_title = {n["metadata"]["title"]: n for n in doc["navigation"]}
    assert by_title["Popular"]["metadata"]["extensions"] == {"layout": "showcase"}
    assert "extensions" not in by_title["Watched"]["metadata"]


@pytest.mark.asyncio
async def test_opds2_toplist_route(tmp_path, monkeypatch):
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "toplist_galleries",
        _async_value(GalleryPageInfo(galleries=[_item(7, "Ranked")], next_page=2)),
    )
    monkeypatch.setattr(service, "get_metadatas", _async_value([]))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/toplist?period=month")
    assert r.status_code == 200
    doc = r.json()
    assert doc["metadata"]["title"] == "E-Hentai: Toplist Past Month"
    assert doc["metadata"]["identifier"] == "urn:ehentai:toplist:month"
    assert len(doc["publications"]) == 1
    next_link = next(l for l in doc["links"] if l["rel"] == "next")
    assert next_link["href"] == "/opds/v2.0/toplist?period=month&page=2"

    r = await _get("/opds/v2.0/toplist?period=bogus")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_opds_v12_home_no_auth(tmp_path):
    """v1.2 stays pure navigation: no extensions, no Home, no publications."""
    from lxml import etree

    settings = _settings(ipb_member_id="", ipb_pass_hash="")
    service = EHService(settings)
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    NS = {"a": "http://www.w3.org/2005/Atom"}
    titles = [e.findtext("a:title", namespaces=NS) for e in root.findall("a:entry", NS)]
    assert "Home" not in titles
    assert "Watched" not in titles and "Favorites" not in titles
    assert "Popular" in titles
    assert "Search" in titles
    assert any(t.startswith("Toplist") for t in titles)
    # no extension markers in v1.2 entries
    body = r.text
    assert "showcase" not in body and "extensions" not in body


@pytest.mark.asyncio
async def test_opds_v12_home_with_auth(tmp_path):
    settings = _settings()
    service = EHService(settings)
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2")
    assert r.status_code == 200
    assert "Watched" in r.text and "Favorites" in r.text


@pytest.mark.asyncio
async def test_opds_v12_toplist_route(tmp_path, monkeypatch):
    from lxml import etree

    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "toplist_galleries",
        _async_value(GalleryPageInfo(galleries=[_item(7, "Ranked")], next_page=2)),
    )
    monkeypatch.setattr(service, "get_metadatas", _async_value([]))
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2/toplist?period=year")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/atom+xml")
    root = etree.fromstring(r.content)
    NS = {"a": "http://www.w3.org/2005/Atom"}
    feed_title = root.findtext("a:title", namespaces=NS)
    assert feed_title == "E-Hentai: Toplist Past Year"
    next_links = [
        l for l in root.findall("a:link", NS) if l.get("rel") == "next"
    ]
    assert next_links and next_links[0].get("href") == (
        "/opds/v1.2/toplist?period=year&page=2"
    )

    r = await _get("/opds/v1.2/toplist?period=bogus")
    assert r.status_code == 400
