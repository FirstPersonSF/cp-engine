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

from cp_engine.authored_element import build_create_rows, slugify
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


def recovered_rows(actions, *, project_id, project_code, bodies, now_iso):
    """Build authored spine_substance rows for the recovered elements.

    `bodies[i]` is the FINAL body for actions[i] (re-distilled or carried).
    Each action → one authored row via `build_create_rows` (origin=authored,
    placement=context, layer=action.layer, est_item_id=_authored/<slug>).

    Slug-collision guard: `build_create_rows` slugifies the label, so two
    elements whose labels slugify identically would collide on est_item_id (the
    second clobbering the first on upsert). We disambiguate by appending a short
    suffix to the LABEL when a slug repeats, so every element keeps a distinct id.
    """
    rows = []
    seen_slugs: dict[str, int] = {}
    for action, body in zip(actions, bodies):
        base_slug = slugify(action.label)
        n = seen_slugs.get(base_slug, 0)
        seen_slugs[base_slug] = n + 1
        # First occurrence keeps the label as-is; repeats get a numeric suffix
        # appended to the LABEL so build_create_rows derives a distinct slug.
        label = action.label if n == 0 else f"{action.label} {n + 1}"
        new = build_create_rows(
            project_id=project_id, project_code=project_code, label=label,
            type_=action.layer, body=body, serves=list(action.serves), now_iso=now_iso,
        )
        rows.extend(new)
    return rows
