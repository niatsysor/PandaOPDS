"""OPDS 2.0 (JSON) feed builder.

Navigation and acquisition documents follow OPDS 2.0
(https://drafts.opds.io/opds-2.0/). PSE streaming is exposed as a custom link
rel on each publication (`http://vaemendis.net/opds-pse/stream`) with
`properties.numberOfItems` set to the page count; acquisition links carry the
same property per the spec. hrefs are relative by default, absolute when
PUBLIC_BASE_URL is set.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import Settings

MIME_NAV = "application/opds+json;profile=navigation"
MIME_ACQ = "application/opds+json;profile=acquisition"
MIME_THUMB = "image/jpeg"

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
REL_THUMB = "http://opds-spec.org/image/thumbnail"
REL_STREAM = "http://vaemendis.net/opds-pse/stream"


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
    ) -> dict:
        link: dict = {"href": href, "rel": rel}
        if mime:
            link["type"] = mime
        if title:
            link["title"] = title
        if properties:
            link["properties"] = properties
        return link

    # -- navigation document -----------------------------------------------

    def navigation_document(
        self,
        entries: list[tuple[str, str, str] | dict],
        publications: list[dict] | None = None,
        next_href: str | None = None,
    ) -> str:
        """Root navigation document. `entries` are (title, href, summary)
        tuples or dicts with optional `extensions` (v2.0-only private layout
        flags, e.g. ``{"layout": "showcase"}``; v1.2 never carries these).

        When `publications` is provided the document is a hybrid: standard
        top-level ``publications[]`` rendered as a grid by every OPDS 2.0
        client — the universal fallback for clients that ignore the private
        showcase flag. `next_href` is the standard rel="next" continuation
        (points at the Latest list page 2 when publications = Latest).
        """
        now = _iso()

        def _nav_item(item: tuple | dict) -> dict:
            if isinstance(item, dict):
                return item
            title, href, summary = item
            return {"title": title, "href": href, "summary": summary}

        navigation: list[dict] = []
        for item in entries:
            ni = _nav_item(item)
            title = ni["title"]
            md: dict = self._metadata(
                title, f"urn:ehentai:subsection:{title.lower()}", now
            )
            if ni.get("summary"):
                md["description"] = ni["summary"]
            if ni.get("extensions"):
                md["extensions"] = ni["extensions"]
            navigation.append(
                {
                    "metadata": md,
                    "links": [
                        self._link(
                            REL_SUBSECTION, self.href(ni["href"]), MIME_ACQ, title
                        )
                    ],
                }
            )

        doc = {
            "metadata": self._metadata("EHOPDS", "urn:ehentai:root", now),
            "links": [
                self._link(REL_SELF, self.href("/opds/v2.0"), MIME_NAV, "EHOPDS"),
                self._link(REL_START, self.href("/opds/v2.0"), MIME_NAV, "EHOPDS"),
                self._link(
                    REL_SEARCH,
                    self.href(SEARCH_TEMPLATE),
                    MIME_ACQ,
                    "Search",
                ),
            ],
            "navigation": navigation,
        }
        if next_href:
            doc["links"].append(self._link(REL_NEXT, next_href, MIME_ACQ, "Next page"))
        if publications:
            doc["publications"] = publications
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
    ) -> str:
        now = _iso()
        doc = {
            "metadata": self._metadata(title, identifier, now),
            "links": [
                self._link(REL_SELF, self.href(self_href), MIME_ACQ, title),
                self._link(REL_START, self.href("/opds/v2.0"), MIME_NAV, "EHOPDS"),
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
        doc["publications"] = publications
        return self.serialize(doc)

    # -- publications ------------------------------------------------------

    def publication(
        self,
        *,
        gid: int,
        token: str,
        title: str,
        modified: str,
        author: str = "",
        language: str = "",
        description: str = "",
        page_count: int | None = None,
        published: str = "",
        subjects: list[str] | None = None,
        number_of_pages: int | None = None,
        extensions: dict | None = None,
    ) -> dict:
        """Build one publication object.

        Standard fields only for generic clients: `subjects` is a flat string
        array of tag texts (Komga-style), `numberOfPages` is the RWPM-standard
        page count. All EH-specific data (rating, Japanese title, category,
        featured-tag styles, ...) lives in `extensions` — the project's
        single private-extension bucket consumed by the first-party client.
        """
        identifier = f"urn:ehentai:gallery:{gid}:{token}"
        md: dict = {"title": title, "identifier": identifier, "modified": modified}
        if author:
            md["authors"] = [{"name": author}]
        if language:
            md["language"] = [language]
        if published:
            md["published"] = published
        if description:
            md["description"] = description
        if subjects:
            md["subjects"] = subjects
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
        links = [
            self._link(
                REL_THUMB,
                self.href(f"/image/{gid}/{token}/thumb"),
                MIME_THUMB,
            ),
            self._link(
                REL_ACQUISITION,
                self.href(f"/opds/v2.0/gallery/{gid}/{token}"),
                MIME_ACQ,
                title,
                properties=page_props,
            ),
        ]
        if page_count:
            links.append(
                self._link(
                    REL_STREAM,
                    self.href(f"/stream/{gid}/{token}/page/{{pageNumber}}"),
                    "image/jpeg",
                    properties=page_props,
                )
            )
        return {"metadata": md, "links": links}
