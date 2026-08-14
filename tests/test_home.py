"""Home feed + Toplist tests (offline).

Covers:
- ranklist page parsing (`.ptt` page-number pagination fallback)
- EHService.toplist_galleries (period -> `?tl=`, `?p=` pagination)
- v2.0 home: TOML-driven layout (groups + navigation), auth-gated nav
- v1.2 home: pure navigation, auth-gated, no extensions
- /toplist routes (v1.2 + v2.0)
"""

import re
from pathlib import Path

import httpx
import pytest
from lxml import html

from app.config import Settings
from app.eh.exceptions import EHException
from app.eh.models import GalleryListItem, GalleryPageInfo
from app.eh.parser import parse_list_page
from app.eh.service import EHService

FIXTURE = Path(__file__).parent / "fixtures" / "list_page.html"


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc", home_config_path=None)
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

def _gallery_rows(n: int) -> str:
    """First `n` gallery rows from the extended-view fixture (real HTML).

    Rows are extracted via lxml so nested tag rows stay inside their gallery
    row (a plain regex would truncate the table at the first nested
    `</table>`).

    Skipped when the fixtures are absent (fresh clone): `tests/fixtures/`
    holds real E-Hentai page captures and is gitignored by design.
    """
    if not FIXTURE.exists():
        pytest.skip("real HTML fixtures not present")
    doc = html.fromstring(FIXTURE.read_text(encoding="utf-8"))
    tbl = doc.cssselect("table.itg.glte")[0]
    rows = [r for r in tbl.cssselect("tr") if r.cssselect('a[href*="/g/"]')]
    assert len(rows) >= n, f"fixture has {len(rows)} gallery rows, need {n}"
    return "".join(html.tostring(r, encoding="unicode") for r in rows[:n])


def ranklist_html(n: int = 2) -> str:
    """Ranklist page: extended rows + `.ptt` pagination, no `#unext`.

    Mirrors the real site's 0-based `p`: displayed page 1's "Next ›" link
    points at `p=1` (displayed page 2).
    """
    return (
        "<html><body>"
        f'<table class="itg glte">{_gallery_rows(n)}</table>'
        '<div class="ptt"><table><tr>'
        '<td><a href="/toplist.php?tl=15">First</a></td>'
        "<td>1</td><td>2</td>"
        '<td><a href="/toplist.php?tl=15&amp;p=1">Next &gt;</a></td>'
        "</tr></table></div>"
        "</body></html>"
    )


def test_ranklist_page_reuses_extended_parser_and_ptt_pagination():
    """Toplist rows parse through the extended view; pagination comes from the
    `.ptt` next link (`?p=`), not `#unext` lastGid."""
    info = parse_list_page(ranklist_html(2))
    assert len(info.galleries) == 2
    assert info.galleries[0].gid and info.galleries[0].title
    assert info.next_gid is None
    assert info.next_page == 2


def test_ranklist_last_page_has_no_next_page():
    html = (
        "<html><body>"
        f'<table class="itg glte">{_gallery_rows(1)}</table>'
        '<div class="ptt"><table><tr>'
        "<td>1</td><td><b>2</b></td>"
        "</tr></table></div>"
        "</body></html>"
    )
    info = parse_list_page(html)
    assert info.next_gid is None
    assert info.next_page is None


