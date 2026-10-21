"""How much of a source the ingest actually extracted, said on pull (#349).

A pull used to read the same whether the parser kept the whole document or
only part of it. "Competitive Messaging Editorial Framework.pptx" (sap-5200)
came out as slide titles only, because its slide bodies are images, and the
Navan eBook PDF gave ~6 chunks from ~19 pages. Both pulls reported
`truncated: false`.

Since document-ingest 7492bdd the PPTX and PDF parsers record coverage in
`rag_assets.meta.coverage`:

    {"unit": "slide" | "page", "total": N, "with_text": K,
     "without_text": [slide/page numbers with no extractable text],
     "title_only": [slides whose only text is a title over pictures],  # pptx
     "with_notes": M,                                                # pptx
     "method": "text" | "visual"}                                    # pdf

Rows ingested before that carry no coverage. Readers then omit the field
rather than claim the document is complete. Other formats record nothing,
because they have no unit of "saw it but found no text".
"""
from __future__ import annotations

import json
from typing import Any

# PostgREST projection of ONLY the coverage object (`->` keeps it jsonb), so
# a reader never pulls the whole `meta` blob, which can be megabytes.
META_COVERAGE_SELECT = "coverage:meta->coverage"

# Cap on the numbers spelled out in a warning; the full list stays in `coverage`.
_MAX_LISTED = 15


def normalize(raw: Any) -> dict | None:
    """The stored coverage as a dict, or None when absent or malformed."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        total = int(raw.get("total"))
        with_text = int(raw.get("with_text"))
    except (TypeError, ValueError):
        return None
    if total < 0 or with_text < 0:
        return None
    out = dict(raw)
    out["total"], out["with_text"] = total, with_text
    for key in ("without_text", "title_only"):
        out[key] = _numbers(raw.get(key))
    return out


def _numbers(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _listing(nums: list[int]) -> str:
    shown = ",".join(str(n) for n in nums[:_MAX_LISTED])
    return shown + (",…" if len(nums) > _MAX_LISTED else "")


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'}"


def is_partial(cov: dict) -> bool:
    return bool(cov["without_text"] or cov["title_only"]
                or cov["with_text"] < cov["total"])


def warning(cov: dict) -> str | None:
    """A plain-English warning when coverage is partial, else None.

    e.g. "12 slides, 4 with text — 8 image-only: 3,5,6,…". Image-only
    content is NOT in the pulled text. Read the original for it.
    """
    if not is_partial(cov):
        return None
    unit = cov.get("unit") or "page"
    missing = cov["without_text"]
    gap = cov["total"] - cov["with_text"]
    parts = [f"{_plural(cov['total'], unit)}, {cov['with_text']} with text"]
    if gap:
        label = "image-only" if unit == "slide" else "without text"
        parts.append(f"{gap} {label}" + (f": {_listing(missing)}" if missing else ""))
    head = " — ".join(parts)
    if cov["title_only"]:
        head += (f"; {_plural(len(cov['title_only']), unit)} with only a title over "
                 f"images: {_listing(cov['title_only'])}")
    return (head + ". That content is not in this text; read the original via "
            "`fetch_project_source`.")


def short_flag(cov: dict) -> str | None:
    """A compact list-view flag for partial coverage, e.g. "4/12 slides with text"."""
    if not is_partial(cov):
        return None
    unit = cov.get("unit") or "page"
    flag = f"{cov['with_text']}/{cov['total']} {unit}s with text"
    if cov["title_only"]:
        flag += f", {len(cov['title_only'])} title-only"
    return flag


def coverage_fields(raw: Any) -> dict:
    """The `coverage` / `coverage_warning` keys a pull result carries.

    Empty when nothing was recorded (a legacy row, or a format without units),
    so absence never reads as "complete".
    """
    cov = normalize(raw)
    if cov is None:
        return {}
    out: dict = {"coverage": cov}
    note = warning(cov)
    if note:
        out["coverage_warning"] = note
    return out


def list_fields(raw: Any) -> dict:
    """The `coverage_flag` key a list entry carries when coverage is partial."""
    cov = normalize(raw)
    if cov is None:
        return {}
    flag = short_flag(cov)
    return {"coverage_flag": flag} if flag else {}
