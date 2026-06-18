"""Placement proposer for the spine_elements → spine_substance merge (Task 6.1).

The legacy `spine_elements` store is layer-organized with no estimate binding.
The new `spine_substance` store hangs distilled memory off estimate work items,
with a `placement` axis: an item sits either ON its estimate work item
(`placement='item'`) or on the project's CONTEXT shelf (`placement='context'`,
the cross-cutting rail).

This module is the pure rule that, given a legacy `SpineElement` and the project's
live `Estimate`, *proposes* where that element should land (design §1):

* Cross-cutting layers (Decisions, Stakeholders, SourceMaterial, …) serve the
  whole project, not one work item → `placement='context'`, no est binding.
* The `Deliverables` layer names a buildable artifact → try to name-match it to
  an estimate work item and PROPOSE a binding (`placement='item'`). A human
  confirms; no confident match falls back to `context` for review.

No I/O, no LLM — a deterministic fuzzy-match rule. The `cp spine-migrate` command
(Task 6.2) drives it over a project's legacy rows.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# Cross-cutting "rail" layers (design §1): each serves the whole project rather
# than one work item, so it lands on the context shelf with no estimate binding.
# This is every spine layer EXCEPT `Deliverables` (the one that binds) — and
# `Drafts`, which is an in-progress *deliverable* layer, not cross-cutting, so it
# is intentionally NOT here (it falls through to the unknown-layer → context
# branch with low confidence rather than claiming full-confidence rail status).
CROSS_CUTTING: frozenset[str] = frozenset(
    {
        "Decisions",
        "Stakeholders",
        "SourceMaterial",
        "Timeline",
        "Research",
        "ClientFeedback",
        "Retrospective",
        "Synthesis",
        "Brief",
        "Agreement",
    }
)

# Minimum SequenceMatcher ratio for a Deliverable→estimate-item binding to be
# proposed (design §1: "If no confident match, fall back to placement='context'").
_MATCH_THRESHOLD = 0.5


@dataclass(frozen=True)
class PlacementProposal:
    """Where a legacy element should land in the substance store, proposed for
    human confirmation. `est_item_id` is set only for `placement='item'`."""

    placement: str  # item | context
    est_item_id: str | None
    confidence: float
    basis: str


def _best_item_match(title: str, estimate):
    """Return (best EstimateItem, ratio) by fuzzy name-match, or (None, 0.0)."""
    best = None
    best_ratio = 0.0
    a = title.lower()
    for item in estimate.all_items():
        ratio = difflib.SequenceMatcher(None, a, item.name.lower()).ratio()
        if ratio > best_ratio:
            best, best_ratio = item, ratio
    return best, best_ratio


def propose_placement(element, estimate) -> PlacementProposal:
    """Propose a placement for one legacy `SpineElement` against the `Estimate`.

    Rules (design §1):
      1. Cross-cutting layer → context rail, full confidence.
      2. `Deliverables` → fuzzy-match the element title to an estimate item;
         bind (`item`) at ratio ≥ 0.5, else fall back to `context` (low conf).
      3. Any other/unknown layer → context, mid confidence.
    """
    layer = element.layer
    if layer in CROSS_CUTTING:
        return PlacementProposal(
            placement="context",
            est_item_id=None,
            confidence=1.0,
            basis=f"{layer} is a cross-cutting rail layer",
        )

    if layer == "Deliverables":
        best, ratio = _best_item_match(element.title, estimate)
        if best is not None and ratio >= _MATCH_THRESHOLD:
            return PlacementProposal(
                placement="item",
                est_item_id=best.id,
                confidence=ratio,
                basis=f"matched '{element.title}' → '{best.name}'",
            )
        return PlacementProposal(
            placement="context",
            est_item_id=None,
            confidence=ratio,
            basis=(
                f"Deliverable but no confident estimate-item match "
                f"(best {ratio:.2f}); placed in context for review"
            ),
        )

    return PlacementProposal(
        placement="context",
        est_item_id=None,
        confidence=0.5,
        basis=f"unknown layer '{layer}' → context",
    )