def test_ranklist_next_page_maps_0based_upstream_p():
    """E-Hentai toplist `p` is 0-based (displayed N <-> p=N-1): the "Next ›"
    link on displayed page 3 (p=2) points at p=3, and the OPDS `page` is the
    1-based displayed number, so next_page is 4 — not the raw p."""
    html = (
        "<html><body>"
        f'<table class="itg glte">{_gallery_rows(1)}</table>'
        '<div class="ptt"><table><tr>'
        '<td><a href="/toplist.php?tl=15&amp;p=1">&lt;</a></td>'
        "<td>1</td><td>2</td>"
        '<td class="ptds"><a href="/toplist.php?tl=15&amp;p=2">3</a></td>'
        '<td><a href="/toplist.php?tl=15&amp;p=3">Next &gt;</a></td>'
        "</tr></table></div>"
        "</body></html>"
    )
    info = parse_list_page(html)
    assert info.next_gid is None
    assert info.next_page == 4


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
    assert seen == [
        ("https://e-hentai.org/toplist.php", {"tl": "13", "p": "1", "inline_set": "dm_e"})
    ]
    assert len(info.galleries) == 1

    # page 1 omits the `p` param
    await service.toplist_galleries(period="yesterday")
    assert seen[-1] == (
        "https://e-hentai.org/toplist.php",
        {"tl": "15", "inline_set": "dm_e"},
    )

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
    """Without IPB cookies: default TOML layout — groups carry publication
    previews + in-group navigation; ungrouped navigation lands in root
    navigation; auth-gated sections (favorites) are omitted."""
    settings = _settings(ipb_member_id="", ipb_pass_hash="")
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "search_galleries",
        _async_value(
            GalleryPageInfo(galleries=[_item(1, "One"), _item(2, "Two")], next_gid=999)
        ),
    )
    monkeypatch.setattr(
        service,
        "popular_galleries",
        _async_value(GalleryPageInfo(galleries=[_item(3, "Three")], next_gid=998)),
    )
    monkeypatch.setattr(
        service,
        "toplist_galleries",
        _async_value(GalleryPageInfo(galleries=[_item(4, "Four")], next_gid=997)),
    )
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    assert r.status_code == 200
    doc = r.json()

    # ungrouped navigation sections only; 我的收藏 (favorites) is auth-gated
    nav_titles = [n["title"] for n in doc["navigation"]]
    assert nav_titles == ["历史总榜", "日文原版"]
    assert "Watched" not in nav_titles and "Favorites" not in nav_titles

    # groups merge their sections into one slot (publications + navigation)
    groups = {g["metadata"]["title"]: g for g in doc["groups"]}
    assert set(groups) == {"排行榜", "浏览", "中文同人"}
    assert len(groups["排行榜"]["publications"]) == 1
    assert [n["title"] for n in groups["排行榜"]["navigation"]] == ["月度精选", "年度佳作"]
    assert [n["title"] for n in groups["浏览"]["navigation"]] == ["最新上传"]
    assert len(groups["浏览"]["publications"]) == 1
    assert len(groups["中文同人"]["publications"]) == 2

    # no top-level publications fallback (previews live in groups) / rel=next
    assert "publications" not in doc
    assert "next" not in {l["rel"] for l in doc["links"]}


def _async_value(v):
    import asyncio

    async def _f(*a, **k):
        return v

    return _f


@pytest.mark.asyncio
async def test_opds2_home_with_auth(tmp_path, monkeypatch):
    """With IPB cookies: auth-gated ungrouped navigation sections appear."""
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(service, "search_galleries", _async_value(GalleryPageInfo()))
    monkeypatch.setattr(service, "popular_galleries", _async_value(GalleryPageInfo()))
    monkeypatch.setattr(service, "toplist_galleries", _async_value(GalleryPageInfo()))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    doc = r.json()
    nav_titles = [n["title"] for n in doc["navigation"]]
    assert nav_titles == ["历史总榜", "我的收藏", "日文原版"]


