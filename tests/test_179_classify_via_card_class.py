"""The sweeps ask `card_class`, not a layer string (#179 scope item 1).

"Is this work?" was inferred from layer strings in four places, each with its
own hand-maintained list:

    stub_sweep.SOURCE_LAYERS       ("source material", "sourcematerial", "source")
    seal_sweep.DELIVERABLE_LAYERS  ("deliverables", "output")
    spine_lint  (same pair, duplicated inline)
    frontend roles.ts CARD_ROLES

That inference is what broke in #172, when `Output` was collapsed into
`Deliverables` and two of the lists had to be updated by hand. `card_kind` is
the explicit answer and already carries the right shape — `classify()` prefers
it and falls back to inference only for rows written before the field existed,
with `classify_is_inferred()` telling the two apart.

THE FALLBACK IS LOAD-BEARING, NOT DECORATION. 126 of the tenant's 149 live
Source-material elements are stubs, many predating `card_kind`. A change that
trusted `classify()` unconditionally would re-classify them on a guess; these
tests pin that an inferred classification defers to the historical list, and an
EXPLICIT one wins.
"""

from __future__ import annotations

from cp_engine.seal_sweep import _is_deliverable
from cp_engine.stub_sweep import _is_stream


class TestExplicitCardKindWins:
    def test_an_explicit_attachment_is_stream_whatever_its_layer(self):
        """The #172 shape: the layer string disagrees, the field decides."""
        row = {"card_kind": "attachment", "layer": "Synthesis",
               "est_item_id": "_authored/x"}
        assert _is_stream(row) is True

    def test_an_explicit_card_is_not_stream_even_on_a_source_layer(self):
        row = {"card_kind": "deliverable", "layer": "Source material",
               "est_item_id": "_authored/x"}
        assert _is_stream(row) is False

    def test_an_explicit_deliverable_is_seen_by_seal_sweep(self):
        row = {"card_kind": "deliverable", "layer": "Output",
               "est_item_id": "_authored/x"}
        assert _is_deliverable(row) is True

    def test_an_explicit_activity_is_not_a_deliverable(self):
        row = {"card_kind": "activity", "layer": "Deliverables",
               "est_item_id": "_authored/x"}
        assert _is_deliverable(row) is False


class TestTheHistoricalFallback:
    """Rows written before `card_kind` existed keep the old behaviour."""

    def test_a_pre_field_source_row_is_still_stream(self):
        row = {"layer": "Source material", "est_item_id": "_authored/x"}
        assert _is_stream(row) is True

    def test_a_pre_field_non_source_row_is_not_stream(self):
        row = {"layer": "Synthesis", "est_item_id": "_authored/x"}
        assert _is_stream(row) is False

    def test_the_output_alias_still_reads_as_a_deliverable(self):
        """#172's casualty: `Output` was the pre-mig-137 spelling."""
        row = {"layer": "Output", "est_item_id": "_authored/x"}
        assert _is_deliverable(row) is True

    def test_a_pre_field_non_deliverable_is_not_one(self):
        row = {"layer": "Decisions", "est_item_id": "_authored/x"}
        assert _is_deliverable(row) is False


class TestDriftDoesNotCrash:
    """An unrecognised `card_kind` is data drift, not an exception.

    `classify()` already falls through to inference on a bad value rather than
    raising — the #172 lesson that a vocabulary which can only validate against
    itself drifts silently. These pin that the sweeps inherit that tolerance.
    """

    def test_an_unknown_card_kind_falls_back_to_the_layer(self):
        row = {"card_kind": "nonsense", "layer": "Source material",
               "est_item_id": "_authored/x"}
        assert _is_stream(row) is True

    def test_an_empty_card_kind_falls_back_to_the_layer(self):
        row = {"card_kind": "", "layer": "Output", "est_item_id": "_authored/x"}
        assert _is_deliverable(row) is True

    def test_a_missing_layer_does_not_raise(self):
        assert _is_stream({"est_item_id": "_authored/x"}) is False
        assert _is_deliverable({"est_item_id": "_authored/x"}) is False
