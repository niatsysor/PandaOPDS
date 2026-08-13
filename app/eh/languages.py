"""Map E-Hentai language tags to BCP 47 (RFC 5646) language tags.

OPDS 2.0 / RWPM ``metadata.language`` requires RFC 5646 (BCP 47) tags
("zh", "ja", "zh-Hans"); E-Hentai uses free-text ``language:`` tag keys
("chinese", "chinese (simplified)"). This module is the single mapping
used by every derivation point (list parser, detail page, gdata fallback)
so the feed layer always emits standards-compliant values.

Unknown keys and marker pseudo-tags (``translated`` / ``rewrite`` / ``raw``)
map to None — the field is omitted, and the raw tag text stays visible in
the detail document's ``subject``, so nothing is lost.
"""

from __future__ import annotations

# E-Hentai `language:` tag key → BCP 47 (RFC 5646) language tag.
EH_LANGUAGE_MAP: dict[str, str] = {
    "chinese": "zh",
    "chinese (simplified)": "zh-Hans",
    "chinese (traditional)": "zh-Hant",
    "english": "en",
    "japanese": "ja",
    "korean": "ko",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "dutch": "nl",
    "portuguese": "pt",
    "portuguese (brazil)": "pt-BR",
    "russian": "ru",
    "vietnamese": "vi",
    "indonesian": "id",
    "thai": "th",
    "arabic": "ar",
    "polish": "pl",
    "turkish": "tr",
    "hungarian": "hu",
    "swedish": "sv",
    "czech": "cs",
    "greek": "el",
    "danish": "da",
    "finnish": "fi",
    "norwegian": "no",
    "ukrainian": "uk",
    "hebrew": "he",
    "hindi": "hi",
    "malay": "ms",
    "bengali": "bn",
    "filipino": "fil",
    "romanian": "ro",
    "catalan": "ca",
    "slovak": "sk",
    "bulgarian": "bg",
    "croatian": "hr",
    "serbian": "sr",
    "slovenian": "sl",
    "latin": "la",
    "persian": "fa",
    "mongolian": "mn",
    "nepali": "ne",
    "burmese": "my",
    "khmer": "km",
    "lao": "lo",
    "tamil": "ta",
    "telugu": "te",
    "punjabi": "pa",
    "gujarati": "gu",
    "urdu": "ur",
    "kazakh": "kk",
    "uzbek": "uz",
    "amharic": "am",
    "swahili": "sw",
    "icelandic": "is",
    "latvian": "lv",
    "lithuanian": "lt",
    "estonian": "et",
    "albanian": "sq",
    "macedonian": "mk",
    "georgian": "ka",
    "armenian": "hy",
    "azerbaijani": "az",
    "welsh": "cy",
    "irish": "ga",
    "esperanto": "eo",
    "afrikaans": "af",
}

# Marker pseudo-tags inside the `language:` namespace (not real languages):
# `translated` — a translation is present; `rewrite` — machine re-translation;
# `raw` — untranslated original. Never produce a language value.
LANGUAGE_MARKERS: frozenset[str] = frozenset(
    {"translated", "rewrite", "raw"}
)


def map_language(key: str) -> str | None:
    """Map an E-Hentai language tag key to a BCP 47 (RFC 5646) tag.

    Returns None for unknown keys and marker pseudo-tags — callers omit the
    field (the raw tag remains in the detail document's ``subject``).
    """
    k = (key or "").strip().lower()
    if k in LANGUAGE_MARKERS:
        return None
    return EH_LANGUAGE_MAP.get(k)