@pytest.mark.asyncio
async def test_opds2_home_custom_toml(tmp_path, monkeypatch):
    """A custom home.toml drives the layout: mixed publication + navigation
    sections merge into one group slot; auth-gated nav is dropped without
    cookies."""
    toml = tmp_path / "home.toml"
    toml.write_text(
        '[[group]]\n'
        'id = "g1"\n'
        'title = "Group One"\n'
        '\n'
        '[[section]]\n'
        'group = "g1"\n'
        'kind = "publication"\n'
        'title = "Pub Preview"\n'
        'type = "search"\n'
        'query = "language:chinese"\n'
        'count = 3\n'
        '\n'
        '[[section]]\n'
        'group = "g1"\n'
        'kind = "navigation"\n'
        'title = "Popular"\n'
        'type = "preset"\n'
        'query = "popular"\n'
        '\n'
        '[[section]]\n'
        'kind = "navigation"\n'
        'title = "Watched"\n'
        'type = "preset"\n'
        'query = "watched"\n',
        encoding="utf-8",
    )
    settings = _settings(ipb_member_id="", ipb_pass_hash="", home_config_path=toml)
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "search_galleries",
        _async_value(GalleryPageInfo(galleries=[_item(1, "One")], next_gid=999)),
    )
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    assert r.status_code == 200
    doc = r.json()
    groups = {g["metadata"]["title"]: g for g in doc["groups"]}
    assert set(groups) == {"Group One"}
    assert len(groups["Group One"]["publications"]) == 1
    assert [n["title"] for n in groups["Group One"]["navigation"]] == ["Popular"]
    # auth-gated ungrouped navigation omitted → empty root navigation
    assert doc["navigation"] == []


@pytest.mark.asyncio
async def test_opds2_home_publication_default_count(tmp_path, monkeypatch):
    """A publication section without `count` falls back to the default
    preview size (10) instead of silently rendering nothing."""
    toml = tmp_path / "home.toml"
    toml.write_text(
        '[[group]]\n'
        'id = "g1"\n'
        'title = "Group One"\n'
        '\n'
        '[[section]]\n'
        'group = "g1"\n'
        'kind = "publication"\n'
        'title = "Pub Preview"\n'
        'type = "preset"\n'
        'query = "toplist:yesterday"\n',
        encoding="utf-8",
    )
    settings = _settings(home_config_path=toml)
    service = EHService(settings)
    items = [_item(i, f"Item {i}") for i in range(1, 13)]
    monkeypatch.setattr(
        service,
        "toplist_galleries",
        _async_value(GalleryPageInfo(galleries=items, next_gid=999)),
    )
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    assert r.status_code == 200
    doc = r.json()
    groups = {g["metadata"]["title"]: g for g in doc["groups"]}
    pubs = groups["Group One"]["publications"]
    assert len(pubs) == 10  # DEFAULT_PUBLICATION_PREVIEW_COUNT
    assert pubs[0]["metadata"]["title"] == "Item 1"


@pytest.mark.asyncio
async def test_opds2_home_publication_explicit_zero_disabled(tmp_path, monkeypatch):
    """Explicit `count = 0` keeps the opt-out: the section is not fetched
    and renders nothing (no `publications` key on the group)."""
    toml = tmp_path / "home.toml"
    toml.write_text(
        '[[group]]\n'
        'id = "g1"\n'
        'title = "Group One"\n'
        '\n'
        '[[section]]\n'
        'group = "g1"\n'
        'kind = "publication"\n'
        'title = "Pub Preview"\n'
        'type = "preset"\n'
        'query = "toplist:yesterday"\n'
        'count = 0\n',
        encoding="utf-8",
    )
    settings = _settings(home_config_path=toml)
    service = EHService(settings)

    async def boom(*a, **k):
        raise AssertionError("count=0 section must not be fetched")

    monkeypatch.setattr(service, "toplist_galleries", boom)
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0")
    assert r.status_code == 200
    doc = r.json()
    groups = {g["metadata"]["title"]: g for g in doc["groups"]}
    assert "publications" not in groups["Group One"]


