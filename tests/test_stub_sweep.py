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


# ──────────────────────────────────────────────────────────────────────
#  #275 — founding material filed against one dated session
# ──────────────────────────────────────────────────────────────────────


def _work(eid, framing, layer, version_date):
    """A real work element — not Source material, so it sets the date floor."""
    row = _row(eid, framing, layer, body="genuine authored content")
    row["version_date"] = version_date
    return row


def test_founding_material_on_a_dated_session_is_flagged():
    """THE #275 CONTROL — the case no check could see.

    ibx-5153: 17 stubs routed to the 07-03 "Campaign Opportunities Report
    walkthrough", every one PREDATING it, so `postdates_target` was correctly
    silent. They were the Apr 23 kickoff briefing, the 4/10 and 4/23 input
    briefs, the first pitch and an analyst report — founding material filed
    against a client feedback session three months later.
    """
    walkthrough = _work("_authored/walkthrough", "Campaign Opportunities "
                        "Report walkthrough", "Client feedback", "2026-07-03")
    workshop = _work("_authored/workshop", "6/17 AI Campaign Workshop",
                     "Activity", "2026-06-17")
    stub = _stub(serves=["_authored/walkthrough"])
    stubs = find_stubs([stub, walkthrough, workshop],
                       source_dates={RAG: "2026-06-13"})
    assert stubs[0].looks_foundational
    text = render_sweep(stubs, code="ibx-5153")
    assert "LOOKS FOUNDATIONAL" in text
    assert "Inputs & Briefing" in text


def test_a_document_from_during_the_work_is_not_flagged():
    """The common case. A source that arrived while the project was running is
    ordinary input to whatever it was routed to."""
    act = _work("_authored/deck-build", "Deck build", "Activity", "2026-06-17")
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, act], source_dates={RAG: "2026-07-20"})
    assert not stubs[0].looks_foundational


def test_a_document_from_the_kickoff_day_itself_is_not_flagged():
    """Equal dates are normal: the brief and the first activity land together.
    Only strictly-earlier is a signal, or every kickoff trips it."""
    act = _work("_authored/kickoff", "Kickoff", "Activity", "2026-06-17")
    stub = _stub(serves=["_authored/kickoff"])
    stubs = find_stubs([stub, act], source_dates={RAG: "2026-06-17"})
    assert not stubs[0].looks_foundational


def test_source_material_does_not_set_the_date_floor():
    """Capture is not work. If Source-material cards counted, the founding
    documents would define the very date they are tested against — and the
    check could never fire."""
    early_stub = _row("_authored/early-capture", "early", "Source material",
                      body=BOILERPLATE)
    early_stub["version_date"] = "2026-01-01"
    act = _work("_authored/deck-build", "Deck build", "Activity", "2026-06-17")
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, early_stub, act],
                       source_dates={RAG: "2026-06-13"})
    target = next(s for s in stubs if s.est_item_id == stub["est_item_id"])
    assert target.looks_foundational, (
        "a Source-material card must not lower the work-date floor"
    )


def test_the_two_checks_do_not_both_fire():
    """`postdates_target` and `looks_foundational` are opposite errors.

    A document cannot both predate the project's first work and postdate the
    target it serves — but if it could, showing two contradictory flags on one
    card would be worse than showing neither.
    """
    act = _work("_authored/deck-build", "Deck build", "Activity", "2026-06-17")
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, act], source_dates={RAG: "2026-07-20"})
    text = render_sweep(stubs, code="x")
    assert not ("DOC ARRIVED AFTER" in text and "LOOKS FOUNDATIONAL" in text)


def test_it_is_a_question_not_a_verdict():
    """#274's lesson: a confidently-worded flag gets acted on in bulk.

    'Almost certainly a bulk route' is what made a 70-stub migration look
    actionable when 43 of 50 were correctly routed. This one asks.
    """
    walkthrough = _work("_authored/w", "Walkthrough", "Client feedback",
                        "2026-07-03")
    stub = _stub(serves=["_authored/w"])
    text = render_sweep(
        find_stubs([stub, walkthrough], source_dates={RAG: "2026-06-13"}),
        code="ibx-5153",
    )
    assert "ℹ" in text and "⚠ LOOKS FOUNDATIONAL" not in text
    assert text.count("Is `Inputs & Briefing` its home?") == 1


def test_no_work_element_means_no_claim():
    """With nothing to compare against, the check stays silent."""
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs([stub, ACTIVITY], source_dates={RAG: "2026-06-13"})
    assert not stubs[0].looks_foundational


# ──────────────────────────────────────────────────────────────────────
#  #179 option 3 — a work-item link-carrier is not a mistake
# ──────────────────────────────────────────────────────────────────────


def test_a_card_serving_a_work_item_is_named_as_a_link_carrier():
    """A work item has no spine row, so no `sources` array to attach to.

    Routing a document there MINTS a card to hold the pointer — deliberately;
    `routeTarget.ts` keeps the mint alive for exactly this case. The result is
    byte-identical to a mistaken Source-material stub and is not one.
    """
    stub = _stub(serves=["estimator-slot-uuid"])  # resolves to no live element
    stubs = find_stubs([stub])
    assert stubs[0].carries_a_work_item_link
    assert stubs[0].orphan  # still true — this says WHY, it does not replace it
    text = render_sweep(stubs, code="ibx-5192")
    assert "carry a WORK-ITEM LINK" in text


