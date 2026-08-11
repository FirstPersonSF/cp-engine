"""Tests for ``cp seal-sweep`` (#175).

Shapes are taken from the real ibx-5192 spine (queried 2026-08-11), not
invented — the standing lesson from the feedback splitter and the
doc-comment mapper is that fixtures with sensible data hide the flaws real
projects expose.
"""

from __future__ import annotations

from datetime import date, timedelta

from cp_engine.seal_sweep import (
    DEFAULT_SHIPPED_WITHIN_DAYS,
    build_rounds,
    render_sweep,
)

TODAY = date(2026, 8, 11)

# The two decks that shipped on 2026-08-10, and a sample of what fed them.
DECK_MEHUL = "_authored/srs-arc-b-friday-deck-build-unblocked-slide-content"
DECK_JAIME = "_authored/main-stage-explainer-jaime-outline-v02"
FEEDBACK_MEHUL = "_authored/mehul-feedback-on-r4-deck-worklist-from-the-8-5-team-review"
PILLAR_RULING = "_authored/pillar-ruling-4-platform-pillars-overrides-the-3-doors-recommendation"
AGREED_FLOW = "_authored/srs-platform-pitch-the-agreed-flow-from-deck-r01-7-20"
JAIME_OUTLINE_V2 = "_authored/jaime-s-outline-v2-from-kimber"


def _row(eid, framing, layer, version_label=None, version_date=None):
    return {
        "est_item_id": eid,
        "framing": framing,
        "layer": layer,
        "status": "live",
        "archived": False,
        "version_label": version_label,
        "version_date": version_date,
    }


def _edge(kind, src, dst):
    return {"kind": kind, "from_item_id": src, "to_item_id": dst}


def _base_rows():
    return [
        _row(DECK_MEHUL, "Mehul — SRS field deck", "Deliverables", "v6", "2026-08-10"),
        _row(DECK_JAIME, "Jaime — Main Stage Explainer", "Deliverables", "v9", "2026-08-10"),
        _row(FEEDBACK_MEHUL, "Mehul feedback on r4 deck", "Client feedback"),
        _row(PILLAR_RULING, "Pillar ruling: 4 platform pillars", "Decision"),
        _row(AGREED_FLOW, "SRS platform pitch — the agreed flow", "Synthesis"),
        _row(JAIME_OUTLINE_V2, "Jaime's outline v2 from Kimber", "Client feedback"),
    ]


def test_shipped_deliverable_surfaces_its_inputs():
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
            _edge("derives_from", AGREED_FLOW, DECK_MEHUL),
        ],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    assert {c.est_item_id for c in mehul.candidates} == {PILLAR_RULING, AGREED_FLOW}


def test_responds_to_counts_as_consumption():
    """The kind the streamlining plan missed.

    On the real project 4 of 12 edges into the two live decks are
    `responds_to` — the feedback a version answered. A sweep that only walked
    informs/derives_from would miss most of what a round actually consumes.
    """
    rounds = build_rounds(
        _base_rows(),
        [_edge("responds_to", FEEDBACK_MEHUL, DECK_MEHUL)],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    assert [c.est_item_id for c in mehul.candidates] == [FEEDBACK_MEHUL]


def test_derives_from_outranks_informs_outranks_responds_to():
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("responds_to", FEEDBACK_MEHUL, DECK_MEHUL),
            _edge("derives_from", AGREED_FLOW, DECK_MEHUL),
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
        ],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    assert [c.est_item_id for c in mehul.candidates] == [
        AGREED_FLOW,
        PILLAR_RULING,
        FEEDBACK_MEHUL,
    ]


def test_multiple_edges_to_one_deliverable_collapse_to_one_candidate():
    """The real data has this: Mehul's feedback card carries BOTH
    `responds_to` and `absorbed_by` toward the same deck. Two edges must not
    become two review rows for the same element."""
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("responds_to", PILLAR_RULING, DECK_MEHUL),
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
        ],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    assert len(mehul.candidates) == 1
    assert mehul.candidates[0].evidence == "informs+responds_to"


def test_already_absorbed_inputs_are_not_reproposed():
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("responds_to", FEEDBACK_MEHUL, DECK_MEHUL),
            _edge("absorbed_by", FEEDBACK_MEHUL, DECK_MEHUL),
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
        ],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    assert [c.est_item_id for c in mehul.candidates] == [PILLAR_RULING]
    assert mehul.already_absorbed == 1


