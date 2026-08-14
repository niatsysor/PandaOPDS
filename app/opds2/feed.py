"""OPDS 2.0 (JSON) feed builder.

Navigation and acquisition documents follow OPDS 2.0
(https://drafts.opds.io/opds-2.0/). PSE streaming is exposed as a custom link
rel on each publication (`http://vaemendis.net/opds-pse/stream`) with
`properties.numberOfItems` set to the page count; acquisition links carry the
same property per the spec. Template hrefs — stream `{pageNumber}`, search
`{searchTerms}` — carry `templated: true` (RWPM link semantics) so compliant
clients substitute instead of requesting them literally. hrefs are relative
by default, absolute when PUBLIC_BASE_URL is set.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import Settings

MIME_NAV = "application/opds+json;profile=navigation"
MIME_ACQ = "application/opds+json;profile=acquisition"
MIME_PUBLICATION = "application/opds+json"
MIME_IMAGE = "image/jpeg"
MIME_THUMB = "image/jpeg"

# RWPM (Web Publication Manifest) context — marks the document as a
# Readium-compatible publication (consumed by Stump/Divina readers).
RWPM_CONTEXT = "https://readium.org/webpub-manifest/context.jsonld"

REL_SELF = "self"
REL_START = "start"
REL_SEARCH = "search"
# JSON search: the search link's href carries the {searchTerms} template
# directly (OPDS 2.0 second form), so clients substitute in one step instead
# of fetching an OpenSearch description document first.
SEARCH_TEMPLATE = "/opds/v2.0/gallery?query={searchTerms}"
REL_NEXT = "next"
REL_SUBSECTION = "subsection"
REL_ACQUISITION = "http://opds-spec.org/acquisition"
REL_STREAM = "http://vaemendis.net/opds-pse/stream"
REL_ALTERNATE = "alternate"  # upstream E-Hentai gallery page (shareable)


def _iso(unix_seconds: int | None = None) -> str:
    if unix_seconds:
        return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Opds2Builder:
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

    def serialize(self, doc: dict) -> str:
        return json.dumps(doc, ensure_ascii=False, indent=2)

    # -- shared helpers ----------------------------------------------------

    def _metadata(self, title: str, identifier: str, modified: str) -> dict:
        return {"title": title, "identifier": identifier, "modified": modified}

    def _link(
        self,
        rel: str,
        href: str,
        mime: str = "",
        title: str = "",
        properties: dict | None = None,
        templated: bool | None = None,
    ) -> dict:
        link: dict = {"href": href, "rel": rel}
        if mime:
            link["type"] = mime
        if title:
            link["title"] = title
        if properties:
            link["properties"] = properties
        if templated is None:
            # Auto-detect RFC 6570 URL templates: any href containing `{...}`
            # is a template (stream {pageNumber}, search {searchTerms}) that
            # clients must substitute — never request literally. The explicit
            # override stays available for exotic URLs with literal braces.
            templated = "{" in href and "}" in href
        if templated:
            link["templated"] = True
        return link

    # -- navigation document -----------------------------------------------

    def navigation_document(
        self,
        navigation: list[dict] | None = None,
        groups: list[dict] | None = None,
    ) -> str:
        """Root navigation document.

        `navigation` items are plain ``{"title", "href"}`` dicts rendered
        into the standard ``navigation[]`` array (OPDS 2.0 §2.1).  Each
        entry is a flat Web Publication link object — `title` + `rel`/`href`/
        `type` on the same level (Komga/Stump-compatible; the nested
        metadata/links form is NOT the spec shape and breaks strict clients
        like Stump's zod parser).

        `groups` are pre-built group dicts placed in the standard
        ``groups[]`` array (OPDS 2.0 §2.5). Each group carries its own
        ``metadata``, a ``self`` link pointing at the full collection, and
        an inline ``publications[]`` preview — server-driven multi-section
        home pages without private extensions.
        """
        now = _iso()

        nav_list: list[dict] = []
        if navigation:
            for item in navigation:
                title = item["title"]
                nav_list.append(
                    {
                        "title": title,
                        "href": self.href(item["href"]),
                        "rel": REL_SUBSECTION,
                        "type": MIME_ACQ,
                    }
                )

        doc = {
            "metadata": self._metadata("PandaOPDS", "urn:ehentai:root", now),
            "links": [
                self._link(REL_SELF, self.href("/opds/v2.0"), MIME_NAV, "PandaOPDS"),
                self._link(REL_START, self.href("/opds/v2.0"), MIME_NAV, "PandaOPDS"),
                self._link(
                    REL_SEARCH,
                    self.href(SEARCH_TEMPLATE),
                    MIME_ACQ,
                    "Search",
                ),
            ],
            "navigation": nav_list,
        }
        if groups:
            doc["groups"] = groups
        return self.serialize(doc)

    # -- acquisition document ----------------------------------------------

    def acquisition_document(
        self,
        *,
        title: str,
        identifier: str,
        publications: list[dict],
        self_href: str,
        next_href: str | None = None,
        facets: list[dict] | None = None,
    ) -> str:
        now = _iso()
        doc = {
            "metadata": self._metadata(title, identifier, now),
            "links": [
                self._link(REL_SELF, self.href(self_href), MIME_ACQ, title),
                self._link(REL_START, self.href("/opds/v2.0"), MIME_NAV, "PandaOPDS"),
                self._link(
                    REL_SEARCH,
                    self.href(SEARCH_TEMPLATE),
                    MIME_ACQ,
                    "Search",
                ),
            ],
        }
        if next_href:
            doc["links"].append(self._link(REL_NEXT, next_href, MIME_ACQ, "Next page"))
        if facets:
            doc["facets"] = facets
        doc["publications"] = publications
        return self.serialize(doc)

    # -- facets -----------------------------------------------------------

    def build_category_facets(self, current_category: str = "") -> list[dict]:
        """Build the Category facet group from settings.facets.

        Each entry in settings.facets is (display_name, f_cats_mask).  The
        facet link href uses ``category={name}`` (human-readable); the
        router maps the name back to the mask at request time.
        """
        links: list[dict] = [
            {
                "href": self.href(f"/opds/v2.0/gallery?category={name}"),
                "title": name,
            }
            for name, _mask in self.settings.facets
        ]
        # Prepend an "All" link that clears the category filter.
        links.insert(
            0,
            {
                "href": self.href("/opds/v2.0/gallery"),
                "title": "All",
            },
        )
        return [{"metadata": {"title": "Category"}, "links": links}]

    # -- publications ------------------------------------------------------

    def publication(
        self,
        *,
        gid: int,
        token: str,
        title: str,
        modified: str,
        authors: list[str] | None = None,
        language: str = "",
        description: str = "",
        page_count: int | None = None,
        published: str = "",
        subjects: list[str] | None = None,
        number_of_pages: int | None = None,
        extensions: dict | None = None,
        detail_document: bool = False,
    ) -> dict:
        """Build one publication object.

        Standard fields only for generic clients: `subject` (RWPM) is a flat
        string array of tag texts (Komga-style), `numberOfPages` is the
        RWPM-standard page count. All EH-specific data (rating, Japanese title,
        category, featured-tag styles, ...) lives in `extensions` — the
        project's single private-extension bucket consumed by the first-party
        client.

        Acquisition link layout is driven by ``settings.opds_acq_detail`` plus
        ``detail_document`` (True for the detail document itself):

        * list/root publications in ``direct`` mode (default) → the acquisition
          link points straight at the image stream (image/jpeg,
          {pageNumber}), so clients read with zero second requests. No
          acquisition/stream links when page_count is unknown.
        * list/root publications in ``detail`` mode → the acquisition link
          points at the detail document (MIME_ACQ) for a second-request flow.
        * the detail document always exposes a direct image-stream acquisition
          link (never a self-referencing one) in both modes.
        """
        identifier = f"urn:ehentai:gallery:{gid}:{token}"
        md: dict = {"title": title, "identifier": identifier, "modified": modified}
        if authors:
            md["authors"] = [{"name": n} for n in authors]
            # RWPM uses the singular `author` (array/object); Komga-style
            # `authors` is kept for existing clients. Stump/Readium parsers
            # only look at `author`.
            md["author"] = [{"name": n} for n in authors]
        if language:
            md["language"] = [language]
        if published:
            md["published"] = published
        if description:
            md["description"] = description
        if subjects:
            md["subject"] = subjects
        if number_of_pages:
            md["numberOfPages"] = number_of_pages
        if extensions:
            md["extensions"] = extensions

        page_props: dict | None = None
        if page_count:
            # numberOfItems is a standard OPDS 2.0 property (page count hint).
            # Page base is a first-party convention (default 1-based) and no
            # longer travels in link properties.
            page_props = {"numberOfItems": page_count}
        stream_href = self.href(f"/stream/{gid}/{token}/page/{{pageNumber}}")
        links: list[dict] = []
        direct = detail_document or not self.settings.opds_acq_detail
        if direct:
            # Acquisition points straight at the image stream: zero-round-trip
            # reading for clients that only understand standard rels/types.
            if page_count:
                links.append(
                    self._link(
                        REL_ACQUISITION,
                        stream_href,
                        MIME_IMAGE,
                        title,
                        properties=page_props,
                    )
                )
            if page_count:
                links.append(
                    self._link(
                        REL_STREAM,
                        stream_href,
                        MIME_IMAGE,
                        properties=page_props,
                    )
                )
        else:
            # detail mode: acquisition leads to the detail document; the
            # client performs a second request for full metadata there.
            links.append(
                self._link(
                    REL_ACQUISITION,
                    self.href(f"/opds/v2.0/gallery/{gid}/{token}"),
                    MIME_ACQ,
                    title,
                    properties=page_props,
                )
            )
            if page_count:
                links.append(
                    self._link(
                        REL_STREAM,
                        stream_href,
                        MIME_IMAGE,
                        properties=page_props,
                    )
                )
        # Upstream E-Hentai gallery page: standard `alternate` link (Atom
        # semantics), so clients can share the canonical EH URL without
        # knowing EH_SITE. Appended last — links[0] stays the acquisition
        # link for naive clients.
        # self link: points at the single-publication document. Clients like
        # Stump open details by following `rel="self"`; the target document
        # must be a top-level publication object (RWPM) for their parser.
        links.append(
            self._link(
                REL_SELF,
                self.href(f"/opds/v2.0/gallery/{gid}/{token}/publication"),
                MIME_PUBLICATION,
                title,
            )
        )
        links.append(
            self._link(
                REL_ALTERNATE,
                self.upstream_url(gid, token),
                "text/html",
                self.settings.site_host,
            )
        )
        # Cover/thumbnail goes in the `images` collection (OPDS 2.0 §2.3):
        # visual representations live there, not in `links` (the thumbnail
        # link relation is the OPDS 1.x approach; v1.2 Atom still uses it).
        images = [
            {
                "href": self.href(f"/image/{gid}/{token}/thumb"),
                "type": MIME_THUMB,
            }
        ]
        publication: dict = {
            "context": RWPM_CONTEXT,
            "metadata": md,
            "links": links,
            "images": images,
        }
        if detail_document and page_count:
            # RWPM readingOrder: per-page image URLs so stream readers
            # (Stump Divina etc.) paginate without extra lookups.
            publication["readingOrder"] = self._reading_order(gid, token, page_count)
        return publication

    def _reading_order(self, gid: int, token: str, page_count: int) -> list[dict]:
        """RWPM readingOrder entries for the image stream, 1 entry per page.

        Page numbers run from ``settings.pse_page_base`` (1 by default) for
        ``page_count`` pages; the stream route serves each number directly.
        """
        base = self.settings.pse_page_base
        return [
            {"href": self.href(f"/stream/{gid}/{token}/page/{n}"), "type": MIME_IMAGE}
            for n in range(base, base + page_count)
        ]
