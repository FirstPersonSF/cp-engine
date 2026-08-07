"""Structural text diff between two versions of a document (#160).

Client feedback on decks increasingly arrives as a REVISED COPY of the
artifact — "comments added" meaning comments resolved in. The ibx-5192
r3UPDATES round proved the extraction: the decisive artifact was a
per-slide similarity diff that had to be hand-scripted (unzip, regex
``<a:t>`` runs, pairwise ratios). This module makes that repeatable.

Units are slides (pptx), heading-sections (docx/md), or the whole text
(txt fallback). Alignment is by best-match similarity, NOT unit index —
decks get reordered, and index-pairing produces garbage. Output is data;
the agent narrates it into a worklist.

The same engine answers the duplicate-verify question from #158 (gap 3):
``compare_files(a, b)`` with an overall ratio beats pulling both full
docs into context, and works across containers where file_hash can't.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# A matched pair at/above this ratio is "unchanged" (formatting noise only).
UNCHANGED_AT = 0.95
# Below this ratio a pair is not a match at all — the unit was cut/added.
MATCH_THRESHOLD = 0.5
# Units with fewer characters than this are flagged as placeholders.
PLACEHOLDER_CHARS = 40


@dataclass
class Unit:
    index: int  # 1-based position in the document
    label: str
    text: str


def _pptx_units(path: Path) -> list[Unit]:
    """One unit per slide, in deck order, text = joined a:t runs."""
    units: list[Unit] = []
    with zipfile.ZipFile(path) as zf:
        slides = sorted(
            (n for n in zf.namelist()
             if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
        )
        for i, name in enumerate(slides, 1):
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                units.append(Unit(i, f"slide {i}", ""))
                continue
            runs = [t.text for t in root.iter(f"{_A}t") if t.text]
            units.append(Unit(i, f"slide {i}", " ".join(runs).strip()))
    return units


def _docx_units(path: Path) -> list[Unit]:
    """Heading-delimited sections; a doc with no headings is one unit."""
    with zipfile.ZipFile(path) as zf:
        if "word/document.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("word/document.xml"))
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] = ("(intro)", [])
    for p in root.iter(f"{_W}p"):
        style = p.find(f"{_W}pPr/{_W}pStyle")
        text = " ".join(t.text for t in p.iter(f"{_W}t") if t.text).strip()
        is_heading = style is not None and (
            (style.get(f"{_W}val") or "").lower().startswith(("heading", "title"))
        )
        if is_heading and text:
            if current[1] or sections:
                sections.append(current)
            current = (text, [])
        elif text:
            current[1].append(text)
    sections.append(current)
    return [
        Unit(i, label, " ".join(parts).strip())
        for i, (label, parts) in enumerate(sections, 1)
        if label != "(intro)" or parts
    ]


_MD_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _md_units(path: Path) -> list[Unit]:
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = list(_MD_HEADING.finditer(text))
    if not matches:
        return [Unit(1, "(document)", text.strip())]
    units: list[Unit] = []
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        units.append(Unit(1, "(intro)", text[: matches[0].start()].strip()))
    for j, m in enumerate(matches):
        end = matches[j + 1].start() if j + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        units.append(Unit(len(units) + 1, m.group(1).strip(), body))
    return units


def extract_units(path: str | Path) -> list[Unit]:
    """Per-unit text for a document; unsupported extensions raise ValueError."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pptx":
        return _pptx_units(p)
    if ext == ".docx":
        return _docx_units(p)
    if ext in (".md", ".markdown", ".txt"):
        return _md_units(p)
    raise ValueError(f"unsupported extension {ext!r} (pptx/docx/md/txt)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def compare_units(
    units_a: list[Unit],
    units_b: list[Unit],
    *,
    match_threshold: float = MATCH_THRESHOLD,
) -> dict:
    """Best-match alignment of A's units onto B's, similarity-scored.

    Greedy on descending ratio: each unit pairs at most once, highest
    scores claim first, pairs under `match_threshold` don't pair at all.
    """
    norm_a = [_norm(u.text) for u in units_a]
    norm_b = [_norm(u.text) for u in units_b]
    scored: list[tuple[float, int, int]] = []
    for i, na in enumerate(norm_a):
        for j, nb in enumerate(norm_b):
            if not na and not nb:
                ratio = 1.0
            elif not na or not nb:
                ratio = 0.0
            else:
                ratio = SequenceMatcher(None, na, nb).ratio()
            if ratio >= match_threshold:
                scored.append((ratio, i, j))
    scored.sort(key=lambda t: -t[0])

    used_a: set[int] = set()
    used_b: set[int] = set()
    matched = []
    for ratio, i, j in scored:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        ua, ub = units_a[i], units_b[j]
        matched.append({
            "a": {"index": ua.index, "label": ua.label},
            "b": {"index": ub.index, "label": ub.label},
            "similarity": round(ratio, 3),
            "verdict": "unchanged" if ratio >= UNCHANGED_AT else "edited",
            "moved": ua.index != ub.index,
        })
    matched.sort(key=lambda m: m["b"]["index"])

    def _side(units: list[Unit], used: set[int]) -> list[dict]:
        return [
            {
                "index": u.index,
                "label": u.label,
                "excerpt": u.text[:200],
                **({"placeholder": True} if len(u.text) < PLACEHOLDER_CHARS else {}),
            }
            for k, u in enumerate(units)
            if k not in used
        ]

    placeholders_b = [
        {"index": u.index, "label": u.label, "chars": len(u.text)}
        for u in units_b
        if len(u.text) < PLACEHOLDER_CHARS
    ]
    return {
        "a_units": len(units_a),
        "b_units": len(units_b),
        "matched": matched,
        "cut": _side(units_a, used_a),       # in A, gone from B
        "new": _side(units_b, used_b),       # in B, not from A
        "placeholders_in_b": placeholders_b,  # thin/empty units — unfinished?
    }


def compare_files(path_a: str | Path, path_b: str | Path) -> dict:
    """Full comparison of two files + an overall similarity (dup check, #158)."""
    units_a = extract_units(path_a)
    units_b = extract_units(path_b)
    result = compare_units(units_a, units_b)
    whole_a = _norm(" ".join(u.text for u in units_a))
    whole_b = _norm(" ".join(u.text for u in units_b))
    result["overall_similarity"] = round(
        SequenceMatcher(None, whole_a, whole_b).ratio(), 3
    ) if (whole_a or whole_b) else 1.0
    result["a_path"] = str(path_a)
    result["b_path"] = str(path_b)
    return result