def test_a_card_routed_to_a_real_element_is_not_a_link_carrier():
    """Those never needed a card: the target has a `sources` array already.

    24 of 42 live bindings are this case — the attach path existed and routing
    did not take it.
    """
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    assert not stubs[0].carries_a_work_item_link
    assert stubs[0].attachable


def test_a_card_with_no_source_is_not_a_link_carrier():
    """A card carrying no rag_asset has no link to carry — it is just empty."""
    row = _row("_authored/empty", "Empty", "Source material", body=BOILERPLATE,
               serves=["estimator-slot-uuid"])
    row["sources"] = []
    stubs = find_stubs([row])
    assert not stubs[0].carries_a_work_item_link


def test_the_orphan_guidance_separates_carriers_from_the_rest():
    """Calling all orphans 'needs a judgement' overstates the backlog.

    Measured on ibx-5192: 11 of 14 orphans are link-carriers. Telling an
    operator all 14 need a decision invites retiring a card whose only job is
    to hold a pointer nothing else can hold.
    """
    carrier = _stub(eid="_authored/carrier", serves=["estimator-slot-uuid"])
    bare = _row("_authored/bare", "Bare", "Source material", body=BOILERPLATE,
                serves=["another-slot"])
    bare["sources"] = []
    text = render_sweep(find_stubs([carrier, bare]), code="ibx-5192")
    assert "1 of these carry a WORK-ITEM LINK" in text
    assert "The rest need a judgement" in text


# ──────────────────────────────────────────────────────────────────────
#  #179 option 3, part 2 — the STORED kind, and the self-clearing rule
# ──────────────────────────────────────────────────────────────────────


def _carrier(eid="_authored/marcello-grande", serves=None, stored=True):
    """A minted link-carrier as the mint now writes it: card_kind='link'."""
    row = _stub(eid, serves=serves or ["estimator-slot-uuid"])
    if stored:
        row["card_kind"] = "link"
    return row


def _bound_deliverable(eid, serves):
    row = _row(eid, "Concept deck", "Deliverables", body="x" * 900, serves=serves)
    row["card_kind"] = "deliverable"
    return row


def test_a_stored_link_is_a_carrier_without_needing_the_derivation():
    """The whole point of the stored kind (#179 option 3).

    `carries_a_work_item_link` otherwise reads `unresolved and sources`, which
    cannot tell a work-item mint from a card minted for a slot somebody later
    deleted. A heuristic of that family already misfired on live data (#269).
    """
    row = _carrier()
    row["sources"] = []          # the derivation would say False on both counts
    row["serves"] = []
    stubs = find_stubs([row])
    assert stubs[0].carries_a_work_item_link


def test_a_stored_link_still_enters_the_sweep():
    """`CardKind.LINK.is_stream` is False, so the class is NOT swept as capture.

    It must still be a CANDIDATE, or `link_has_a_home` could never notice that
    the carrier has come free — the row would simply vanish from the report.
    """
    assert find_stubs([_carrier()]), "a stored link-carrier must be swept"


def test_a_carrier_is_held_while_its_work_item_has_no_bound_element():
    """Not retirable merely for being a carrier — that drops the pointer."""
    stubs = find_stubs([_carrier(serves=["slot-uuid"])])
    assert stubs[0].carries_a_work_item_link
    assert not stubs[0].link_has_a_home
    text = render_sweep(stubs, code="ibx-5192")
    assert "carry a WORK-ITEM LINK" in text
    assert "now FREE" not in text


def test_a_carrier_comes_FREE_when_its_work_item_gains_a_bound_element():
    """The self-clearing half. The document now has a real home, so the card
    is redundant rather than load-bearing, and the sweep may propose the move."""
    stubs = find_stubs([
        _carrier(serves=["slot-uuid"]),
        _bound_deliverable("_authored/concept-deck", serves=["slot-uuid"]),
    ])
    carrier = next(s for s in stubs if s.est_item_id == "_authored/marcello-grande")
    assert carrier.link_has_a_home
    assert carrier._link_home == "_authored/concept-deck"
    text = render_sweep(stubs, code="ibx-5192")
    assert "now FREE" in text
    assert "_authored/concept-deck" in text


def test_two_bound_elements_do_not_free_a_carrier():
    """Ambiguity is a guess about WHICH deliverable a document belongs to.

    Same rule the frontend's `boundElementIndex` applies, for the same reason:
    one live work item is bound by four distinct concept docs.
    """
    stubs = find_stubs([
        _carrier(serves=["slot-uuid"]),
        _bound_deliverable("_authored/concept-a", serves=["slot-uuid"]),
        _bound_deliverable("_authored/concept-b", serves=["slot-uuid"]),
    ])
    carrier = next(s for s in stubs if s.est_item_id == "_authored/marcello-grande")
    assert not carrier.link_has_a_home


def test_a_capture_card_does_not_free_a_carrier():
    """Work-class only: resolving onto another capture card moves the problem."""
    other = _stub("_authored/another-doc", serves=["slot-uuid"])
    other["card_kind"] = "attachment"
    stubs = find_stubs([_carrier(serves=["slot-uuid"]), other])
    carrier = next(s for s in stubs if s.est_item_id == "_authored/marcello-grande")
    assert not carrier.link_has_a_home


def test_a_link_carrier_cannot_date_the_project_s_first_work():
    """#275, which LINK would have reintroduced.

    A carrier is not stream, so the old `not _is_stream(r)` test counted it as
    work — letting a pointer minted today date the work it points at.
    """
    from cp_engine.stub_sweep import _is_work

    carrier = _carrier()
    carrier["version_date"] = "2026-09-16"
    assert not _is_work(carrier)
