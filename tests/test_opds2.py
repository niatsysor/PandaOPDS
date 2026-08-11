"""OPDS 2.0 feed builder tests (offline)."""

import json

from app.config import Settings
from app.opds2.feed import (
    MIME_ACQ,
    MIME_NAV,
    REL_ACQUISITION,
    REL_STREAM,
    REL_THUMB,
    Opds2Builder,
    _iso,
)


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc")
    base.update(kw)
    return Settings(**base)


def _load(xml: str) -> dict:
    return json.loads(xml)


def _pub(builder: Opds2Builder, **kw) -> dict:
    base = dict(
        gid=123,
        token="abc",
        title="My Gallery",
        modified=_iso(1700000000),
        author="uploader",
        language="Chinese",
        description="Pages: 42",
        page_count=42,
        published=_iso(1700000000),
        subjects=["female:netorare", "parody:zenless zone zero"],
        number_of_pages=42,
        extensions={
            "rating": 4.0,
            "titleJpn": "テスト",
            "category": "Doujinshi",
            "sizeBytes": 12345,
            "tags": [
                {
                    "namespace": "female",
                    "key": "netorare",
                    "status": "confidence",
                    "style": {
                        "color": "#f1f1f1",
                        "borderColor": "#048751",
                        "background": "radial-gradient(#048751,#24A771)",
                    },
                }
            ],
        },
    )
    base.update(kw)
    return builder.publication(**base)


# -- navigation document ----------------------------------------------------


def test_navigation_document():
    builder = Opds2Builder(_settings())
    doc = _load(
        builder.navigation_document(
            [
                ("Home", "/opds/v2.0/gallery", "Latest uploads"),
                ("Watched", "/opds/v2.0/gallery?query=watched", "Watched galleries"),
                ("Favorites", "/opds/v2.0/gallery?query=favorites", "Favorite galleries"),
                ("Popular", "/opds/v2.0/gallery?query=popular", "Popular this week"),
            ]
        )
    )
    assert doc["metadata"]["title"] == "EHOPDS"
    assert doc["metadata"]["identifier"] == "urn:ehentai:root"
    assert "modified" in doc["metadata"]
    links = {l["rel"]: l for l in doc["links"]}
    assert {"self", "start", "search"} <= set(links)
    # JSON search: href carries the {searchTerms} template directly
    search = links["search"]
    assert search["href"] == "/opds/v2.0/gallery?query={searchTerms}"
    assert search["type"] == MIME_ACQ
    nav_titles = [n["metadata"]["title"] for n in doc["navigation"]]
    assert nav_titles == ["Home", "Watched", "Favorites", "Popular"]
    hrefs = [n["links"][0]["href"] for n in doc["navigation"]]
    assert hrefs == [
        "/opds/v2.0/gallery",
        "/opds/v2.0/gallery?query=watched",
        "/opds/v2.0/gallery?query=favorites",
        "/opds/v2.0/gallery?query=popular",
    ]
    assert doc["navigation"][0]["links"][0]["rel"] == "subsection"


# -- publications -----------------------------------------------------------


