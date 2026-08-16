"""Parse author names from E-Hentai gallery titles.

EH naming convention:
    [Author] Clean Title [Language] [Digital]
    (Event) [Circle (Artist)] Clean Title
    [Author1, Author2] Clean Title

The last ``[...]`` before the clean title text is treated as the author
bracket.  Its content is returned **verbatim as a single author string** —
``Group (artist)`` and comma/、 enumerations are never split apart.
"""

from __future__ import annotations

import re

# Strip all bracket/paren content to isolate the clean title.
_BRACKET_RE = re.compile(r"\[.*?\]|\(.*?\)")

# Last [...] before the clean title — this is the author bracket.
_LAST_BRACKET_RE = re.compile(r"\[([^\]]+)\]\s*$")

def parse_title_authors(title: str, category: str = "") -> tuple[str, list[str]]:
    """Parse an EH gallery title into (clean_title, list_of_author_names).

    Parameters
    ----------
    title : str
        Raw title from gdata or list page (e.g. ``[No1r] Yor Forger [AI Generated]``).
    category : str
        Gallery category (``Doujinshi`` / ``Manga`` / …).  Currently not used
        to switch parsing strategies (the unification approach treats all
        names as flat authors), but kept in the signature for future use.

    Returns
    -------
    tuple[str, list[str]]
        ``(clean_title, [author_name, ...])``.
        Authors may be empty if no author bracket is found.
    """
    # 1. Strip ALL bracket/paren groups to isolate the clean title.
    clean_raw = _BRACKET_RE.sub("", title).strip()

    # 2. Find where the raw clean title starts in the original string.
    #    Use raw spacing (pre-collapse) so str.find() matches the original.
    pos = title.find(clean_raw) if clean_raw else -1
    if pos == -1 or pos == 0:
        clean = re.sub(r"\s+", " ", clean_raw) if clean_raw else title
        return (clean, [])

    before = title[:pos]

    # 3. Match the LAST [...] before the clean title (EH author convention).
    m = _LAST_BRACKET_RE.search(before)
    if not m:
        clean = re.sub(r"\s+", " ", clean_raw)
        return (clean, [])

    author_text = m.group(1).strip()
    if not author_text:
        clean = re.sub(r"\s+", " ", clean_raw)
        return (clean, [])

    # 4. Collapse spaces for the final clean title.
    clean = re.sub(r"\s+", " ", clean_raw)

    # 5. Parse names from the author bracket.
    return (clean, _parse_author_names(author_text))


def _parse_author_names(text: str) -> list[str]:
    """Return the author bracket content as a single whole — never split.

    ``Group (artist)`` and comma/、 enumerations stay verbatim as one
    author string; callers treat it as an opaque label.
    """
    return [text]


def parse_detail_title(
    title: str, title_jpn: str, category: str = ""
) -> tuple[str, list[str]]:
    """Detail-document title: prefer the Japanese title as the clean-title
    source; fall back to the default title when titleJpn is missing or
    contains nothing but bracket markers.

    A titleJpn of only markers (e.g. ``[中国翻訳]``) parses back to the raw
    input, so it is treated as missing and the default title wins.
    """
    if title_jpn and _BRACKET_RE.sub("", title_jpn).strip():
        clean, authors = parse_title_authors(title_jpn, category)
        if clean:
            return clean, authors
    return parse_title_authors(title, category)