@pytest.mark.asyncio
async def test_opds2_toplist_route(tmp_path, monkeypatch):
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "toplist_galleries",
        _async_value(GalleryPageInfo(galleries=[_item(7, "Ranked")], next_page=2)),
    )
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/toplist?period=month")
    assert r.status_code == 200
    doc = r.json()
    assert doc["metadata"]["title"] == "E-Hentai: Toplist Past Month"
    assert doc["metadata"]["identifier"] == "urn:ehentai:toplist:month"
    assert len(doc["publications"]) == 1
    next_link = next(l for l in doc["links"] if l["rel"] == "next")
    assert next_link["href"] == "/opds/v2.0/toplist?period=month&page=2"
    # period facets (OPDS 2.0): 4 links, current period marked active
    assert "facets" in doc
    fg = doc["facets"][0]
    assert fg["metadata"]["title"] == "Period"
    flinks = fg["links"]
    assert [l["title"] for l in flinks] == [
        "Yesterday", "Past Month", "Past Year", "All Time",
    ]
    assert [l["active"] for l in flinks] == [False, True, False, False]
    assert all(
        l["href"].startswith("/opds/v2.0/toplist?period=") for l in flinks
    )

    r = await _get("/opds/v2.0/toplist?period=bogus")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_opds_v12_home_no_auth(tmp_path):
    """v1.2 is hard-coded pure navigation (no home.toml): Latest first,
    auth-gated Watched/Favorites dropped without IPB cookies, single
    Toplist entry, trailing Search; no extension markers."""
    from lxml import etree

    settings = _settings(ipb_member_id="", ipb_pass_hash="")
    service = EHService(settings)
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    NS = {"a": "http://www.w3.org/2005/Atom"}
    titles = [e.findtext("a:title", namespaces=NS) for e in root.findall("a:entry", NS)]
    assert titles == ["Latest", "Popular", "Toplist", "Search"]
    # no extension markers in v1.2 entries
    body = r.text
    assert "showcase" not in body and "extensions" not in body


@pytest.mark.asyncio
async def test_opds_v12_home_with_auth(tmp_path):
    from lxml import etree

    settings = _settings()
    service = EHService(settings)
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    NS = {"a": "http://www.w3.org/2005/Atom"}
    titles = [e.findtext("a:title", namespaces=NS) for e in root.findall("a:entry", NS)]
    # Latest first; auth-gated Watched/Favorites appear; exactly one Search
    # entry (regression: the Search link used to be appended twice)
    assert titles == [
        "Latest", "Watched", "Favorites", "Popular", "Toplist", "Search",
    ]
    assert titles.count("Search") == 1


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
    # period facets (OPDS 1.2 standard): 4 links in one facet group, the
    # current period carries opds:activeFacet="true"
    OPDS = "{http://opds-spec.org/2010/catalog}"
    facet_links = [
        l for l in root.findall("a:link", NS)
        if l.get("rel") == "http://opds-spec.org/facet"
    ]
    assert [l.get("title") for l in facet_links] == [
        "Yesterday", "Past Month", "Past Year", "All Time",
    ]
    assert {l.get(f"{OPDS}facetGroup") for l in facet_links} == {"period"}
    active = [
        l for l in facet_links if l.get(f"{OPDS}activeFacet") == "true"
    ]
    assert len(active) == 1 and active[0].get("title") == "Past Year"

    r = await _get("/opds/v1.2/toplist?period=bogus")
    assert r.status_code == 400


# --------------------------------------------------------------------------
# browsing never touches gdata (traditional-crawler mode)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opds2_gallery_browse_never_calls_gdata(tmp_path, monkeypatch):
    """Browsing renders from list-page data only: even if gdata explodes,
    the gallery feed still returns entries with the list-page extension
    subset (category), never the gdata-only keys."""
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "search_galleries",
        _async_value(
            GalleryPageInfo(galleries=[_item(1, "[Author] One")], next_gid=999)
        ),
    )

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called while browsing")

    monkeypatch.setattr(service, "get_metadatas", boom)
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/gallery")
    assert r.status_code == 200
    doc = r.json()
    pubs = doc["publications"]
    assert len(pubs) == 1
    md = pubs[0]["metadata"]
    assert md["title"] == "One"
    assert md["numberOfPages"] == 10
    ext = md["extensions"]
    assert ext["category"] == "Manga"
    assert "titleJpn" not in ext and "sizeBytes" not in ext
    assert "uploader" not in ext