def test_input_still_feeding_an_unshipped_deliverable_is_held_back():
    """#164 step 2: don't close an input another open round is living on."""
    rows = _base_rows() + [
        _row("_authored/next-deck", "Next deck (no version yet)", "Deliverables")
    ]
    rounds = build_rounds(
        rows,
        [
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
            _edge("informs", PILLAR_RULING, "_authored/next-deck"),
        ],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    assert mehul.candidates == []


def test_deliverable_outside_the_window_is_skipped_unless_all():
    stale = _row(
        "_authored/old-deliverable", "Old deliverable", "Deliverables", "v1", "2026-06-01"
    )
    rows = _base_rows() + [stale]
    edges = [_edge("informs", PILLAR_RULING, "_authored/old-deliverable")]

    windowed = build_rounds(rows, edges, today=TODAY)
    assert "_authored/old-deliverable" not in {r.est_item_id for r in windowed}

    everything = build_rounds(rows, edges, today=TODAY, all_deliverables=True)
    assert "_authored/old-deliverable" in {r.est_item_id for r in everything}


def test_output_layer_alias_is_still_recognised():
    """mig 137 folded `Output` into `Deliverables`, but historical rows and
    un-migrated tenants still carry it (#172)."""
    rows = [
        _row("_authored/legacy", "Legacy output", "Output", "v2", "2026-08-10"),
        _row(PILLAR_RULING, "Pillar ruling", "Decision"),
    ]
    rounds = build_rounds(
        rows, [_edge("informs", PILLAR_RULING, "_authored/legacy")], today=TODAY
    )
    assert [r.est_item_id for r in rounds] == ["_authored/legacy"]


def test_non_deliverable_layers_are_never_rounds():
    rows = [
        _row(PILLAR_RULING, "Pillar ruling", "Decision", "v2", "2026-08-10"),
        _row(AGREED_FLOW, "Agreed flow", "Synthesis", "v1", "2026-08-10"),
    ]
    assert build_rounds(rows, [], today=TODAY) == []


def test_undated_deliverable_needs_all_flag():
    """No version_date means the recency window can't judge it — it must not
    silently pass as 'recent'."""
    rows = [_row("_authored/undated", "Undated deck", "Deliverables", "v1", None)]
    assert build_rounds(rows, [], today=TODAY) == []
    assert len(build_rounds(rows, [], today=TODAY, all_deliverables=True)) == 1


def test_window_boundary_is_inclusive():
    edge_day = (TODAY - timedelta(days=DEFAULT_SHIPPED_WITHIN_DAYS)).isoformat()
    rows = [_row("_authored/edge", "Edge deck", "Deliverables", "v1", edge_day)]
    assert len(build_rounds(rows, [], today=TODAY)) == 1


def test_rounds_sort_newest_first():
    rows = [
        _row("_authored/older", "Older", "Deliverables", "v1", "2026-08-04"),
        _row("_authored/newer", "Newer", "Deliverables", "v2", "2026-08-10"),
    ]
    rounds = build_rounds(rows, [], today=TODAY)
    assert [r.est_item_id for r in rounds] == ["_authored/newer", "_authored/older"]


def test_render_names_the_seal_call_with_every_candidate():
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
            _edge("derives_from", AGREED_FLOW, DECK_MEHUL),
        ],
        today=TODAY,
    )
    text = render_sweep(rounds, code="ibx-5192")
    assert "seal_to_deliverable(" in text
    assert PILLAR_RULING in text
    assert AGREED_FLOW in text
    assert 'deliverable_key="' + DECK_MEHUL in text

    # The rendered call is meant to be COPY-PASTED, so it has to parse.
    # The first live run emitted a space-separated list — valid-looking and
    # not valid Python — which every "is the key in the text?" assertion
    # happily passed.
    import ast

    call = next(
        line.strip() for line in text.splitlines()
        if line.strip().startswith("seal_to_deliverable(")
    )
    parsed = ast.parse(call, mode="eval").body
    assert isinstance(parsed, ast.Call)
    kwargs = {kw.arg: kw.value for kw in parsed.keywords}
    keys = [el.value for el in kwargs["absorbed_keys"].elts]
    assert keys == [AGREED_FLOW, PILLAR_RULING]


def test_render_says_so_when_a_round_is_already_compressed():
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
            _edge("absorbed_by", PILLAR_RULING, DECK_MEHUL),
        ],
        today=TODAY,
    )
    text = render_sweep(rounds, code="ibx-5192")
    assert "already compressed" in text
    # A compressed round must not emit a seal command with an empty list.
    assert "absorbed_keys=[]" not in text


def test_a_round_with_no_edges_reads_as_blind_not_clean():
    """sap-5171 has 27 live elements, 2 versioned deliverables and ZERO feed
    edges. Reporting that as 'already compressed' would tell the exact lie
    this sweep exists to prevent — the live run is what surfaced it."""
    rows = [_row("_authored/d", "A deck", "Deliverables", "v1", "2026-08-10")]
    text = render_sweep(build_rounds(rows, [], today=TODAY), code="sap-5171")
    assert "already compressed" not in text
    assert "No edges into this deliverable" in text
    assert "the sweep is blind" in text


def test_a_round_whose_inputs_are_all_absorbed_reads_as_compressed():
    rounds = build_rounds(
        _base_rows(),
        [
            _edge("informs", PILLAR_RULING, DECK_MEHUL),
            _edge("absorbed_by", PILLAR_RULING, DECK_MEHUL),
        ],
        today=TODAY,
    )
    mehul = next(r for r in rounds if r.est_item_id == DECK_MEHUL)
    text = render_sweep([mehul], code="ibx-5192")
    assert "already compressed" in text
    assert "the sweep is blind" not in text


def test_render_empty_points_at_the_all_flag():
    text = render_sweep([], code="ibx-5192")
    assert "Nothing to seal" in text
    assert "--all" in text
