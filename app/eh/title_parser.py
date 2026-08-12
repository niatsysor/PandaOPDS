"""Parse author names from E-Hentai gallery titles.

EH naming convention (per JHenTai reference):
    [Author] Clean Title [Language] [Digital]
    (Event) [Circle (Artist)] Clean Title
    [Author1, Author2] Clean Title

The last ``[...]`` before the clean title text is treated as the author
bracket.  All names extracted from it are returned as a flat list.
"""

from __future__ import annotations

import re

# Strip all bracket/paren content to isolate the clean title.
_BRACKET_RE = re.compile(r"\[.*?\]|\(.*?\)")

# Last [...] before the clean title — this is the author bracket.
_LAST_BRACKET_RE = re.compile(r"\[([^\]]+)\]\s*$")

# "Name (Role)" pattern inside the author bracket.
_NAME_ROLE_RE = re.compile(r"^(.+?)\s*\((.+?)\)\s*$")


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
    """Extract author names from bracket content.

    Handles:
    - ``Name (Role)`` → ``["Name", "Role"]``  (circle / artist convention)
    - ``Name1, Name2`` → ``["Name1", "Name2"]``
    - ``Name1、Name2`` → ``["Name1", "Name2"]``
    - Plain ``Name`` → ``["Name"]``
    """
    # Try "Name (Role)" pattern first.
    m = _NAME_ROLE_RE.match(text)
    if m:
        names = [m.group(1).strip(), m.group(2).strip()]
    else:
        names = [text]

    # Further split each part by common separators.
    result: list[str] = []
    for name in names:
        split = False
        for sep in ("、", ",", "，"):
            if sep in name:
                result.extend(s.strip() for s in name.split(sep) if s.strip())
                split = True
                break
        if not split:
            result.append(name)

    return result
