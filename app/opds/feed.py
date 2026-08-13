"""OPDS-PSE Atom feed builder (lxml).

Namespace layout follows OPDS-PSE v1.0 (http://vaemendis.net/opds-pse/)
and the Tachidesk/Suwayomi reference implementation. Feeds are emitted with
relative hrefs by default; absolute when PUBLIC_BASE_URL is set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from lxml import etree

from ..config import Settings

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_OPDS = "http://opds-spec.org/2010/catalog"
NS_PSE = "http://vaemendis.net/opds-pse/ns"
NS_OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"

REL_STREAM = "http://vaemendis.net/opds-pse/stream"
REL_THUMB = "http://opds-spec.org/image/thumbnail"
REL_IMAGE = "http://opds-spec.org/image"
REL_ACQUISITION = "http://opds-spec.org/acquisition"
REL_ALTERNATE = "alternate"  # upstream E-Hentai gallery page (shareable)
REL_SUBSECTION = "subsection"
REL_NEXT = "next"
REL_SELF = "self"
REL_START = "start"
REL_SEARCH = "search"

MIME_NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
MIME_ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
MIME_OPEN_SEARCH = "application/opensearchdescription+xml"
MIME_THUMB = "image/jpeg"

NSMAP = {None: NS_ATOM, "pse": NS_PSE, "opds": NS_OPDS, "opensearch": NS_OPENSEARCH}


@dataclass
class FeedLink:
    rel: str
    href: str
    type: str = ""
    title: str = ""
    count: int | None = None  # pse:count (stream links)
    page_number_template: bool = False  # href contains {pageNumber}


@dataclass
class FeedEntry:
    id: str
    title: str
    updated: str
    author: str = ""
    category_term: str = ""
    category_label: str = ""
    summary: str = ""
    links: list[FeedLink] = field(default_factory=list)


def _iso(unix_seconds: int | None = None) -> str:
    if unix_seconds:
        return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FeedBuilder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.public_base_url  # "" -> relative hrefs

    def href(self, path: str) -> str:
        """Prefix a relative path with the public base URL when configured."""
        return f"{self.base}{path}" if self.base else path

    def upstream_url(self, gid: int, token: str) -> str:
        """Canonical upstream gallery page (shareable; matches JHenTai
        ``GalleryUrl.url``). Absolute, driven by EH_SITE — deliberately NOT
        routed through ``href()`` so PUBLIC_BASE_URL never affects it."""
        return f"https://{self.settings.site_host}/g/{gid}/{token}/"

    # -- document scaffolding ---------------------------------------------

    def _feed(self, title: str, updated: str, mime: str) -> etree._Element:
        feed = etree.Element(f"{{{NS_ATOM}}}feed", nsmap=NSMAP)
        etree.SubElement(feed, f"{{{NS_ATOM}}}title").text = title
        etree.SubElement(feed, f"{{{NS_ATOM}}}id").text = f"urn:ehentai:{title}"
        etree.SubElement(feed, f"{{{NS_ATOM}}}updated").text = updated
        etree.SubElement(feed, f"{{{NS_ATOM}}}author").text = "PandaOPDS"
        # feed-level links
        self._link(feed, REL_SELF, self.href("/opds/v1.2"), MIME_NAV, "PandaOPDS")
        self._link(feed, REL_START, self.href("/opds/v1.2"), MIME_NAV, "PandaOPDS")
        self._link(
            feed, REL_SEARCH, self.href("/opds/v1.2/search.xml"), MIME_OPEN_SEARCH, "Search"
        )
        return feed

    def _link(
        self,
        parent: etree._Element,
        rel: str,
        href: str,
        mime: str = "",
        title: str = "",
        count: int | None = None,
    ) -> None:
        link = etree.SubElement(parent, f"{{{NS_ATOM}}}link")
        link.set("rel", rel)
        link.set("href", href)
        if mime:
            link.set("type", mime)
        if title:
            link.set("title", title)
        if count is not None:
            link.set(f"{{{NS_PSE}}}count", str(count))

    def _entry(self, parent: etree._Element, entry: FeedEntry) -> None:
        e = etree.SubElement(parent, f"{{{NS_ATOM}}}entry")
        etree.SubElement(e, f"{{{NS_ATOM}}}id").text = entry.id
        etree.SubElement(e, f"{{{NS_ATOM}}}title").text = entry.title
        etree.SubElement(e, f"{{{NS_ATOM}}}updated").text = entry.updated
        if entry.author:
            a = etree.SubElement(e, f"{{{NS_ATOM}}}author")
            etree.SubElement(a, f"{{{NS_ATOM}}}name").text = entry.author
        if entry.category_term:
            cat = etree.SubElement(e, f"{{{NS_ATOM}}}category")
            cat.set("term", entry.category_term)
            cat.set("label", entry.category_label or entry.category_term)
            cat.set("scheme", "http://e-hentai.org")
        if entry.summary:
            etree.SubElement(e, f"{{{NS_ATOM}}}summary", type="text").text = entry.summary
        for link in entry.links:
            self._link(e, link.rel, link.href, link.type, link.title, link.count)

    def serialize(self, root: etree._Element) -> str:
        return etree.tostring(
            root, encoding="utf-8", xml_declaration=True, pretty_print=True
        ).decode("utf-8")

    # -- root navigation feed ---------------------------------------------

    def root_feed(self, nav_entries: list[tuple[str, str, str]]) -> str:
        """Root navigation feed. `nav_entries` are (title, href, summary)."""
        now = _iso()
        feed = self._feed("PandaOPDS", now, MIME_NAV)
        for title, href, summary in nav_entries:
            entry = FeedEntry(
                id=f"urn:ehentai:subsection:{title.lower()}",
                title=title,
                updated=now,
                summary=summary,
                links=[FeedLink(rel=REL_SUBSECTION, href=self.href(href), type=MIME_ACQ)],
            )
            self._entry(feed, entry)
        return self.serialize(feed)

    # -- OpenSearch description -------------------------------------------

    def open_search(self, gallery_path: str = "/opds/v1.2/gallery") -> str:
        root = etree.Element(
            f"{{{NS_OPENSEARCH}}}OpenSearchDescription",
            nsmap={None: NS_OPENSEARCH},
        )
        etree.SubElement(root, f"{{{NS_OPENSEARCH}}}ShortName").text = "E-Hentai"
        etree.SubElement(root, f"{{{NS_OPENSEARCH}}}Description").text = (
            "Search E-Hentai galleries via PandaOPDS"
        )
        etree.SubElement(root, f"{{{NS_OPENSEARCH}}}InputEncoding").text = "UTF-8"
        url = etree.SubElement(root, f"{{{NS_OPENSEARCH}}}Url")
        url.set("type", MIME_ACQ)
        url.set(
            "template",
            self.href(f"{gallery_path}?query={{searchTerms}}"),
        )
        return self.serialize(root)

    # -- gallery acquisition feed -----------------------------------------

    def gallery_feed(
        self,
        *,
        query: str,
        entries: list[FeedEntry],
        updated: str,
        next_href: str | None = None,
        feed_id: str = "galleries",
        title: str = "E-Hentai Galleries",
    ) -> str:
        feed = self._feed(title, updated, MIME_ACQ)
        if next_href:
            self._link(feed, REL_NEXT, next_href, MIME_ACQ, "Next page")
        for entry in entries:
            self._entry(feed, entry)
        return self.serialize(feed)

    # -- chapter feed (single entry + PSE stream link) ---------------------

    def chapter_feed(
        self,
        *,
        gid: int,
        token: str,
        title: str,
        updated: str,
        author: str = "",
        category_term: str = "",
        category_label: str = "",
        summary: str = "",
        filecount: int,
        thumb_href: str,
    ) -> str:
        feed = self._feed(title, updated, MIME_ACQ)
        entry = FeedEntry(
            id=f"urn:ehentai:gallery:{gid}:{token}",
            title=f"Chapter 1: {title}",
            updated=updated,
            author=author,
            category_term=category_term,
            category_label=category_label,
            summary=summary,
            links=[
                FeedLink(
                    rel=REL_THUMB,
                    href=self.href(thumb_href),
                    type=MIME_THUMB,
                ),
                FeedLink(
                    rel=REL_STREAM,
                    href=self.href(f"/stream/{gid}/{token}/page/{{pageNumber}}"),
                    type="image/jpeg",
                    count=filecount,
                ),
                # Upstream E-Hentai gallery page (Atom ``alternate`` semantics
                # = the entry's original web page); shareable by any client.
                FeedLink(
                    rel=REL_ALTERNATE,
                    href=self.upstream_url(gid, token),
                    type="text/html",
                    title=self.settings.site_host,
                ),
            ],
        )
        self._entry(feed, entry)
        return self.serialize(feed)
