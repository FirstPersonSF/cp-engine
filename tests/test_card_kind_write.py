"""`card_kind` is written only where structure decides it.

The load-bearing property is the NEGATIVE one: a row this module cannot
classify structurally must be left NULL, not filled with a plausible guess.
Writing an inference into the column would launder it into unflagged fact and
destroy `classify_is_inferred()` permanently — which is the whole reason
`card_class` says it does not guess.
"""

from __future__ import annotations

from cp_engine.card_class import CardKind, classify, is_ambiguous
from cp_engine.card_kind_write import build_plan


def _row(**kw) -> dict:
    base = {
        "id": "p/x/v1",
        "project_code": "p",
        "est_item_id": "_authored/x",
        "layer": "Synthesis",
        "placement": "context",
        "card_kind": None,
        "framing": "f",
    }
    base.update(kw)
    return base


# ── the placement rule ────────────────────────────────────────────────


def test_item_placement_is_an_activity():
    """A uuid-keyed estimate slot IS the work — 'Kickoff meeting with the team'."""
    row = _row(est_item_id="uuid-1", layer="Activity", placement="item")
    assert classify(row) is CardKind.ACTIVITY


def test_context_placement_on_activity_layer_is_an_attachment():
    """The sap-5174 interview write-ups: contents of an activity, not activities.

    This is the case `card_class`'s docstring names as unresolvable from layer
    alone. Placement resolves it.
    """
    row = _row(layer="Activity", placement="context")
    assert classify(row) is CardKind.ATTACHMENT


def test_placement_beats_ambiguous_layer_both_ways():
    assert classify(_row(layer="Email", placement="item")) is CardKind.ACTIVITY
    assert classify(_row(layer="Email", placement="context")) is CardKind.ATTACHMENT


def test_deliverable_layer_outranks_placement():
    """A Deliverables row is a deliverable card wherever it sits."""
    row = _row(layer="Deliverables", placement="context")
    assert classify(row) is CardKind.DELIVERABLE


def test_engagement_id_wins_outright():
    row = _row(est_item_id="_authored/inputs-briefing", layer="Brief")
    assert classify(row) is CardKind.ENGAGEMENT


def test_no_placement_falls_back_to_the_layer_rule():
    """Pre-mig-070 rows and partial dicts keep the original behaviour."""
    assert classify(_row(layer="Activity", placement=None)) is CardKind.ACTIVITY
    assert classify(_row(layer="Note", placement=None)) is CardKind.ATTACHMENT


# ── ambiguity ─────────────────────────────────────────────────────────


def test_recorded_placement_settles_ambiguity():
    assert not is_ambiguous(_row(layer="Activity", placement="item"))
    assert not is_ambiguous(_row(layer="Email", placement="context"))


def test_straddling_layer_without_placement_stays_ambiguous():
    assert is_ambiguous(_row(layer="Activity", placement=None))
    assert is_ambiguous(_row(layer="Email", placement=""))


def test_explicit_kind_is_never_ambiguous():
    assert not is_ambiguous(_row(layer="Activity", placement=None,
                                 card_kind="attachment"))


# ── the writer ────────────────────────────────────────────────────────


def test_ambiguous_rows_are_left_null_with_a_reason():
    plan = build_plan([_row(id="a", layer="Activity", placement=None)])
    assert plan.to_set == []
    assert len(plan.left_null) == 1
    assert "ambiguous" in plan.left_null[0][1]


def test_rows_with_an_explicit_kind_are_skipped_not_rewritten():
    plan = build_plan([_row(id="a", card_kind="deliverable")])
    assert plan.to_set == []
    assert plan.already_set == 1


def test_plan_counts_match_the_measured_shape():
    """43 cards / 228 attachments was the 2026-08-15 tenant measurement."""
    rows = (
        [_row(id=f"e{i}", est_item_id="_authored/inputs-briefing") for i in range(10)]
        + [_row(id=f"d{i}", layer="Deliverables") for i in range(18)]
        + [_row(id=f"a{i}", est_item_id=f"u{i}", placement="item") for i in range(15)]
        + [_row(id=f"t{i}") for i in range(228)]
    )
    plan = build_plan(rows)
    assert plan.counts == {
        "engagement": 10,
        "deliverable": 18,
        "activity": 15,
        "attachment": 228,
    }
    cards = sum(n for k, n in plan.counts.items() if k != "attachment")
    assert cards == 43


def test_an_unclassifiable_row_never_becomes_a_card_by_accident():
    """Defence in depth: the default must be attachment, never a card kind."""
    plan = build_plan([_row(id="a", layer=None, placement="context")])
    assert plan.to_set == [("a", "attachment")]