@pytest.mark.asyncio
async def test_opds_v12_gallery_browse_never_calls_gdata(tmp_path, monkeypatch):
    from lxml import etree

    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(
        service,
        "search_galleries",
        _async_value(
            GalleryPageInfo(galleries=[_item(1, "[Author] One")], next_gid=999)
        ),
    )

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called while browsing")

    monkeypatch.setattr(service, "get_metadatas", boom)
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2/gallery")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    NS = {"a": "http://www.w3.org/2005/Atom"}
    titles = [
        e.findtext("a:title", namespaces=NS)
        for e in root.findall("a:entry", NS)
    ]
    assert titles == ["One"]
    # list entries carry the upstream E-Hentai page (shareable, no EH_SITE)
    entry = root.find("a:entry", NS)
    alt = [l for l in entry.findall("a:link", NS) if l.get("rel") == "alternate"]
    assert alt and alt[0].get("href") == "https://e-hentai.org/g/1/tok1/"
    assert alt[0].get("type") == "text/html"


@pytest.mark.asyncio
async def test_get_thumb_url_uses_cover_cache_not_gdata(tmp_path, monkeypatch):
    """Thumbnail resolution prefers the cover cache written by list parses
    and never falls back to gdata."""
    service = EHService(_settings(cache_dir=tmp_path))
    await service.mem.set("cover:1:tok1", "https://ehgt.org/t/1.jpg", 3600)

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called for thumbs")

    monkeypatch.setattr(service, "get_metadata", boom)
    url = await service.get_thumb_url(1, "tok1")
    assert url == "https://ehgt.org/t/1.jpg"


@pytest.mark.asyncio
async def test_get_thumb_url_falls_back_to_detail_page(tmp_path, monkeypatch):
    """Cold cover cache: first thumbnail of the detail page (1 HTML request),
    still without touching gdata."""
    from app.eh.models import DetailPageInfo, GalleryThumbnail

    service = EHService(_settings(cache_dir=tmp_path))

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called for thumbs")

    monkeypatch.setattr(service, "get_metadata", boom)

    async def fake_detail(gid, token, page_index):
        return DetailPageInfo(
            image_no_from=0,
            image_no_to=0,
            image_count=1,
            current_page_no=1,
            page_count=1,
            thumbnails=[
                GalleryThumbnail(
                    href="/s/x/2-1",
                    thumb_url="https://ehgt.org/t/2.jpg",
                    page_no=1,
                )
            ],
        )

    monkeypatch.setattr(service, "get_detail_page", fake_detail)
    url = await service.get_thumb_url(2, "tok2")
    assert url == "https://ehgt.org/t/2.jpg"


# --------------------------------------------------------------------------
# detail documents render from detail-page HTML (zero gdata)
# --------------------------------------------------------------------------

def _detail(gid: int, token: str = "tok1") -> "DetailPageInfo":
    from app.eh.models import DetailPageInfo

    return DetailPageInfo(
        image_no_from=0,
        image_no_to=0,
        image_count=42,
        current_page_no=1,
        page_count=3,
        title=f"[Author] Gallery {gid}",
        title_jpn="テスト",
        category="Manga",
        cover_url="https://ehgt.org/t/x.jpg",
        rating=4.5,
        uploader="up1",
        publish_time="2026-08-12 13:11",
        language="zh",
        filesize_text="12.34 MB",
        torrent_count=1,
    )


