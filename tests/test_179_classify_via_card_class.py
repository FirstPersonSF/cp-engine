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


# ── Reference is its own class (#179) ─────────────────────────────────
#
# Drew's first call was that Reference need not be first-class — a Stakeholder
# would just be "a card that is deliberately unbound". The data said otherwise.
# `card_kind='attachment'` held 374 live elements (67% of the spine) and was
# two different things:
#
#                    Stream (capture)   Reference (authored)
#   count                  129                 245
#   avg body               120 chars         4,782 chars
#   stubs (<800ch)         129 (100%)           28 (11%)
#   layers spanned           3                  12
#
# And it is NOT "Source material" renamed: 24 authored elements sit on that
# layer, while the 245 span twelve. No list of layer strings separates them.
#
# THE RULE uses two independent signals that agreed on all 374 rows:
# body under 200 chars AND a pointer at the asset it wraps (a `sources` entry,
# or a `rag_asset:` line in the body — 126 of 129 had the former, 3 only the
# latter). Requiring BOTH the length and a pointer is what keeps a short
# authored note out of Stream.

from cp_engine.card_class import CardKind, classify


def _ctx(**kw) -> dict:
    base = {"est_item_id": "_authored/x", "layer": "Synthesis",
            "placement": "context", "card_kind": None}
    base.update(kw)
    return base


class TestStreamIsCaptureOnly:
    def test_the_ingest_wrapper_is_stream(self):
        """The real shape: short body naming the rag_asset it duplicates."""
        row = _ctx(
            layer="Source material",
            body="Ingested document: **ABM slides.pptx** (doc)\n\nrag_asset: `6b2404de`",
        )
        assert classify(row) is CardKind.ATTACHMENT
        assert classify(row).is_stream is True

    def test_a_sources_entry_also_marks_capture(self):
        """126 of 129 carry the pointer as a `sources` entry instead."""
        row = _ctx(layer="Source material", body="Ingested document: x",
                   sources=[{"rag_asset_id": "abc"}])
        assert classify(row) is CardKind.ATTACHMENT

    def test_a_short_authored_note_is_NOT_stream(self):
        """The length alone must not demote a thought — this is why the rule
        requires a pointer too."""
        row = _ctx(body="Rina wants the roadshow split shipped with the pop-ups.")
        assert classify(row) is CardKind.REFERENCE


class TestReferenceIsNotADefect:
    def test_a_stakeholder_dossier_is_reference(self):
        """33 live Stakeholders, 0 stubs, avg 5.4k — correct as they are."""
        row = _ctx(layer="Stakeholders", body="x" * 7000)
        assert classify(row) is CardKind.REFERENCE

    def test_a_retrospective_is_reference(self):
        row = _ctx(layer="Retrospective", body="x" * 6800)
        assert classify(row) is CardKind.REFERENCE

    def test_reference_is_not_work(self):
        """It is not a card in the Work sense — but it is not capture either."""
        kind = classify(_ctx(body="x" * 5000))
        assert kind.is_card is False
        assert kind.is_stream is False


class TestTheSweepsSeeTheDifference:
    def test_stub_sweep_ignores_reference(self):
        """THE NOISE FIX. An unedged Stakeholder is not a stub to collapse."""
        assert _is_stream(_ctx(layer="Stakeholders", body="x" * 7000)) is False

    def test_stub_sweep_still_finds_real_capture(self):
        row = _ctx(layer="Source material",
                   body="Ingested document: x\n\nrag_asset: `abc`")
        assert _is_stream(row) is True

    def test_work_is_untouched_by_the_split(self):
        """Deliverables and activities classify exactly as before."""
        assert classify(_ctx(layer="Deliverables")) is CardKind.DELIVERABLE
        assert classify(_ctx(layer="Activity", placement="item")) is CardKind.ACTIVITY
