"""Tests for the layer-rule placement proposer (elements→substance merge)."""

from __future__ import annotations

from pathlib import Path

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.placement_rule import (
    CROSS_CUTTING,
    PlacementProposal,
    propose_placement,
)
from cp_engine.spine import SpineElement


def _element(layer: str, title: str, *, serves=(), eid="ibx-5153/x") -> SpineElement:
    return SpineElement(
        id=eid,
        project="ibx-5153",
        layer=layer,
        title=title,
        status="active",
        last_touched="2026-06-11",
        path=Path("spine") / layer / "x.md",
        body="body text",
        serves=tuple(serves),
    )


def _estimate(items: list[tuple[str, str, str]]) -> Estimate:
    """Build a one-phase estimate from (id, name, kind) tuples."""
    est_items = tuple(
        EstimateItem(
            id=i, phase_id="ph-0", kind=k, name=n,
            short_description=None, position=pos, library_item_id=None,
        )
        for pos, (i, n, k) in enumerate(items)
    )
    phase = EstimatePhase(id="ph-0", name="Phase 0", overview=None, position=0, items=est_items)
    return Estimate(id="est-1", mc_project_id="mc-1", name="Estimate 1", phases=(phase,))


def test_cross_cutting_layer_goes_to_context_full_confidence():
    est = _estimate([("d-0", "Perspectives & Possibilities Report", "deliverable")])
    el = _element("Decisions", "Two-track AI story locked")
    p = propose_placement(el, est)
    assert isinstance(p, PlacementProposal)
    assert p.placement == "context"
    assert p.est_item_id is None
    assert p.confidence == 1.0
    assert "cross-cutting" in p.basis


def test_every_cross_cutting_layer_is_context():
    est = _estimate([("d-0", "Some Deliverable", "deliverable")])
    for layer in CROSS_CUTTING:
        p = propose_placement(_element(layer, "anything"), est)
        assert p.placement == "context", layer
        assert p.est_item_id is None, layer


def test_deliverable_matching_estimate_item_binds_with_proposed_placement():
    est = _estimate(
        [
            ("a-0", "Strategy Alignment", "activity"),
            ("d-0", "Perspectives & Possibilities Report", "deliverable"),
        ]
    )
    el = _element("Deliverables", "Perspectives and Possibilities Report")
    p = propose_placement(el, est)
    assert p.placement == "item"
    assert p.est_item_id == "d-0"
    assert p.confidence >= 0.5
    assert "Perspectives" in p.basis


def test_deliverable_with_no_good_match_falls_back_to_context_low_conf():
    est = _estimate([("d-0", "Perspectives & Possibilities Report", "deliverable")])
    el = _element("Deliverables", "Completely Unrelated Widget Spec")
    p = propose_placement(el, est)
    assert p.placement == "context"
    assert p.est_item_id is None
    assert p.confidence < 0.5
    assert "no confident estimate-item match" in p.basis


def test_deliverable_with_empty_estimate_falls_back_to_context():
    est = _estimate([])
    el = _element("Deliverables", "Perspectives & Possibilities Report")
    p = propose_placement(el, est)
    assert p.placement == "context"
    assert p.est_item_id is None
    assert p.confidence < 0.5


def test_unknown_layer_goes_to_context_mid_confidence():
    est = _estimate([("d-0", "Some Deliverable", "deliverable")])
    el = _element("Drafts", "An in-progress draft")  # not in CROSS_CUTTING, not Deliverables
    p = propose_placement(el, est)
    assert p.placement == "context"
    assert p.est_item_id is None
    assert p.confidence == 0.5
    assert "unknown layer" in p.basis


def test_best_match_wins_among_several_items():
    est = _estimate(
        [
            ("d-0", "Narrative Audit", "deliverable"),
            ("d-1", "AI Story Framework", "deliverable"),
            ("d-2", "Stakeholder Map", "deliverable"),
        ]
    )
    el = _element("Deliverables", "AI Story Framework v2")
    p = propose_placement(el, est)
    assert p.placement == "item"
    assert p.est_item_id == "d-1"