@pytest.mark.asyncio
async def test_opds_v12_chapter_feed_from_detail_html(tmp_path, monkeypatch):
    """v1.2 /chapters renders from the detail page (no gdata); the page-URL
    mapping is pre-warmed so the first /stream hits cache."""
    from lxml import etree

    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(service, "get_detail_page", _async_value(_detail(1)))

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called for detail documents")

    monkeypatch.setattr(service, "get_metadata", boom)
    _install_app_state(settings, service)

    r = await _get("/opds/v1.2/gallery/1/tok1/chapters")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    NS = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", NS)
    # detail title prefers titleJpn as the clean-title source
    assert entry.findtext("a:title", namespaces=NS) == "Chapter 1: テスト"
    # "テスト" carries no author bracket -> no <author> element
    assert entry.findtext("a:author/a:name", namespaces=NS) is None
    cat = entry.find("a:category", NS)
    assert cat.get("term") == "Manga"
    stream = [
        l for l in entry.findall("a:link", NS)
        if l.get("rel") == "http://vaemendis.net/opds-pse/stream"
    ]
    assert stream[0].get("{http://vaemendis.net/opds-pse/ns}count") == "42"


@pytest.mark.asyncio
async def test_opds2_gallery_detail_from_detail_html(tmp_path, monkeypatch):
    """v2.0 single-publication document renders full extensions from the
    detail page (titleJpn/sizeBytes/rating/uploader), no gdata."""
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(service, "get_detail_page", _async_value(_detail(1)))

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called for detail documents")

    monkeypatch.setattr(service, "get_metadata", boom)
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/gallery/1/tok1")
    assert r.status_code == 200
    doc = r.json()
    md = doc["publications"][0]["metadata"]
    # detail title prefers titleJpn as the clean-title source
    assert md["title"] == "テスト"
    assert md["language"] == ["zh"]
    assert md["numberOfPages"] == 42
    assert md["published"] == "2026-08-12T13:11:00Z"
    ext = md["extensions"]
    assert ext["rating"] == 4.5
    assert ext["titleJpn"] == "テスト"
    assert ext["uploader"] == "up1"
    assert ext["sizeBytes"] == 12939427  # 12.34 MB
    assert ext["category"] == "Manga"
    assert "expunged" not in ext
    # detail publication: acquisition points at the image stream (never at
    # the detail document itself — no self-referencing loop)
    links = {l["rel"]: l for l in doc["publications"][0]["links"]}
    acq = links["http://opds-spec.org/acquisition"]
    assert acq["href"] == "/stream/1/tok1/page/{pageNumber}"
    assert acq["type"] == "image/jpeg"
    assert acq["properties"]["numberOfItems"] == 42
    # stream href is a {pageNumber} template (templated: true, never literal)
    assert acq["templated"] is True


@pytest.mark.asyncio
async def test_opds2_gallery_publication_rwpm_document(tmp_path, monkeypatch):
    """/gallery/{gid}/{token}/publication returns a top-level RWPM publication
    (the `rel=self` target): metadata + links + images + readingOrder, with
    no acquisition-feed wrapper — the shape Stump's parser expects."""
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(service, "get_detail_page", _async_value(_detail(1)))

    async def boom(*a, **k):
        raise RuntimeError("gdata must not be called for publication documents")

    monkeypatch.setattr(service, "get_metadata", boom)
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/gallery/1/tok1/publication")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/opds+json")
    pub = r.json()
    # top-level publication object, not an acquisition feed
    assert "publications" not in pub
    assert pub["context"] == "https://readium.org/webpub-manifest/context.jsonld"
    md = pub["metadata"]
    assert md["title"] == "テスト"
    # title_jpn carries no author bracket → no author/author fields
    assert "author" not in md and "authors" not in md
    assert md["extensions"]["rating"] == 4.5
    links = {l["rel"]: l for l in pub["links"]}
    # self points at the publication document itself (the URL just fetched)
    assert links["self"]["href"] == "/opds/v2.0/gallery/1/tok1/publication"
    # acquisition is the direct image stream (no self-reference loop)
    assert links["http://opds-spec.org/acquisition"]["href"] == (
        "/stream/1/tok1/page/{pageNumber}"
    )
    assert links["http://opds-spec.org/acquisition"]["templated"] is True
    # self is a concrete document URL — no templated flag
    assert "templated" not in links["self"]
    # readingOrder: one image URL per page, 1-based by default
    order = pub["readingOrder"]
    assert len(order) == 42
    assert order[0]["href"] == "/stream/1/tok1/page/1"
    assert order[-1]["href"] == "/stream/1/tok1/page/42"
    assert all(l["type"] == "image/jpeg" for l in order)


