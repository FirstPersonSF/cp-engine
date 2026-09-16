"""Tests for the single definition of card-ness (#179 step 1).

Row shapes are taken from the live tenant (2026-08-11), including the two
layers that provably straddle — those are the reason this module refuses to
guess silently.
"""

from __future__ import annotations

import pytest

from cp_engine.card_class import (
    AMBIGUOUS_LAYERS,
    CardKind,
    attaches_to_engagement,
    classify,
    classify_is_inferred,
    is_ambiguous,
    is_card,
)


def _row(**kw):
    row = {"est_item_id": "_authored/x", "layer": None}
    row.update(kw)
    return row


# --- explicit kind wins -----------------------------------------------------


def test_explicit_kind_beats_layer_inference():
    """The whole point: once a row states its kind, no layer string is
    consulted. A deliverable mislabelled `Source material` stays a
    deliverable."""
    row = _row(layer="Source material", card_kind="deliverable")
    assert classify(row) is CardKind.DELIVERABLE
    assert not classify_is_inferred(row)


def test_explicit_attachment_beats_a_card_layer():
    row = _row(layer="Deliverables", card_kind="attachment")
    assert classify(row) is CardKind.ATTACHMENT
    assert not is_card(row)


def test_an_unrecognised_kind_falls_back_rather_than_crashing():
    """Data drift must not raise. #172's lesson: a vocabulary that can only
    validate against itself drifts silently — so an unknown value degrades to
    inference AND reports itself as inferred."""
    row = _row(layer="Deliverables", card_kind="widget")
    assert classify(row) is CardKind.DELIVERABLE
    assert classify_is_inferred(row)


# --- the three card kinds ---------------------------------------------------


def test_the_standing_briefing_element_is_the_engagement():
    """`_authored/inputs-briefing` exists on every project with a real spine
    and already anchors canon (spec v04 §2)."""
    assert classify(_row(est_item_id="_authored/inputs-briefing")) is CardKind.ENGAGEMENT


def test_engagement_id_wins_over_its_own_layer():
    row = _row(est_item_id="_authored/inputs-briefing", layer="Synthesis")
    assert classify(row) is CardKind.ENGAGEMENT


@pytest.mark.parametrize("layer", ["Brief", "Agreement", "Timeline", "Retrospective"])
def test_engagement_level_material_ATTACHES_it_is_not_its_own_card(layer):
    """A project has ONE engagement, not thirteen.

    The first live run classified these as engagement CARDS and produced 26
    across 5 projects (13 Brief + 11 Agreement + 2 Retrospective) — the exact
    accumulation this effort exists to stop, reintroduced one layer up. They
    are attachments that hang off the engagement."""
    row = _row(layer=layer)
    assert classify(row) is CardKind.REFERENCE
    assert not is_card(row)
    assert attaches_to_engagement(row)


def test_work_material_does_not_attach_to_the_engagement():
    """Feedback and sources belong to the work that consumed them."""
    for layer in ("Client feedback", "Source material", "Synthesis"):
        assert not attaches_to_engagement(_row(layer=layer)), layer


def test_a_card_never_reports_as_engagement_level_material():
    assert not attaches_to_engagement(_row(layer="Deliverables"))
    assert not attaches_to_engagement(_row(est_item_id="_authored/inputs-briefing"))


@pytest.mark.parametrize("layer", ["Deliverables", "Output", "Drafts"])
def test_deliverable_layers_including_the_pre_172_alias(layer):
    """`Output` was an alias nobody folded in; it made spine_stats report 0 of
    sap-5174's 2 deliverables for months."""
    assert classify(_row(layer=layer)) is CardKind.DELIVERABLE


def test_layer_casing_and_spacing_do_not_matter():
    """Layers drift between CamelCase (code) and spaced Title Case (live DB)."""
    for spelling in ("SourceMaterial", "Source material", "source material"):
        assert classify(_row(layer=spelling)) is CardKind.REFERENCE
    for spelling in ("ClientFeedback", "Client feedback"):
        assert classify(_row(layer=spelling)) is CardKind.REFERENCE


# --- attachments ------------------------------------------------------------


@pytest.mark.parametrize(
    "layer",
    ["Source material", "Client feedback", "Synthesis", "Decisions",
     "Stakeholders", "Note", "Research"],
)
def test_everything_else_attaches(layer):
    row = _row(layer=layer)
    assert classify(row) is CardKind.REFERENCE
    assert not is_card(row)


def test_a_missing_layer_attaches_rather_than_becoming_a_card():
    """11 live elements carry layer: null. Defaulting them to a card would put
    unfilable rows on the most-trusted surface."""
    assert classify(_row(layer=None)) is CardKind.REFERENCE


