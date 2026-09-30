"""Confidentiality markings in a source document's BODY (#324 part b).

A title does not say a document is embargoed; its footer does. Before this,
`rag_assets.status_note` was only ever written by hand (`set_source_status`),
so an "INTERNAL – SAP Concur Confidential" guide or a "CAB 2026 | CONFIDENTIAL"
deck read exactly like a public case study at every point of use.

This module is PURE: text in, markings out. Ingest calls it on the chunks it
just wrote and records a hit in `status_note` when — and only when — nobody
has written one (a human note always outranks a detected one).

WHAT COUNTS AS A MARKING. The patterns target the way documents LABEL
themselves — headers, footers, slide strips, cover lines — not prose that
merely discusses confidentiality. Tuned against the live source store
(2026-09-30, 472 active assets across the tenant):

  - Lowercase "confidential" in running prose never fires ("maintaining
    confidentiality", "a confidential ticket", "confidential client
    frameworks" — all real, all noise).
  - "Confidential Information" is a contract's DEFINED TERM, not a marking;
    excluded. Every SOW and MSA would otherwise read as embargoed.
  - "proprietary" alone is marketing copy ("our proprietary AI model");
    it fires only as "Proprietary and Confidential".
  - Bare "NDA" is prose ("we signed the NDA"); it fires only as "under NDA"
    / "subject to NDA" / "NDA only".
  - "embargo" fires only as EMBARGOED (caps) or "embargoed until" /
    "under embargo" — a site nav's "Ethics Embargoes" does not.

KNOWN FALSE-POSITIVE CLASS: a document ABOUT marked documents (a source
register that quotes "CONFIDENTIAL — INTERNAL USE ONLY" while describing
another file) carries the marking verbatim and is flagged. That is why the
note says "detected — unconfirmed": it is a prompt to look, not a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Marking:
    """One confidentiality marking found in a body."""

    kind: str  # confidential | internal-only | nda | embargoed | no-distribution | privileged
    phrase: str  # the matched text, whitespace-collapsed
    context: str  # a short window around it, whitespace-collapsed


# (kind, compiled pattern). Case sensitivity is deliberate per pattern: the
# capitalised/uppercase forms are how documents label themselves; the
# lowercase forms are how people talk about them.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("privileged", re.compile(
        r"\b(?:privileged\s+(?:and|&)\s+confidential|attorney[\s-]+client\s+privilege[d]?)\b",
        re.IGNORECASE)),
    ("confidential", re.compile(
        r"\b(?:proprietary\s+(?:and|&)\s+confidential|confidential\s+(?:and|&)\s+proprietary)\b",
        re.IGNORECASE)),
    # "SAP Confidential", "SAP Concur Confidential", "Company Confidential",
    # "Strictly Confidential": capitalised word(s) + capitalised Confidential,
    # never the defined term "Confidential Information".
    ("confidential", re.compile(
        r"\b(?:[A-Z][A-Za-z&]*\s+){1,2}Confidential\b(?!\s+(?:Information|Treatment))")),
    # The uppercase marking: "CAB 2026 | CONFIDENTIAL", "CONFIDENTIAL — …".
    ("confidential", re.compile(r"\bCONFIDENTIAL\b(?!\s+INFORMATION)")),
    # OCR sometimes splits the last letter ("ONL Y") — tolerate one space.
    ("internal-only", re.compile(
        r"\b(?:FOR\s+)?INTERNAL\s+USE\s+ONL\s?Y\b|\bFor\s+Internal\s+Use\s+Only\b")),
    ("nda", re.compile(
        r"\b(?:under\s+(?:an?\s+|the\s+)?NDA|subject\s+to\s+(?:an?\s+|the\s+)?NDA|NDA\s+only)\b",
        re.IGNORECASE)),
    ("embargoed", re.compile(r"\bEMBARGOED\b")),
    ("embargoed", re.compile(
        r"\b(?:embargoed\s+(?:until|till|through)|under\s+(?:an?\s+)?embargo)\b",
        re.IGNORECASE)),
    ("no-distribution", re.compile(
        r"\b(?:not\s+for\s+(?:external\s+|public\s+|wider\s+)?(?:distribution|release|circulation)"
        r"|do\s+not\s+(?:distribute|forward|circulate)"
        r"|do\s+not\s+share\s+(?:outside|externally)"
        r"|do\s+not\s+quote\s+externally)\b",
        re.IGNORECASE)),
)

_CONTEXT = 30
_NOTE_PREFIX = "auto-detected at ingest"
_KIND_ORDER = ("embargoed", "privileged", "confidential", "nda",
               "internal-only", "no-distribution")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def detect_markings(text: str | None) -> list[Marking]:
    """Every confidentiality marking in `text`, in document order, one per
    (kind, phrase) — a footer repeated on 24 pages is one marking."""
    if not text:
        return []
    found: list[tuple[int, Marking]] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            phrase = _squash(m.group(0))
            key = (kind, phrase.lower())
            if key in seen:
                continue
            seen.add(key)
            lo, hi = max(0, m.start() - _CONTEXT), min(len(text), m.end() + _CONTEXT)
            found.append((m.start(), Marking(kind, phrase, _squash(text[lo:hi]))))
    found.sort(key=lambda t: t[0])
    return [mk for _, mk in found]


def status_note_for(markings: list[Marking]) -> str | None:
    """The `status_note` a detected marking earns, or None for no markings.

    Says it was DETECTED and is UNCONFIRMED (the `set_source_status` rule: a
    confidently wrong note is worse than none), names the kinds, and quotes
    the first marking so a reader can judge without pulling the document.
    """
    if not markings:
        return None
    kinds = sorted({m.kind for m in markings}, key=_KIND_ORDER.index)
    first = next(m for m in markings if m.kind == kinds[0])
    return (
        f"{_NOTE_PREFIX}: body marked {', '.join(kinds)} "
        f"(e.g. “{first.context}”) — unconfirmed; confirm or clear "
        "with set_source_status before quoting externally"
    )