@pytest.mark.asyncio
async def test_opds2_list_mytags_only_highlighted(tmp_path, monkeypatch):
    """extensions.mytags on list feeds: only tags with a style, no status,
    and subject excludes language/artist while keeping the rest."""
    from app.eh.models import GalleryTag, TagStyle

    item = _item(1, "[Author] One")
    item.tags = [
        GalleryTag("language", "english", style=None),
        GalleryTag("artist", "Someone", style=None),
        GalleryTag("female", "netorare", style=TagStyle(background="#0f0")),
        GalleryTag("parody", "Zenless Zone Zero", style=None),
        GalleryTag("male", "uncertain", status="skepticism"),
    ]
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(
        service, "search_galleries",
        _async_value(GalleryPageInfo(galleries=[item], next_gid=999)),
    )
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/gallery")
    assert r.status_code == 200
    md = r.json()["publications"][0]["metadata"]
    # list subject: full set minus language/artist (skepticism kept: balanced)
    assert md["subject"] == [
        "female:netorare", "parody:Zenless Zone Zero", "male:uncertain",
    ]
    ext = md["extensions"]
    assert ext["mytags"] == [
        {
            "namespace": "female",
            "key": "netorare",
            "style": {"background": "#0f0"},
        }
    ]


@pytest.mark.asyncio
async def test_opds2_detail_subject_full_and_no_mytags(tmp_path, monkeypatch):
    """Detail document: full #taglist in subject (incl. language/artist),
    no mytags (list-feeds-only field), incorrect dropped by default filter."""
    from app.eh.models import GalleryTag, TagStyle

    detail = _detail(1)
    detail.tags = [
        GalleryTag("language", "english", style=None),
        GalleryTag("artist", "Someone", style=None),
        GalleryTag("female", "netorare", style=TagStyle(background="#0f0")),
        GalleryTag("parody", "Zenless Zone Zero", style=None),
        GalleryTag("male", "wrong", status="incorrect"),
    ]
    settings = _settings()
    service = EHService(settings)
    monkeypatch.setattr(service, "get_detail_page", _async_value(detail))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/gallery/1/tok1")
    assert r.status_code == 200
    md = r.json()["publications"][0]["metadata"]
    # detail subject: complete taglist (language/artist included), incorrect dropped
    # detail subject: complete taglist (language/artist included), incorrect
    # dropped; highlighted tag sorts first (stable sort keeps original order)
    assert md["subject"] == [
        "female:netorare", "language:english", "artist:Someone",
        "parody:Zenless Zone Zero",
    ]
    assert "mytags" not in md["extensions"]


@pytest.mark.asyncio
async def test_opds2_detail_subject_strict_filter(tmp_path, monkeypatch):
    """TAG_STATUS_FILTER=strict drops skepticism from detail subject too."""
    from app.eh.models import GalleryTag

    detail = _detail(1)
    detail.tags = [
        GalleryTag("female", "a", style=None),
        GalleryTag("male", "b", status="skepticism"),
    ]
    settings = _settings(tag_status_filter="strict")
    service = EHService(settings)
    monkeypatch.setattr(service, "get_detail_page", _async_value(detail))
    _install_app_state(settings, service)

    r = await _get("/opds/v2.0/gallery/1/tok1")
    assert r.status_code == 200
    md = r.json()["publications"][0]["metadata"]
    assert md["subject"] == ["female:a"]