# --- the straddling layers --------------------------------------------------


def test_email_is_ambiguous_because_it_carries_real_feedback():
    """15 live Email rows include "Janet Feedback on r3 Decks" and "Final Video
    Feedback from Janet and team" — client feedback, which the compression loop
    exists to disposition. Email is a transport, not a class."""
    row = _row(layer="Email")
    assert is_ambiguous(row)
    assert classify_is_inferred(row)


def test_activity_is_ambiguous_because_of_the_interview_write_ups():
    """20 live Activity rows, 14 of them per-person interview write-ups on
    sap-5174 that are CONTENTS of the `1:1 Stakeholder Interviews` activity,
    not activities."""
    row = _row(layer="Activity")
    assert is_ambiguous(row)
    assert classify(row) is CardKind.ACTIVITY  # the guess, flagged as a guess


def test_an_explicit_kind_settles_a_straddling_layer():
    row = _row(layer="Email", card_kind="attachment")
    assert not is_ambiguous(row)
    assert classify(row) is CardKind.ATTACHMENT


def test_unambiguous_layers_are_not_flagged():
    for layer in ("Deliverables", "Brief", "Source material", "Synthesis"):
        assert not is_ambiguous(_row(layer=layer)), layer


def test_ambiguous_layers_are_named_not_open_ended():
    """If this set grows, it should be a deliberate edit with evidence — not a
    quiet accretion like the five layer-string lists this module replaces."""
    assert AMBIGUOUS_LAYERS == frozenset({"email", "activity", "activities"})


# --- the property the callers actually use ----------------------------------


def test_is_card_is_true_for_all_three_card_kinds():
    assert is_card(_row(est_item_id="_authored/inputs-briefing"))
    assert is_card(_row(layer="Deliverables"))
    assert is_card(_row(layer="Activity"))
    assert not is_card(_row(layer="Source material"))


def test_card_kind_is_card_property():
    assert CardKind.ENGAGEMENT.is_card
    assert CardKind.ACTIVITY.is_card
    assert CardKind.DELIVERABLE.is_card
    assert not CardKind.ATTACHMENT.is_card


# --- unfilled standing elements are not capture (#179 follow-up) ------------
#
# Found during the 2026-09-15 backfill: three `_authored/sow` v1 rows are the
# tenant's own scaffolding — 181 chars, one source, no rag_asset. Both of
# `_is_capture`'s signals fired, so they classified as Stream, and `stub_sweep`
# would then have proposed RETIRING the slots that exist to be filled in.

_PLACEHOLDER_BODY = (
    "_Standing element — the signed scope of work._\n\n"
    "What belongs here: the executed SOW (attach the document as a source), "
    "key commitments, exclusions, and change orders as they happen."
)


def test_unfilled_standing_element_is_reference_not_capture():
    """The live sap-5174/ibx-5192/sap-5171 `_authored/sow` v1 shape."""
    row = _row(layer="Agreement", placement="context",
               body=_PLACEHOLDER_BODY,
               sources=[{"id": "abc", "title": "SOW.pdf"}])
    assert len(row["body"]) < 200, "fixture must be short enough to look like capture"
    assert classify(row) is CardKind.REFERENCE
    assert not classify(row).is_stream, (
        "a standing element awaiting content must never be swept as a stub")


def test_filled_standing_element_stays_reference():
    """The marker alone must not decide it — sap-5174's filled SOW v2 is 1,771
    chars and carries no instructional line."""
    row = _row(layer="Agreement", placement="context",
               body="_Standing element — the signed scope of work._\n\n" + ("x" * 2000),
               sources=[{"id": "abc"}])
    assert classify(row) is CardKind.REFERENCE


def test_real_capture_is_still_stream():
    """The guard must not have widened into a hole: the ingest's own wrapper,
    which is what `stub_sweep` exists to find, still classifies as Stream."""
    row = _row(layer="Source material", placement="context",
               body="Ingested document: **Marcello Grande** (doc)\n\n"
                    "rag_asset: 1fb5e23e-0cfe-4d85-87e8-903d46c48a33",
               sources=[{"id": "1fb5e23e"}])
    assert classify(row) is CardKind.ATTACHMENT
    assert classify(row).is_stream


def test_placeholder_needs_both_signals():
    """Marker without instructions, or instructions without marker, is not a
    placeholder — neither alone separated the 20 live rows."""
    from cp_engine.card_class import is_unfilled_placeholder
    assert not is_unfilled_placeholder({"body": "_Standing element — the SOW._"})
    assert not is_unfilled_placeholder({"body": "What belongs here: anything"})
    assert is_unfilled_placeholder({"body": _PLACEHOLDER_BODY})
