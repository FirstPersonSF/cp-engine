"""`cp spine-recover` — re-home a project's legacy spine elements into the
current capture-loop authored format under the canonical code.

Background: a project whose `spine_substance` rows are split across two
project_codes (the cp spine-migrate slug-drift) has its real memory stranded in
legacy capitalized-layer disk files. This recovers them via the shipped write
engine. See docs/plans/2026-06-19-spine-code-drift-recovery-design.md.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from cp_engine.spine import SpineElement, load_spine


def load_legacy_elements(project_dir: Path) -> tuple[SpineElement, ...]:
    """Parse a project's LEGACY spine elements (capitalized-layer dirs) off disk.

    Thin wrapper over `cp_engine.spine.load_spine`, which iterates the 11 fixed
    capitalized LAYERS under `<project_dir>/spine/` — naturally skipping the new
    lowercase phase dirs (substance), `_context/`, `_authored/`, and snapshots,
    which are not LAYERS. These legacy files are the recovery's authoritative
    source."""
    return load_spine(project_dir)

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


@dataclasses.dataclass(frozen=True)
class RecoveryAction:
    mode: str                  # "redistill" | "carry"
    layer: str
    label: str                 # the element's title → becomes the authored label
    body: str                  # existing body (carry) or "" placeholder (redistill fills it)
    serves: tuple              # legacy cross-links, preserved
    asset_id: str | None = None        # the matched rag_asset id (redistill only)
    source_basename: str | None = None # the matched source filename (redistill only, for the report)


def plan_element(el, assets: list[dict]) -> RecoveryAction:
    """Decide one legacy element's recovery path (pure — no I/O/LLM/DB).

    Source-backed layer WITH a confident rag_asset match → 'redistill' (record
    asset_id). Otherwise → 'carry' the existing body verbatim. `serves` and
    `layer`/`label` are preserved either way."""
    has_source = bool(el.source)
    kind = classify_element(el.layer, has_source=has_source)
    if kind == "source-backed":
        asset = match_source_asset(el.source, assets)
        if asset is not None:
            return RecoveryAction(
                mode="redistill", layer=el.layer, label=el.title, body="",
                serves=tuple(el.serves), asset_id=asset["id"],
                source_basename=asset["title"],
            )
    # synthesis, or source-backed with no confident asset → carry verbatim
    return RecoveryAction(
        mode="carry", layer=el.layer, label=el.title, body=el.body,
        serves=tuple(el.serves),
    )


_REDISTILL_PROMPT = """\
You are distilling project source material into a single buildable spine element,
UNDER a directing framing line. Stay faithful to the source material — do not
invent — but organize and emphasize it to serve the framing. Write 200-450 words
of dense, buildable prose (no preamble, no headers, no meta-commentary). Output
ONLY the distilled body.

## Framing (the element this becomes)
{framing}

## Source material
{raw}
"""


def redistill_body(action, *, asset_text: str, distiller) -> str:
    """Re-distill a source-backed element's body from its matched asset text.

    Builds a directed-distillation prompt (the element's `label` as the framing +
    the asset text as raw material) and returns the injected `distiller`'s output
    (stripped). `distiller` is a callable str->str; the orchestrator passes a real
    LLM wrapper, tests pass a fake."""
    prompt = _REDISTILL_PROMPT.format(framing=action.label, raw=asset_text)
    return distiller(prompt).strip()
