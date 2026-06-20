"""`cp spine-recover` — re-home a project's legacy spine elements into the
current capture-loop authored format under the canonical code.

Background: a project whose `spine_substance` rows are split across two
project_codes (the cp spine-migrate slug-drift) has its real memory stranded in
legacy capitalized-layer disk files. This recovers them via the shipped write
engine. See docs/plans/2026-06-19-spine-code-drift-recovery-design.md.
"""
from __future__ import annotations

import os

# Layers whose elements summarize ONE identifiable source → re-distill from it.
_SOURCE_BACKED_LAYERS = frozenset({
    "SourceMaterial", "ClientFeedback", "Research", "Brief", "Agreement",
})


def classify_element(layer: str, *, has_source: bool) -> str:
    """'source-backed' (re-distill from the mapped asset) or 'synthesis'
    (carry the existing body verbatim). A source-backed layer with no `source:`
    field can't be re-distilled, so it carries over too."""
    if layer in _SOURCE_BACKED_LAYERS and has_source:
        return "source-backed"
    return "synthesis"


def source_basename(entry: str) -> str:
    """Basename of a `source:` path entry ('Reference Materials/x.pptx' -> 'x.pptx')."""
    return os.path.basename(str(entry).strip())


def match_source_asset(sources: tuple, assets: list[dict]) -> dict | None:
    """Resolve an element's `source` tuple to a rag_asset.

    A typed `{"type":"rag_asset","id":...}` entry resolves by id; a scalar path
    entry resolves by filename basename against `rag_assets.title`. Returns the
    first confident match, or None (→ caller carries the element over)."""
    by_id = {a["id"]: a for a in assets}
    by_title = {a["title"]: a for a in assets}
    for s in sources:
        if isinstance(s, dict) and s.get("type") == "rag_asset" and s.get("id") in by_id:
            return by_id[s["id"]]
        if not isinstance(s, dict):
            hit = by_title.get(source_basename(s))
            if hit:
                return hit
    return None
