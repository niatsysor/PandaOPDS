"""OPDS 2.0 feed builder tests (offline)."""

import json

from app.config import Settings
from app.opds2.feed import (
    MIME_ACQ,
    MIME_IMAGE,
    MIME_NAV,
    REL_ACQUISITION,
    REL_STREAM,
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
        authors=["uploader"],
        language="Chinese",
        page_count=42,
        published=_iso(1700000000),
        subjects=["female:netorare", "parody:zenless zone zero"],
        number_of_pages=42,
        extensions={
            "rating": 4.0,
            "originalTitle": "[uploader] My Gallery",
            "titleJpn": "テスト",
            "uploader": "uploader",
            "category": "Doujinshi",
            "sizeBytes": 12345,
            "mytags": [
                {
                    "namespace": "female",
                    "key": "netorare",
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
                {"title": "Home", "href": "/opds/v2.0/gallery", "summary": "Latest uploads"},
                {"title": "Watched", "href": "/opds/v2.0/gallery?query=watched", "summary": "Watched galleries"},
                {"title": "Favorites", "href": "/opds/v2.0/gallery?query=favorites", "summary": "Favorite galleries"},
                {"title": "Popular", "href": "/opds/v2.0/gallery?query=popular", "summary": "Popular this week"},
            ]
        )
    )
    assert doc["metadata"]["title"] == "PandaOPDS"
    assert doc["metadata"]["identifier"] == "urn:ehentai:root"
    assert "modified" in doc["metadata"]
    links = {l["rel"]: l for l in doc["links"]}
    assert {"self", "start", "search"} <= set(links)
    # JSON search: href carries the {searchTerms} template directly
    search = links["search"]
    assert search["href"] == "/opds/v2.0/gallery?query={searchTerms}"
    assert search["type"] == MIME_ACQ
    nav_titles = [n["title"] for n in doc["navigation"]]
    assert nav_titles == ["Home", "Watched", "Favorites", "Popular"]
    hrefs = [n["href"] for n in doc["navigation"]]
    assert hrefs == [
        "/opds/v2.0/gallery",
        "/opds/v2.0/gallery?query=watched",
        "/opds/v2.0/gallery?query=favorites",
        "/opds/v2.0/gallery?query=popular",
    ]
    # navigation entries are flat Web Publication links (OPDS 2.0 §2.1,
    # Komga/Stump-compatible): title/href/rel/type on one level — strict
    # clients (e.g. Stump's zod parser) reject the nested metadata/links form.
    assert doc["navigation"][0]["rel"] == "subsection"
    assert doc["navigation"][0]["type"] == MIME_ACQ
    # summary is not part of the flat link shape; it is ignored
    assert "summary" not in doc["navigation"][0]
    assert "metadata" not in doc["navigation"][0]


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
    # standard subject: flat tag strings (Komga-style), no category/scheme
    assert md["subject"] == ["female:netorare", "parody:zenless zone zero"]
    # RWPM-standard page count
    assert md["numberOfPages"] == 42

    # all EH-specific data lives in the single `extensions` bucket
    ext = md["extensions"]
    assert ext["rating"] == 4.0
    assert ext["originalTitle"] == "[uploader] My Gallery"
    assert ext["titleJpn"] == "テスト"
    assert ext["uploader"] == "uploader"
    assert ext["category"] == "Doujinshi"
    assert ext["sizeBytes"] == 12345
    assert ext["mytags"][0]["namespace"] == "female"
    assert ext["mytags"][0]["key"] == "netorare"
    assert ext["mytags"][0]["style"]["background"] == (
        "radial-gradient(#048751,#24A771)"
    )
    # mytags entries never carry status (consumed server-side by the filter)
    assert "status" not in ext["mytags"][0]

    # Cover lives in the `images` collection (OPDS 2.0 §2.3), not in `links`:
    # the thumbnail link relation is the OPDS 1.x approach.
    assert pub["images"] == [{"href": "/image/123/abc/thumb", "type": "image/jpeg"}]
    links = {l["rel"]: l for l in pub["links"]}
    assert "http://opds-spec.org/image/thumbnail" not in links
    # default acquisition mode is `direct`: the acquisition link points
    # straight at the image stream (zero second requests), so clients that
    # only understand standard rels/types can start reading immediately.
    acq = links[REL_ACQUISITION]
    assert acq["href"] == "/stream/123/abc/page/{pageNumber}"
    assert acq["type"] == MIME_IMAGE
    assert acq["properties"]["numberOfItems"] == 42
    stream = links[REL_STREAM]
    assert stream["href"] == "/stream/123/abc/page/{pageNumber}"
    assert stream["type"] == "image/jpeg"
    assert stream["properties"]["numberOfItems"] == 42
    # pageBase was dropped: pages are 1-based by convention
    assert "pageBase" not in stream["properties"]
    assert "pageBase" not in acq["properties"]
    # upstream E-Hentai gallery page (shareable; client never needs EH_SITE)
    alt = links["alternate"]
    assert alt["href"] == "https://e-hentai.org/g/123/abc/"
    assert alt["type"] == "text/html"
    assert alt["title"] == "e-hentai.org"
    # alternate is appended last: links[0] stays the acquisition link
    assert pub["links"][0]["rel"] == REL_ACQUISITION
    # RWPM self link: clients like Stump open details by following it; the
    # target is the single-publication document (top-level publication).
    self_link = links["self"]
    assert self_link["href"] == "/opds/v2.0/gallery/123/abc/publication"
    assert self_link["type"] == "application/opds+json"
    # RWPM context marks the object as a Readium publication
    assert pub["context"] == "https://readium.org/webpub-manifest/context.jsonld"
    # RWPM singular `author` mirrors `authors` (Stump/Readium parsers)
    assert pub["metadata"]["author"] == [{"name": "uploader"}]
    # list publications carry no readingOrder (only the detail doc embeds it)
    assert "readingOrder" not in pub


def test_publication_alternate_link_follows_eh_site_not_public_base():
    """alternate always points at the upstream site (EH_SITE-driven); it is
    absolute and unaffected by PUBLIC_BASE_URL (server-local links still
    honor it)."""
    builder = Opds2Builder(
        _settings(eh_site="exhentai", public_base_url="https://opds.example.com")
    )
    pub = _pub(builder)
    links = {l["rel"]: l for l in pub["links"]}
    alt = links["alternate"]
    assert alt["href"] == "https://exhentai.org/g/123/abc/"
    assert alt["type"] == "text/html"
    # direct mode: acquisition honors PUBLIC_BASE_URL and points at the stream
    assert links[REL_ACQUISITION]["href"] == (
        "https://opds.example.com/stream/123/abc/page/{pageNumber}"
    )


def test_publication_detail_mode_acquisition_points_at_detail_document():
    """OPDS_ACQ_MODE=detail: list publications expose the detail document as
    the acquisition target (second-request flow); stream stays alongside."""
    builder = Opds2Builder(_settings(opds_acq_mode="detail"))
    pub = _pub(builder)
    links = {l["rel"]: l for l in pub["links"]}
    acq = links[REL_ACQUISITION]
    assert acq["href"] == "/opds/v2.0/gallery/123/abc"
    assert acq["type"] == MIME_ACQ
    assert acq["properties"]["numberOfItems"] == 42
    stream = links[REL_STREAM]
    assert stream["href"] == "/stream/123/abc/page/{pageNumber}"
    assert stream["type"] == MIME_IMAGE
    # acquisition stays links[0] for naive clients
    assert pub["links"][0]["rel"] == REL_ACQUISITION


def test_publication_detail_document_never_self_referencing():
    """The detail document always exposes a direct image-stream acquisition
    link (never a self-referencing one) — in both modes."""
    for mode in ("direct", "detail"):
        builder = Opds2Builder(_settings(opds_acq_mode=mode))
        pub = _pub(builder, detail_document=True)
        links = {l["rel"]: l for l in pub["links"]}
        acq = links[REL_ACQUISITION]
        assert acq["href"] == "/stream/123/abc/page/{pageNumber}"
        assert acq["type"] == MIME_IMAGE
        assert acq["properties"]["numberOfItems"] == 42
        # the document never points at itself
        assert acq["href"] != "/opds/v2.0/gallery/123/abc"
        assert REL_STREAM in links
        # detail publications embed the RWPM readingOrder (per-page image
        # URLs) so stream readers (Stump Divina) paginate without lookups
        order = pub["readingOrder"]
        assert len(order) == 42
        assert order[0] == {"href": "/stream/123/abc/page/1", "type": "image/jpeg"}
        assert order[-1] == {"href": "/stream/123/abc/page/42", "type": "image/jpeg"}
        assert all(link["type"] == "image/jpeg" for link in order)
        assert "alternate" in links


def test_publication_standard_fields_omitted_when_unset():
    builder = Opds2Builder(_settings())
    pub = builder.publication(gid=1, token="t", title="T", modified=_iso())
    md = pub["metadata"]
    assert "subject" not in md
    assert "numberOfPages" not in md
    assert "extensions" not in md
    assert "authors" not in md
    assert "language" not in md
    assert "description" not in md


def test_publication_page_base_zero():
    # PSE_PAGE_BASE no longer travels in link properties: it is a first-party
    # deployment convention (default 1-based). Nothing in the document changes.
    builder = Opds2Builder(_settings(pse_page_base=0))
    pub = _pub(builder)
    links = {l["rel"]: l for l in pub["links"]}
    assert links[REL_STREAM]["properties"] == {"numberOfItems": 42}
    assert links[REL_ACQUISITION]["properties"] == {"numberOfItems": 42}


def test_publication_no_page_count_omits_stream():
    """Unknown page count: direct mode omits the acquisition/stream links
    entirely (they need {pageNumber} + a page count); self/alternate stay."""
    builder = Opds2Builder(_settings())
    pub = _pub(builder, page_count=None)
    rels = {l["rel"] for l in pub["links"]}
    assert REL_STREAM not in rels
    assert REL_ACQUISITION not in rels
    # self/alternate are unconditional (self = detail entry, alternate = share)
    assert "self" in rels
    assert "alternate" in rels
    # no readingOrder without a page count either
    assert "readingOrder" not in pub


def test_publication_no_page_count_detail_mode_keeps_acquisition():
    """Unknown page count in detail mode: the acquisition link (→ detail
    document) survives without numberOfItems; stream is omitted."""
    builder = Opds2Builder(_settings(opds_acq_mode="detail"))
    pub = _pub(builder, page_count=None)
    rels = {l["rel"] for l in pub["links"]}
    assert REL_STREAM not in rels
    acq = [l for l in pub["links"] if l["rel"] == REL_ACQUISITION][0]
    assert acq["href"] == "/opds/v2.0/gallery/123/abc"
    assert acq["type"] == MIME_ACQ
    assert "properties" not in acq
    assert "alternate" in rels


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


# -- facets -----------------------------------------------------------------


def test_acquisition_document_with_facets():
    builder = Opds2Builder(_settings())
    pub = _pub(builder)
    facets = builder.build_category_facets()
    doc = _load(
        builder.acquisition_document(
            title="E-Hentai: Latest",
            identifier="urn:ehentai:gallery-list:latest",
            publications=[pub],
            self_href="/opds/v2.0/gallery",
            facets=facets,
        )
    )
    assert "facets" in doc
    fg = doc["facets"][0]
    assert fg["metadata"]["title"] == "Category"
    links = fg["links"]
    # First link is "All" (no category param), then the 10 default categories.
    assert links[0]["title"] == "All"
    assert links[0]["href"] == "/opds/v2.0/gallery"
    assert len(links) == 11  # All + 10 categories
    # Spot-check a few category facet links.
    titles = {l["title"] for l in links}
    assert "Doujinshi" in titles
    assert "Manga" in titles
    assert "Western" in titles
    dou = next(l for l in links if l["title"] == "Doujinshi")
    assert dou["href"] == "/opds/v2.0/gallery?category=Doujinshi"


def test_facets_not_emitted_when_not_provided():
    builder = Opds2Builder(_settings())
    pub = _pub(builder)
    doc = _load(
        builder.acquisition_document(
            title="Test",
            identifier="urn:test",
            publications=[pub],
            self_href="/opds/v2.0/gallery",
        )
    )
    assert "facets" not in doc


def test_build_category_facets_custom_config():
    """With a custom FACETS list, only those entries appear."""
    custom = [("日系", 7), ("Western", 991)]
    builder = Opds2Builder(_settings(facets=custom))
    facets = builder.build_category_facets()
    links = facets[0]["links"]
    assert links[0]["title"] == "All"
    assert links[1]["title"] == "日系"
    assert links[1]["href"] == "/opds/v2.0/gallery?category=日系"
    assert links[2]["title"] == "Western"
    assert len(links) == 3  # All + 2 custom


def test_facet_links_with_public_base_url():
    builder = Opds2Builder(_settings(public_base_url="https://opds.example.com"))
    facets = builder.build_category_facets()
    links = facets[0]["links"]
    assert links[0]["href"] == "https://opds.example.com/opds/v2.0/gallery"
    dou = next(l for l in links if l["title"] == "Doujinshi")
    assert dou["href"] == "https://opds.example.com/opds/v2.0/gallery?category=Doujinshi"


# -- absolute URLs ----------------------------------------------------------


def test_absolute_urls_when_public_base_set():
    builder = Opds2Builder(_settings(public_base_url="https://opds.example.com"))
    doc = _load(builder.navigation_document([{"title": "Latest", "href": "/opds/v2.0/gallery", "summary": ""}]))
    assert doc["links"][0]["href"] == "https://opds.example.com/opds/v2.0"
    # flat navigation entries honor PUBLIC_BASE_URL too
    assert doc["navigation"][0]["href"] == (
        "https://opds.example.com/opds/v2.0/gallery"
    )
    pub = _pub(builder)
    stream = [l for l in pub["links"] if l["rel"] == REL_STREAM][0]
    assert (
        stream["href"]
        == "https://opds.example.com/stream/123/abc/page/{pageNumber}"
    )


# -- comment gallery-link rewriting ----------------------------------------

from app.eh.models import GalleryComment
from app.opds2.router import _comment_payload


_COMMENT_HTML = (
    '<div class="c6" id="comment_0">'
    '<a href="https://e-hentai.org/g/867387/3a0d9903d3/">https://e-hentai.org/g/867387/3a0d9903d3/</a>'
    ' <a href="https://exhentai.org/g/123456/aabbcc/?p=2#comments">ex page</a>'
    ' <a href="https://e-hentai.org/mpv/98765/fedcba/">mpv</a>'
    ' <a href="https://e-hentai.org/uploader/gvc051126">uploader</a>'
    ' <a href="https://forums.e-hentai.org/index.php?showuser=685825">forum</a>'
    ' <a href="https://example.com/x">external</a>'
    "</div>"
)


def _comment(**kw) -> GalleryComment:
    base = dict(id=0, username="u", time="2026-08-12 13:11", content_html=_COMMENT_HTML)
    base.update(kw)
    return GalleryComment(**base)


def test_comment_payload_preserves_html_without_href():
    """Without an href() helper the content is passed through verbatim."""
    item = _comment_payload(_comment())
    assert item["content"] == _COMMENT_HTML


def test_comment_gallery_links_rewritten_relative():
    item = _comment_payload(_comment(), href=lambda p: p)
    content = item["content"]
    # /g/ links (e-hentai + exhentai) rewrite to the OPDS detail doc; query/fragment dropped
    assert (
        'href="/opds/v2.0/gallery/867387/3a0d9903d3"'
        in content
    )
    assert 'href="/opds/v2.0/gallery/123456/aabbcc"' in content
    # /mpv/ viewer links map to the same detail doc
    assert 'href="/opds/v2.0/gallery/98765/fedcba"' in content
    # non-gallery links stay verbatim
    assert 'href="https://e-hentai.org/uploader/gvc051126"' in content
    assert 'href="https://forums.e-hentai.org/index.php?showuser=685825"' in content
    assert 'href="https://example.com/x"' in content
    # anchor text untouched
    assert "https://e-hentai.org/g/867387/3a0d9903d3/</a>" in content


def test_comment_gallery_links_rewritten_absolute():
    item = _comment_payload(
        _comment(),
        href=Opds2Builder(
            _settings(public_base_url="https://opds.example.com")
        ).href,
    )
    assert (
        'href="https://opds.example.com/opds/v2.0/gallery/867387/3a0d9903d3"'
        in item["content"]
    )
