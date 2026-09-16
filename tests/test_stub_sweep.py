"""Tests for ``cp stub-sweep`` (#178).

Shapes taken from the real tenant (queried 2026-08-11): every stub's body is
the ingest boilerplate ``Ingested document: **X** (doc)\\n\\nrag_asset: <uuid>``
with the SAME rag_asset already in `sources`, and 79 of 82 carry a `serves`
binding — the fact that makes this a transfer rather than a delete.
"""

from __future__ import annotations

from cp_engine.stub_sweep import find_stubs, render_sweep, verify_transfer

RAG = "1fb5e23e-0cfe-4d85-87e8-903d46c48a33"
BOILERPLATE = f"Ingested document: **Marcello Grande** (doc)\n\nrag_asset: `{RAG}`"


def _row(eid, framing, layer, body="", serves=None, sources=None):
    return {
        "est_item_id": eid,
        "framing": framing,
        "layer": layer,
        "status": "live",
        "archived": False,
        "body": body,
        "serves": serves or [],
        "sources": sources or [],
    }


def _stub(eid="_authored/marcello-grande", serves=None):
    return _row(
        eid, "Marcello Grande", "Source material",
        body=BOILERPLATE, serves=serves or [],
        sources=[{"id": RAG, "type": "rag_asset", "title": "Marcello Grande"}],
    )


ACTIVITY = _row("_authored/deck-build", "Deck build", "Activity")


def test_a_boilerplate_card_serving_a_real_element_is_attachable():
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    assert len(stubs) == 1
    s = stubs[0]
    assert s.attachable
    assert not s.orphan
    assert s.targets == [("_authored/deck-build", "Deck build", True)]


def test_a_stub_serving_a_bare_estimate_slot_is_an_orphan():
    """48 of the tenant's routings point at a slot that is not a spine
    element. There is nothing to attach to, so nothing may be proposed —
    inventing a target is the mistake that killed #174's backfill."""
    stubs = find_stubs([_stub(serves=["some-estimate-slot-uuid"])])
    assert stubs[0].orphan
    assert not stubs[0].attachable
    assert stubs[0].unresolved == ["some-estimate-slot-uuid"]


def test_a_stub_routed_nowhere_at_all_is_an_orphan():
    stubs = find_stubs([_stub(serves=[])])
    assert stubs[0].orphan


def test_a_hand_written_short_note_is_never_a_stub():
    """Thin is not empty. Only the ingest's own boilerplate qualifies — a
    short human note is somebody's thought, and not this sweep's call."""
    rows = [_row("_authored/note", "A real thought", "Source material",
                 body="Carol's framework is the alignment target.")]
    assert find_stubs(rows) == []


def test_a_long_source_card_is_never_a_stub():
    rows = [_row("_authored/big", "A distilled source", "Source material",
                 body="Ingested document: **X** (doc)\n\n" + "x" * 500)]
    assert find_stubs(rows) == []


def test_non_source_layers_are_ignored():
    rows = [_row("_authored/d", "A deck", "Deliverables", body=BOILERPLATE)]
    assert find_stubs(rows) == []


def test_a_stub_serving_itself_does_not_become_its_own_target():
    eid = "_authored/self"
    stubs = find_stubs([_stub(eid=eid, serves=[eid])])
    assert stubs[0].orphan
    assert stubs[0].targets == []


def test_typed_edges_are_flagged_because_retiring_cascades_them():
    """`retire` deletes a card's edges. A stub that something points at is
    not safe to retire silently."""
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs(
        [stub, ACTIVITY],
        [{"kind": "informs", "from_item_id": stub["est_item_id"],
          "to_item_id": "_authored/deck-build"}],
    )
    assert stubs[0].has_edges
    assert "has typed edges" in render_sweep(stubs, code="ibx-5192")


def test_a_stub_with_no_sources_is_not_attachable():
    """Nothing to transfer — retiring it would lose nothing, but the sweep
    must not claim there is provenance to move."""
    row = _row("_authored/empty", "Empty", "Source material",
               body=BOILERPLATE, serves=["_authored/deck-build"], sources=[])
    stubs = find_stubs([row, ACTIVITY])
    assert not stubs[0].attachable


def test_render_names_the_source_and_the_destination():
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    text = render_sweep(stubs, code="ibx-5192")
    assert "Marcello Grande" in text
    assert "→ serves: Deck build" in text
    assert "attachable" in text


def test_render_keeps_orphans_visible_and_says_why():
    stubs = find_stubs([_stub(serves=["bare-slot"])])
    text = render_sweep(stubs, code="ibx-5192")
    assert "bare estimate slot" in text
    assert "Inventing a target would be a guess." in text


