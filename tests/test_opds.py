"""OPDS-PSE spec compliance tests for the feed builder (offline)."""

from lxml import etree

from app.config import Settings
from app.opds.feed import (
    MIME_ACQ,
    NS_ATOM,
    NS_PSE,
    REL_STREAM,
    REL_THUMB,
    FeedBuilder,
    FeedEntry,
    FeedLink,
    _iso,
)


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc")
    base.update(kw)
    return Settings(**base)


def _parse(xml: str) -> etree._Element:
    return etree.fromstring(xml.encode())


def test_root_feed_structure():
    xml = FeedBuilder(_settings()).root_feed()
    root = _parse(xml)
    assert root.tag == f"{{{NS_ATOM}}}feed"
    assert root.find(f"{{{NS_ATOM}}}title").text == "EHOPDS"
    entries = root.findall(f"{{{NS_ATOM}}}entry")
    titles = [e.find(f"{{{NS_ATOM}}}title").text for e in entries]
    assert titles == ["Latest", "Popular", "Search"]
    # search link present
    search_links = [
        l for l in root.findall(f"{{{NS_ATOM}}}link")
        if l.get("rel") == "search"
    ]
    assert search_links and search_links[0].get("href") == "/opds/search.xml"


def test_open_search_template():
    xml = FeedBuilder(_settings()).open_search()
    root = _parse(xml)
    url = root.find("{http://a9.com/-/spec/opensearch/1.1/}Url")
    assert "{searchTerms}" in url.get("template")
    assert url.get("type") == MIME_ACQ


def test_gallery_feed_entries_and_pagination():
    builder = FeedBuilder(_settings())
    entries = [
        FeedEntry(
            id="urn:ehentai:gallery:1:tok",
            title="Gallery One",
            updated=_iso(1700000000),
            author="uploader",
            category_term="Manga",
            category_label="Manga",
            summary="Pages: 10",
            links=[
                FeedLink(rel="http://opds-spec.org/acquisition",
                         href="/opds/gallery/1/tok/chapters", type=MIME_ACQ),
                FeedLink(rel=REL_STREAM,
                         href="/stream/1/tok/page/{pageNumber}",
                         type="image/jpeg", count=10),
            ],
        )
    ]
    xml = builder.gallery_feed(
        query="test", entries=entries, updated=_iso(), next_href="/opds/gallery?next=999&query=test"
    )
    root = _parse(xml)
    next_links = [l for l in root.findall(f"{{{NS_ATOM}}}link") if l.get("rel") == "next"]
    assert next_links and next_links[0].get("href") == "/opds/gallery?next=999&query=test"
    entry = root.find(f"{{{NS_ATOM}}}entry")
    assert entry.find(f"{{{NS_ATOM}}}id").text == "urn:ehentai:gallery:1:tok"
    cat = entry.find(f"{{{NS_ATOM}}}category")
    assert cat.get("term") == "Manga" and cat.get("scheme") == "http://e-hentai.org"
    acq = [l for l in entry.findall(f"{{{NS_ATOM}}}link") if l.get("rel") == "http://opds-spec.org/acquisition"]
    assert acq[0].get("href") == "/opds/gallery/1/tok/chapters"
    # gallery entries also carry the PSE stream link (clients register chapters
    # directly from the list feed, e.g. Kasane)
    stream = [l for l in entry.findall(f"{{{NS_ATOM}}}link") if l.get("rel") == REL_STREAM]
    assert len(stream) == 1
    assert stream[0].get("href") == "/stream/1/tok/page/{pageNumber}"
    assert stream[0].get(f"{{{NS_PSE}}}count") == "10"


def test_chapter_feed_pse_stream_link():
    builder = FeedBuilder(_settings())
    xml = builder.chapter_feed(
        gid=123,
        token="abc",
        title="My Gallery",
        updated=_iso(1700000000),
        author="uploader",
        category_term="Doujinshi",
        category_label="Doujinshi",
        summary="Pages: 42",
        filecount=42,
        thumb_href="/image/123/abc/thumb",
    )
    root = _parse(xml)
    stream = [
        l for l in root.findall(f".//{{{NS_ATOM}}}link")
        if l.get("rel") == REL_STREAM
    ]
    assert len(stream) == 1
    link = stream[0]
    # pse:count attribute in the PSE namespace
    assert link.get(f"{{{NS_PSE}}}count") == "42"
    # href carries the {pageNumber} placeholder
    assert link.get("href") == "/stream/123/abc/page/{pageNumber}"
    assert link.get("type") == "image/jpeg"
    thumb = [l for l in root.findall(f".//{{{NS_ATOM}}}link") if l.get("rel") == REL_THUMB]
    assert thumb and thumb[0].get("href") == "/image/123/abc/thumb"
    # entry title convention
    entry = root.find(f"{{{NS_ATOM}}}entry")
    assert entry.find(f"{{{NS_ATOM}}}title").text == "Chapter 1: My Gallery"


def test_absolute_urls_when_public_base_set():
    builder = FeedBuilder(_settings(public_base_url="https://opds.example.com"))
    xml = builder.chapter_feed(
        gid=1, token="t", title="T", updated=_iso(),
        filecount=5, thumb_href="/image/1/t/thumb",
    )
    root = _parse(xml)
    stream = [l for l in root.findall(f".//{{{NS_ATOM}}}link") if l.get("rel") == REL_STREAM]
    assert stream[0].get("href") == "https://opds.example.com/stream/1/t/page/{pageNumber}"
    thumb = [l for l in root.findall(f".//{{{NS_ATOM}}}link") if l.get("rel") == REL_THUMB]
    assert thumb[0].get("href") == "https://opds.example.com/image/1/t/thumb"