def test_publication_metadata_and_links():
    builder = Opds2Builder(_settings())
    pub = _pub(builder)
    md = pub["metadata"]
    assert md["title"] == "My Gallery"
    assert md["identifier"] == "urn:ehentai:gallery:123:abc"
    assert md["authors"] == [{"name": "uploader"}]
    assert md["language"] == ["Chinese"]
    assert md["published"] == "2023-11-14T22:13:20Z"
    # standard subjects: flat tag strings (Komga-style), no category/scheme
    assert md["subjects"] == ["female:netorare", "parody:zenless zone zero"]
    # RWPM-standard page count
    assert md["numberOfPages"] == 42

    # all EH-specific data lives in the single `extensions` bucket
    ext = md["extensions"]
    assert ext["rating"] == 4.0
    assert ext["titleJpn"] == "テスト"
    assert ext["category"] == "Doujinshi"
    assert ext["sizeBytes"] == 12345
    assert ext["tags"][0]["namespace"] == "female"
    assert ext["tags"][0]["key"] == "netorare"
    assert ext["tags"][0]["style"]["background"] == (
        "radial-gradient(#048751,#24A771)"
    )

    links = {l["rel"]: l for l in pub["links"]}
    assert links[REL_THUMB]["href"] == "/image/123/abc/thumb"
    acq = links[REL_ACQUISITION]
    assert acq["href"] == "/opds/v2.0/gallery/123/abc"
    assert acq["type"] == MIME_ACQ
    assert acq["properties"]["numberOfItems"] == 42
    stream = links[REL_STREAM]
    assert stream["href"] == "/stream/123/abc/page/{pageNumber}"
    assert stream["type"] == "image/jpeg"
    assert stream["properties"]["numberOfItems"] == 42
    # pageBase was dropped: pages are 1-based by convention
    assert "pageBase" not in stream["properties"]
    assert "pageBase" not in acq["properties"]


def test_publication_standard_fields_omitted_when_unset():
    builder = Opds2Builder(_settings())
    pub = builder.publication(gid=1, token="t", title="T", modified=_iso())
    md = pub["metadata"]
    assert "subjects" not in md
    assert "numberOfPages" not in md
    assert "extensions" not in md
    assert "authors" not in md
    assert "language" not in md


def test_publication_page_base_zero():
    # PSE_PAGE_BASE no longer travels in link properties: it is a first-party
    # deployment convention (default 1-based). Nothing in the document changes.
    builder = Opds2Builder(_settings(pse_page_base=0))
    pub = _pub(builder)
    links = {l["rel"]: l for l in pub["links"]}
    assert links[REL_STREAM]["properties"] == {"numberOfItems": 42}
    assert links[REL_ACQUISITION]["properties"] == {"numberOfItems": 42}


def test_publication_no_page_count_omits_stream():
    builder = Opds2Builder(_settings())
    pub = _pub(builder, page_count=None)
    rels = {l["rel"] for l in pub["links"]}
    assert REL_STREAM not in rels
    assert "properties" not in pub["links"][1]


def test_publication_chinese_title_ascii_safe():
    builder = Opds2Builder(_settings())
    pub = builder.publication(gid=1, token="t", title="中文标题", modified=_iso())
    assert pub["metadata"]["title"] == "中文标题"
    # JSON serialization must not escape non-ASCII
    assert "中文标题" in builder.serialize(pub)


# -- acquisition document ---------------------------------------------------


def test_acquisition_document_structure():
    builder = Opds2Builder(_settings())
    pub = _pub(builder)
    doc = _load(
        builder.acquisition_document(
            title="E-Hentai: Latest",
            identifier="urn:ehentai:gallery-list:latest",
            publications=[pub],
            self_href="/opds/v2.0/gallery",
            next_href="/opds/v2.0/gallery?next=999",
        )
    )
    assert doc["metadata"]["title"] == "E-Hentai: Latest"
    assert doc["publications"][0]["metadata"]["identifier"] == "urn:ehentai:gallery:123:abc"
    rels = {l["rel"] for l in doc["links"]}
    assert {"self", "start", "search", "next"} <= rels


# -- absolute URLs ----------------------------------------------------------


def test_absolute_urls_when_public_base_set():
    builder = Opds2Builder(_settings(public_base_url="https://opds.example.com"))
    doc = _load(builder.navigation_document([("Latest", "/opds/v2.0/gallery", "")]))
    assert doc["links"][0]["href"] == "https://opds.example.com/opds/v2.0"
    pub = _pub(builder)
    stream = [l for l in pub["links"] if l["rel"] == REL_STREAM][0]
    assert (
        stream["href"]
        == "https://opds.example.com/stream/123/abc/page/{pageNumber}"
    )