def test_attachable_stubs_sort_before_orphans():
    rows = [
        _stub(eid="_authored/z-orphan", serves=[]),
        _stub(eid="_authored/a-attachable", serves=["_authored/deck-build"]),
        ACTIVITY,
    ]
    stubs = find_stubs(rows)
    assert [s.est_item_id for s in stubs] == [
        "_authored/a-attachable", "_authored/z-orphan"]


def test_render_empty_is_quiet():
    assert "no empty Source-material cards" in render_sweep([], code="ibx-5192")


UNLAYERED = _row("5fca0b9c", "This is our post meeting conversation…", None)


def test_a_target_with_no_layer_is_flagged_as_unsound():
    """34 of the 65 attachable stubs route to a card with `layer: null` —
    one spine-lint already calls unfilable. Moving provenance onto it buries
    the document in a card that is itself broken, so the migration has an
    ordering constraint: fix the destination first."""
    stubs = find_stubs([_stub(serves=["5fca0b9c"]), UNLAYERED])
    assert stubs[0].attachable          # it still HAS somewhere to go...
    assert stubs[0].unsound_targets     # ...but that somewhere is broken
    text = render_sweep(stubs, code="ibx-5192")
    assert "UNLAYERED TARGET" in text
    assert "Fix the destination's layer first" in text


def test_a_sound_target_produces_no_warning():
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    assert stubs[0].unsound_targets == []
    text = render_sweep(stubs, code="ibx-5192")
    assert "UNLAYERED TARGET" not in text
    assert "Fix the destination" not in text


def _dated(eid, framing, layer, version_date):
    row = _row(eid, framing, layer)
    row["version_date"] = version_date
    return row


def test_a_doc_newer_than_its_target_is_flagged():
    """A source cannot have fed a session that predates it.

    Real on ibx-5192: 8 use-case briefs ingested 2026-07-21 were routed to a
    2026-06-27 debrief. They belonged to the 07-22 metrics-inventory synthesis.
    """
    act = _dated("_authored/deck-build", "Post-Mehul debrief", "Activity", "2026-06-27")
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, act], source_dates={RAG: "2026-07-21"})
    assert stubs[0].postdates_target
    text = render_sweep(stubs, code="ibx-5192")
    assert "DOC ARRIVED AFTER" in text


def test_a_doc_predating_its_target_is_clean():
    """sap-5174's kickoff has 14 of 18 docs predating it — real curation."""
    act = _dated("_authored/deck-build", "Kickoff meeting", "Activity", "2026-07-08")
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, act], source_dates={RAG: "2026-07-01"})
    assert not stubs[0].postdates_target
    assert "DOC ARRIVED AFTER" not in render_sweep(stubs, code="sap-5174")


def test_a_backfilled_stub_is_not_flagged_when_its_doc_is_old(monkeypatch):
    """THE #274 REGRESSION. The card is new; the document is not.

    ibx-5153 minted 47 stubs in a four-minute window on 2026-08-17 for
    documents that had arrived 2026-06-17 — the workshop's own agenda, bet
    board and transcript, routed to that workshop. Comparing the STUB's
    version_date reported all of them as bulk routes. Comparing the
    DOCUMENT's arrival date reports none.
    """
    act = _dated("_authored/workshop", "6/17 AI Campaign Workshop", "Activity",
                 "2026-06-17")
    stub = _stub(serves=["_authored/workshop"])
    stub["version_date"] = "2026-08-17"          # the backfill stamp
    stubs = find_stubs([stub, act], source_dates={RAG: "2026-06-17"})
    assert not stubs[0].postdates_target, (
        "a stub created by a later backfill must not be flagged when the "
        "document it wraps predates its target"
    )
    assert "DOC ARRIVED AFTER" not in render_sweep(stubs, code="ibx-5153")


def test_the_check_is_silent_when_the_source_date_is_unknown():
    """An unresolved arrival date is not evidence. No date, no claim."""
    act = _dated("_authored/deck-build", "Post-Mehul debrief", "Activity", "2026-06-27")
    stub = _stub(serves=["_authored/deck-build"])
    stub["version_date"] = "2026-08-17"
    stubs = find_stubs([stub, act])          # no source_dates supplied
    assert not stubs[0].postdates_target
    assert "DOC ARRIVED AFTER" not in render_sweep(stubs, code="ibx-5153")


def test_the_earliest_source_date_decides():
    """A card wrapping several docs is exonerated by its OLDEST one.

    It has fed its target from the moment the first document landed; taking
    the latest would flag a card whose bulk of material predates the work.
    """
    act = _dated("_authored/deck-build", "Debrief", "Activity", "2026-07-01")
    stub = _stub(serves=["_authored/deck-build"])
    stub["sources"] = [
        {"id": "aaa", "type": "rag_asset", "title": "early"},
        {"id": "bbb", "type": "rag_asset", "title": "late"},
    ]
    stubs = find_stubs(
        [stub, act], source_dates={"aaa": "2026-06-20", "bbb": "2026-07-21"}
    )
    assert not stubs[0].postdates_target


