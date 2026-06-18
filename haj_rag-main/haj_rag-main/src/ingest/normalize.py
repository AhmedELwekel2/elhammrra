"""
Arabic text normalization for the Hajj book pipeline.

Used by the chunker (src/ingest/chunk.py) to build the `text_norm` field and,
later, by the embedder so query text and chunk text are normalized identically.
Normalization is deliberately lossy: it strips the orthographic variation that
hurts lexical/embedding recall (tashkeel, alef/ya/ta-marbuta variants, tatweel)
while leaving the consonant skeleton intact.

Keep this module dependency-free so both steps can import it cheaply.
"""

from __future__ import annotations

import re

# Tashkeel / diacritics to delete outright. Covers the Arabic harakat block
# (064B-0652), superscript alef (0670), the small-mark range (06D6-06ED), and
# the Quranic annotation marks (0610-061A).
_TASHKEEL = "".join(
    [
        "ؘؙؚؐؑؒؓؔؕؖؗ",
        "ًٌٍَُِّْٕٓٔ",
        "ٖٜٟٗ٘ٙٚٛٝٞ",
        "ٰ",
        "ۖۗۘۙۚۛۜ۟۠ۡۢ",
        "ۣۤۥۦ۪ۭۧۨ۫۬",
    ]
)
_TASHKEEL_RE = re.compile(f"[{_TASHKEEL}]")

# Tatweel (kashida) — pure decoration, never semantic.
_TATWEEL = "ـ"

# Alef variants -> bare alef; ta-marbuta -> ha; alef-maqsura -> ya.
_LETTER_MAP = {
    "أ": "ا",  # أ hamza-above -> ا
    "إ": "ا",  # إ hamza-below -> ا
    "آ": "ا",  # آ madda      -> ا
    "ٱ": "ا",  # ٱ wasla      -> ا
    "ة": "ه",  # ة ta-marbuta -> ه
    "ى": "ي",  # ى alef-maqsura -> ي
}
_LETTER_TABLE = str.maketrans(_LETTER_MAP)

# Footnote / citation markers like "(١)", "( 12 )", "(٣)" — both Arabic-Indic
# and ASCII digits. Stripped from text_norm so bibliographic numbering does not
# pollute the embedding signal.
_MARKER_RE = re.compile(r"\(\s*[٠-٩۰-۹0-9]+\s*\)")

# Leading marker on a *line* (used by the chunker to detect footnote blocks).
FOOTNOTE_RE = re.compile(r"^\(\s*[٠-٩۰-۹0-9]+\s*\)")

_WS_RE = re.compile(r"\s+")


def strip_tashkeel(text: str) -> str:
    """Remove harakat / diacritics and tatweel only (orthography preserved)."""
    return _TASHKEEL_RE.sub("", text).replace(_TATWEEL, "")


def normalize_arabic(text: str) -> str:
    """Full normalization for embedding/matching.

    Order matters: strip diacritics first (so combining marks don't block the
    letter map), then fold letter variants, drop citation markers, and finally
    collapse whitespace.
    """
    text = strip_tashkeel(text)
    text = text.translate(_LETTER_TABLE)
    text = _MARKER_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()