def test_missing_dates_never_claim_a_violation():
    """Absent data is not evidence of a bulk route."""
    act = _dated("_authored/deck-build", "Some activity", "Activity", None)
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, act])
    assert not stubs[0].postdates_target


# ──────────────────────────────────────────────────────────────────────
#  #276 — a multi-source card must not read as one document
# ──────────────────────────────────────────────────────────────────────


def _multi(eid, titles, serves):
    """A stub wrapping several documents — the shape that hid in the output."""
    row = _row(eid, eid.split("/")[-1], "Source material", body=BOILERPLATE,
               serves=serves)
    row["sources"] = [
        {"id": str(i), "type": "rag_asset", "title": t}
        for i, t in enumerate(titles, start=1)
    ]
    return row


def test_a_multi_source_card_lists_every_document():
    """THE #276 CONTROL. Joined onto one line, four documents read as one.

    A transfer list built from the printed titles moved one and retired the
    card; the other three lost their only pointer. Near-missed on ibx-5153's
    `carol-s-our-ai-story-narrative-prose`, caught by a hand-written query
    rather than by this output.
    """
    stub = _multi("_authored/carol", [
        "Our AI Story - Final V1.docx", "Our AI Story Deck.pdf",
        "Our AI Story.docx", "Our AI Story - Jun 2026.docx",
    ], ["_authored/deck-build"])
    text = render_sweep(find_stubs([stub, ACTIVITY]), code="ibx-5153")
    assert "sources (4):" in text
    for title in ("Final V1", "Deck.pdf", "Jun 2026"):
        assert title in text


def test_a_single_source_card_still_reads_as_one_line():
    """The common case must not become a list of one."""
    text = render_sweep(
        find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY]),
        code="ibx-5192",
    )
    assert "source: Marcello Grande" in text
    assert "sources (" not in text


def test_the_summary_reports_pointers_when_they_exceed_cards():
    """Cards and source pointers are different counts, and the transfer list
    is built from the second one."""
    stub = _multi("_authored/carol", ["a.docx", "b.pdf", "c.docx"],
                  ["_authored/deck-build"])
    text = render_sweep(find_stubs([stub, ACTIVITY]), code="ibx-5153")
    assert "1 empty Source-material card(s) · 3 source pointers" in text


def test_the_summary_stays_one_number_when_they_agree():
    text = render_sweep(
        find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY]),
        code="ibx-5192",
    )
    assert "source pointers" not in text


# ──────────────────────────────────────────────────────────────────────
#  #276 — verify_transfer checks sources AND edges
# ──────────────────────────────────────────────────────────────────────


def test_verify_is_safe_when_every_source_is_on_the_target():
    stub = _stub(serves=["_authored/deck-build"])
    report = verify_transfer(
        find_stubs([stub, ACTIVITY]), "_authored/deck-build",
        [{"id": RAG, "type": "rag_asset", "title": "Marcello Grande"}],
    )
    assert "SAFE TO RETIRE" in report


def test_verify_names_the_sources_that_were_never_moved():
    """The multi-source failure: three of four transferred, one forgotten."""
    stub = _multi("_authored/carol", ["a.docx", "b.pdf", "c.docx"],
                  ["_authored/deck-build"])
    report = verify_transfer(
        find_stubs([stub, ACTIVITY]), "_authored/deck-build",
        [{"id": "1", "title": "a.docx"}, {"id": "2", "title": "b.pdf"}],
    )
    assert "SAFE TO RETIRE" not in report
    assert "c.docx" in report


def test_verify_blocks_a_stub_carrying_a_typed_edge():
    """THE OTHER #276 CONTROL, and the one that was not a near miss.

    A verifier that checked only sources would return SAFE here — which is
    exactly what happened on 2026-09-16, when a source-provenance query
    greenlit a batch that destroyed an edge.
    """
    stub = _stub(serves=["_authored/deck-build"])
    report = verify_transfer(
        find_stubs([stub, ACTIVITY]), "_authored/deck-build",
        [{"id": RAG, "type": "rag_asset", "title": "Marcello Grande"}],
        relations=[{"kind": "derives_from", "status": "active",
                    "from_item_id": "_authored/marcello-grande",
                    "to_item_id": "_authored/deck-build"}],
    )
    assert "SAFE TO RETIRE" not in report
    assert "ACTIVE typed edge" in report


def test_verify_ignores_a_non_active_edge():
    stub = _stub(serves=["_authored/deck-build"])
    report = verify_transfer(
        find_stubs([stub, ACTIVITY]), "_authored/deck-build",
        [{"id": RAG, "type": "rag_asset", "title": "Marcello Grande"}],
        relations=[{"kind": "derives_from", "status": "dismissed",
                    "from_item_id": "_authored/marcello-grande",
                    "to_item_id": "_authored/deck-build"}],
    )
    assert "SAFE TO RETIRE" in report
