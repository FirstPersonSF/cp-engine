# tests/test_spine_lint.py — warn-only spine hygiene checks (#69)
from cp_engine.spine_lint import lint_cp_placeholders, lint_spine_rows


def _row(eid="w1", *, framing=None, layer="Note", binding="live",
         serves=("x",), important=False, body="b", sources=("s",)):
    return {"est_item_id": eid, "framing": framing or eid, "layer": layer,
            "binding": binding, "serves": list(serves),
            "important": important, "body": body, "sources": list(sources)}


def test_clean_spine_yields_nothing():
    rows = [_row(),
            _row("w2", important=True),                      # important + bound
            _row("w3", binding="unbound", serves=())]        # floating, not important
    assert lint_spine_rows(rows) == []


def test_important_unbound_serving_nothing_flags():
    row = _row("_authored/q", framing="Guiding question",
               important=True, binding="unbound", serves=())
    out = lint_spine_rows([row])
    assert len(out) == 1
    assert "important-but-floating" in out[0]
    assert "Guiding question" in out[0] and "_authored/q" in out[0]


def test_agreement_attach_instruction_without_source_flags():
    row = _row("_authored/sow", framing="SOW", layer="Agreement",
               body="Signed document: sow/final.md (attach as source)",
               sources=())
    out = lint_spine_rows([row])
    assert len(out) == 1
    assert "unexecuted attach-instruction" in out[0]
    # The verb moved to the hosted server (#143) — the hint names the connector
    # so the reader doesn't hunt for it on stdio.
    assert "add_element_source" in out[0]
    assert "cp-hosted" in out[0]


def test_agreement_with_source_or_without_instruction_is_quiet():
    with_source = _row(layer="Agreement", body="attach as source",
                       sources=({"type": "rag_asset", "id": "a1"},))
    no_instruction = _row(layer="Agreement", body="human terms", sources=())
    non_agreement = _row(layer="Note", body="attach as source", sources=())
    assert lint_spine_rows([with_source, no_instruction, non_agreement]) == []


def test_attach_instruction_phrasing_variants():
    for body in ("Attach as source", "attached as source",
                 "attach as a source"):
        row = _row(layer="Agreement", body=body, sources=())
        assert lint_spine_rows([row]), body


def test_one_row_can_flag_twice():
    row = _row(layer="Agreement", body="attach as source", sources=(),
               important=True, binding="unbound", serves=())
    assert len(lint_spine_rows([row])) == 2


def test_placeholders_collapse_to_one_warning():
    text = ("# cp\n"
            "- _<one real line about the project>_\n"
            "- real content\n"
            "- _<who owns what>_\n")
    out = lint_cp_placeholders(text)
    assert len(out) == 1
    assert "2 template bullet(s)" in out[0]
    assert "one real line about the project" in out[0]
    assert "+1 more" in out[0]


def test_no_placeholders_no_warning():
    assert lint_cp_placeholders("# cp\n- real line\n") == []
    assert lint_cp_placeholders("") == []


# ── spec-v04 lifecycle checks (#149) ─────────────────────────────────────


def _lrow(eid, **kw):
    base = {"est_item_id": eid, "framing": kw.pop("framing", eid),
            "layer": "Synthesis", "binding": "unbound", "serves": [],
            "important": False, "body": "", "sources": [],
            "version_date": "2026-07-01"}
    base.update(kw)
    return base


def _edge(kind, frm, to):
    return {"kind": kind, "from_item_id": frm, "to_item_id": to}


def test_lifecycle_clean_canon_and_seal():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("brief"), _lrow("a"), _lrow("b"), _lrow("sealed")]
    rels = [_edge("canon_of", "a", "brief"), _edge("canon_of", "b", "brief"),
            _edge("absorbed_by", "sealed", "deliv")]
    assert lint_lifecycle(rows, rels) == []


def test_lifecycle_canon_oversized():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow(f"m{i}") for i in range(8)]
    rels = [_edge("canon_of", f"m{i}", "brief") for i in range(8)]
    out = lint_lifecycle(rows, rels)
    assert len(out) == 1 and "canon oversized" in out[0]


def test_lifecycle_absorbed_but_serving():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("sealed", binding="live", serves=["work-item"])]
    rels = [_edge("absorbed_by", "sealed", "deliv")]
    out = lint_lifecycle(rows, rels)
    assert len(out) == 1 and "absorbed-but-serving" in out[0]


def test_dead_end_activity_is_flagged():
    """#163: an Activity feeding no deliverable strands its whole stream."""
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("discovery", layer="Activity", framing="Stakeholder interviews"),
            _lrow("deck", layer="Deliverables")]
    out = lint_lifecycle(rows, [])
    assert len(out) == 1
    assert "dead-end activity" in out[0]
    assert "Stakeholder interviews" in out[0]


def test_activity_feeding_a_deliverable_is_clean():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("discovery", layer="Activity"), _lrow("deck", layer="Deliverables")]
    assert lint_lifecycle(rows, [_edge("informs", "discovery", "deck")]) == []
    # derives_from counts too — both already carry activity -> deliverable.
    assert lint_lifecycle(rows, [_edge("derives_from", "discovery", "deck")]) == []


def test_activity_feeding_only_a_non_deliverable_is_still_a_dead_end():
    """An edge into another source note is not reaching the work."""
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("discovery", layer="Activity"), _lrow("note"),
            _lrow("deck", layer="Deliverables")]
    out = lint_lifecycle(rows, [_edge("informs", "discovery", "note")])
    assert len(out) == 1 and "dead-end activity" in out[0]


def test_absorbed_activity_is_exempt_from_the_dead_end_check():
    """Absorbed = finished. Flagging it would make every sealed round noisier
    than the last."""
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("discovery", layer="Activity")]
    out = lint_lifecycle(rows, [_edge("absorbed_by", "discovery", "deck")])
    assert out == []


def test_output_layer_counts_as_a_deliverable():
    """Both 'Deliverables' and 'Output' are live layer values in the tenant."""
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("discovery", layer="Activity"), _lrow("deck", layer="Output")]
    assert lint_lifecycle(rows, [_edge("informs", "discovery", "deck")]) == []


def test_lifecycle_stale_canon_member_absorbed_and_superseded():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("old"), _lrow("gone"), _lrow("new")]
    rels = [_edge("canon_of", "old", "brief"),
            _edge("canon_of", "gone", "brief"),
            _edge("supersedes", "new", "old"),
            _edge("absorbed_by", "gone", "deliv")]
    out = lint_lifecycle(rows, rels)
    assert len(out) == 2
    assert all("stale canon member" in w for w in out)


# ── Curation checks (#112 P3 + #158 gaps 2–4) ────────────────────────────

from datetime import date as _d

from cp_engine.spine_lint import lint_curation

_TODAY = _d(2026, 8, 6)


def _crow(**kw) -> dict:
    row = {
        "est_item_id": "e1", "framing": "A card", "layer": "Synthesis",
        "binding": "live", "serves": [], "important": False,
        "body": "x" * 500, "sources": [], "version_date": "2026-08-01",
    }
    row.update(kw)
    return row


def test_curation_flags_scaffold_brief() -> None:
    warns = lint_curation([_crow(
        framing="Inputs & Briefing", layer="Brief", body="- _<fill this in>_",
    )], today=_TODAY)
    assert any("unauthored standing Brief" in w for w in warns)
    # An authored Brief is clean.
    assert not lint_curation([_crow(
        framing="Inputs & Briefing", layer="Brief", body="w" * 900,
    )], today=_TODAY)


def test_curation_flags_past_date_untouched() -> None:
    warns = lint_curation([_crow(
        framing="Feedback we will receive on monday 7-27",
        body="placeholder", version_date="2026-07-20",
    )], today=_TODAY)
    assert any("time-bound and past" in w and "2026-07-27" in w for w in warns)
    # Versioned AFTER the referenced date → the outcome was captured; clean.
    assert not lint_curation([_crow(
        framing="Feedback we will receive on monday 7-27",
        body="the outcome", version_date="2026-07-28",
    )], today=_TODAY)
    # Future dates never flag.
    assert not lint_curation([_crow(
        framing="Workshop on 9/15", version_date="2026-08-01",
    )], today=_TODAY)


def test_curation_flags_raw_paste_on_distill_layer() -> None:
    warns = lint_curation([_crow(
        framing="Feedback from Janet + Mehul r3", layer="ClientFeedback",
        body="y" * 17_000,
    )], today=_TODAY)
    assert any("undistilled capture" in w for w in warns)
    # Same size on SourceMaterial (a pointer layer) is fine.
    assert not lint_curation([_crow(
        framing="Raw deck text", layer="SourceMaterial", body="y" * 17_000,
    )], today=_TODAY)


def test_curation_flags_unlayered_and_instruction_framing() -> None:
    warns = lint_curation([_crow(
        framing="This is our post meeting conversation. You can capture our "
                "initial reactions",
        layer=None,
    )], today=_TODAY)
    assert any("unlayered element" in w for w in warns)
    assert any("instruction-shaped framing" in w for w in warns)


def test_curation_clean_rows_are_clean() -> None:
    assert lint_curation([_crow()], today=_TODAY) == []


# --- #177: classification checks on the Deliverables layer -----------------
# Every framing below was observed live on 2026-08-11.


def test_deliverables_layer_flags_event_and_reaction_framings() -> None:
    for framing in (
        "Kick off and direction for Geoff Ahmann for the PPT design",
        "Clarification on the deliverable for Mehul",
        "Initial feedback on Derek's designs",
        "Notes from the 8/5 team review",
        "Post-meeting debrief",
    ):
        warns = lint_curation(
            [_crow(framing=framing, layer="Deliverables", version_label="v2",
                   body="x" * 3000)],
            today=_TODAY,
        )
        assert any("misfiled on Deliverables" in w for w in warns), framing


def test_a_real_deliverable_mentioning_feedback_is_not_flagged() -> None:
    """'Response to Platform Narrative Feedback' IS a deliverable — a
    client-facing communication. The check is anchored to the start of the
    framing so a deliverable may mention feedback without being it."""
    warns = lint_curation(
        [_crow(framing="SRS Response to Platform Narrative Feedback — v02",
               layer="Deliverables", version_label="v3", body="x" * 3000)],
        today=_TODAY,
    )
    assert not any("misfiled on Deliverables" in w for w in warns)


def test_thin_v1_deliverable_pointing_nowhere_is_flagged() -> None:
    warns = lint_curation(
        [_crow(framing="Campaign concepts", layer="Deliverables",
               version_label="v1", body="Some concepts.", sources=[])],
        today=_TODAY,
    )
    assert any("empty deliverable" in w for w in warns)


def test_a_thin_POINTER_card_is_not_flagged() -> None:
    """The first live run flagged seven of these on ibx-5153 — every one a
    real, delivered deliverable whose substance lives in a file. A card that
    says WHERE the work is is doing its job; thin is not the same as empty."""
    for body in (
        "Campaign brief operationalizing the foundation. Delivered 6/12. "
        "See synthesis-docs/storyos_campaign_brief.md.",
        "~30-min pre-read for attendees: strategic inputs, candidate "
        "directions, the five decisions needed. Delivered 6/12.",
        "Client-owned; final map due 6/26. We reconcile against it.",
    ):
        warns = lint_curation(
            [_crow(framing="A deliverable", layer="Deliverables",
                   version_label="v1", body=body, sources=[])],
            today=_TODAY,
        )
        assert not any("empty deliverable" in w for w in warns), body[:40]


def test_a_thin_card_with_an_attached_source_is_not_flagged() -> None:
    warns = lint_curation(
        [_crow(framing="A deliverable", layer="Deliverables",
               version_label="v1", body="Short.",
               sources=[{"title": "the actual deck.pptx"}])],
        today=_TODAY,
    )
    assert not any("empty deliverable" in w for w in warns)


def test_a_substantial_v1_deliverable_is_left_alone() -> None:
    """13 of the tenant's 19 shipped deliverables sit at v1; the fat ones are
    genuine work that shipped once. Flagging them all would drown the lint."""
    warns = lint_curation(
        [_crow(framing="Workshop Master Run of Show v05", layer="Deliverables",
               version_label="v1", body="x" * 16_000)],
        today=_TODAY,
    )
    assert not any("empty deliverable" in w for w in warns)


def test_a_versioned_deliverable_is_never_stub_flagged() -> None:
    warns = lint_curation(
        [_crow(framing="Mehul — SRS field deck", layer="Deliverables",
               version_label="v6", body="x" * 100)],
        today=_TODAY,
    )
    assert not any("empty deliverable" in w for w in warns)


def test_output_alias_gets_the_same_classification_checks() -> None:
    warns = lint_curation(
        [_crow(framing="Initial feedback on the designs", layer="Output",
               version_label="v2", body="x" * 3000)],
        today=_TODAY,
    )
    assert any("misfiled on Deliverables" in w for w in warns)


def test_classification_checks_do_not_fire_off_the_deliverables_layer() -> None:
    warns = lint_curation(
        [_crow(framing="Initial feedback on Derek's designs",
               layer="Client feedback", version_label="v1", body="x" * 100)],
        today=_TODAY,
    )
    assert not any("misfiled on Deliverables" in w or "empty deliverable" in w
                   for w in warns)


def test_misfiled_wins_over_thin_so_one_card_gets_one_verdict() -> None:
    """A thin, event-shaped card is misfiled — saying both would double-count
    the same defect and inflate the warning list."""
    warns = lint_curation(
        [_crow(framing="Kick off and direction for Geoff", layer="Deliverables",
               version_label="v1", body="x" * 100)],
        today=_TODAY,
    )
    hits = [w for w in warns
            if "misfiled on Deliverables" in w or "empty deliverable" in w]
    assert len(hits) == 1
    assert "misfiled" in hits[0]


# --- #176: archive integrity -----------------------------------------------

from cp_engine.spine_lint import lint_archived_referrers, lint_partial_archive


def _arow(eid, framing, **kw):
    row = {"est_item_id": eid, "framing": framing, "body": "", "sources": [],
           "archived": False, "layer": "Deliverables"}
    row.update(kw)
    return row


def test_archived_element_named_by_TITLE_in_prose_is_flagged() -> None:
    """The motivating case writes the reference as a quoted TITLE, not an id:
    '[HISTORICAL — superseded by `SRS Arc B — v08→r01 build delta`]'. An
    id-only check would miss the very card the issue was filed about."""
    archived = [_arow("_authored/srs-arc-b-delta",
                      "SRS Arc B — v08→r01 build delta", archived=True)]
    live = [_arow("_authored/srs-response", "SRS Response",
                  body="> **[HISTORICAL — superseded by "
                       "`SRS Arc B — v08→r01 build delta`]**")]
    warns = lint_archived_referrers(live, archived, [])
    assert any("archived but still referenced" in w for w in warns)
    assert any("SRS Response" in w for w in warns)


def test_archived_element_referenced_by_id_is_flagged() -> None:
    archived = [_arow("_authored/gone", "A gone card", archived=True)]
    live = [_arow("_authored/here", "A live card",
                  body="see _authored/gone for the detail")]
    assert lint_archived_referrers(live, archived, [])


def test_archived_element_referenced_by_active_edge_is_flagged() -> None:
    archived = [_arow("_authored/gone", "A gone card", archived=True)]
    live = [_arow("_authored/deck", "The deck")]
    warns = lint_archived_referrers(
        live, archived,
        [{"kind": "informs", "from_item_id": "_authored/gone",
          "to_item_id": "_authored/deck"}],
    )
    assert any("(edge)" in w for w in warns)


def test_unreferenced_archived_element_is_silent() -> None:
    archived = [_arow("_authored/gone", "A gone card", archived=True)]
    live = [_arow("_authored/here", "A live card", body="unrelated prose")]
    assert lint_archived_referrers(live, archived, []) == []


def test_short_titles_do_not_match_ordinary_prose() -> None:
    """A framing like 'Notes' or a person's name collides with normal text;
    matching it would fire on every card that used the word."""
    archived = [_arow("_authored/n", "Notes", archived=True)]
    live = [_arow("_authored/here", "A live card",
                  body="Notes from the call are below.")]
    assert lint_archived_referrers(live, archived, []) == []


def test_half_archived_element_is_not_reported_as_dangling() -> None:
    """If some versions are still live the element remains readable — that is
    the partial-archive defect, not a dangling pointer. Reporting both would
    double-count one problem."""
    archived = [_arow("_authored/half", "A half-archived card", archived=True)]
    live = [_arow("_authored/half", "A half-archived card"),
            _arow("_authored/ref", "Referrer",
                  body="points at _authored/half")]
    assert lint_archived_referrers(live, archived, []) == []


def test_partial_archive_is_flagged() -> None:
    rows = [
        _arow("_authored/x", "Held deck", archived=True),
        _arow("_authored/x", "Held deck", archived=False),
    ]
    warns = lint_partial_archive(rows)
    assert any("partially archived" in w for w in warns)
    assert any("1 archived version(s) and 1 live" in w for w in warns)


def test_fully_archived_and_fully_live_elements_are_silent() -> None:
    all_archived = [_arow("_authored/a", "A", archived=True),
                    _arow("_authored/a", "A", archived=True)]
    all_live = [_arow("_authored/b", "B"), _arow("_authored/b", "B")]
    assert lint_partial_archive(all_archived) == []
    assert lint_partial_archive(all_live) == []


def test_no_archived_rows_short_circuits() -> None:
    assert lint_archived_referrers([_arow("_authored/x", "X")], [], []) == []


def test_a_card_that_already_holds_the_source_is_not_a_dangling_referrer() -> None:
    """False positive found in use on ibx-5192 (2026-08-12).

    Kimber's card says "Resources attached as sources: … SRS Block Details ·
    Platform Media Coverage" — naming its OWN attachments — and both archived
    stubs' rag_assets already sit in its `sources`. The provenance transferred;
    nothing dangles. Flagging it told the reader to un-archive a stub whose
    only content was a pointer the live card already has."""
    asset = {"id": "cde901de", "type": "rag_asset", "title": "SRS Block Details.docx"}
    archived = [_arow("_authored/srs-block-details-docx", "SRS Block Details.docx",
                      archived=True, sources=[asset])]
    live = [_arow("_authored/kimber", "Kimber's feedback on Mainstage outline",
                  body="Resources attached as sources: … SRS Block Details.docx",
                  sources=[asset])]
    assert lint_archived_referrers(live, archived, []) == []


def test_a_referrer_WITHOUT_the_source_still_dangles() -> None:
    """The narrowing must not silence the real case: a card naming an archived
    element it does NOT hold the provenance for is still a dead pointer."""
    archived = [_arow("_authored/gone", "SRS Arc B — v08→r01 build delta",
                      archived=True, sources=[{"id": "x1", "type": "rag_asset"}])]
    live = [_arow("_authored/ref", "A live card",
                  body="superseded by `SRS Arc B — v08→r01 build delta`",
                  sources=[])]
    assert lint_archived_referrers(live, archived, [])


def test_a_sourceless_archived_element_is_unaffected_by_the_narrowing() -> None:
    """Most archived cards carry no sources at all; the transfer check must
    not accidentally exempt them (an empty set is a subset of everything)."""
    archived = [_arow("_authored/gone", "A gone card", archived=True, sources=[])]
    live = [_arow("_authored/ref", "Referrer", body="see _authored/gone")]
    assert lint_archived_referrers(live, archived, [])


# ── #319: every remedy must name a move that clears its finding ──────────
#
# A warning whose advice doesn't work is worse than no warning: it costs a
# round trip, then teaches the reader to skip the lint. Each test below applies
# the remedy the message names and asserts the finding is gone — and, where the
# old text named a move that did NOT clear it, that the move is no longer
# offered.


def test_important_but_floating_remedy_clears_it_and_versioning_is_not_offered():
    """08-25: two elements were "versioned with their answers" as the old text
    offered, and the warning persisted — the check reads binding/serves only."""
    row = _row("_authored/q", framing="Guiding question",
               important=True, binding="unbound", serves=())
    (msg,) = lint_spine_rows([row])
    assert "version it with its answer" not in msg
    # Bind it to the work it serves (set_spine_element sets binding=live too).
    assert "serves" in msg
    assert lint_spine_rows([dict(row, serves=["wi-1"], binding="live")]) == []
    # …or clear the important flag once it has been answered.
    assert "important flag" in msg
    assert lint_spine_rows([dict(row, important=False)]) == []
    # A new version changes neither field — and the message says so.
    assert lint_spine_rows([dict(row, version_label="v2")])
    assert "new version" in msg and "does not clear" in msg


def test_dead_end_says_what_the_edge_must_point_at_and_the_edge_clears_it():
    """08-21 slt-5196: an `informs` edge to a DECISION was wired, stayed live,
    and the warning (correctly — #163 wants the stream reachable from the
    deliverable) kept firing. The message must say a decision doesn't count."""
    from cp_engine.spine_lint import lint_lifecycle

    rows = [_lrow("shoot", layer="Activity", framing="Location scout"),
            _lrow("_authored/decision", layer="Decisions"),
            _lrow("film", layer="Deliverables")]
    to_decision = [_edge("informs", "shoot", "_authored/decision")]
    (msg,) = lint_lifecycle(rows, to_decision)
    assert "Deliverables layer" in msg
    assert "decision" in msg and "does not count" in msg
    # The remedy as written — an edge FROM the activity TO a deliverable.
    assert lint_lifecycle(rows, to_decision + [_edge("informs", "shoot", "film")]) == []


def test_no_deliverables_is_one_direct_finding_not_n_dead_ends():
    """08-25: four projects had NO Deliverables element and each surfaced as N
    dead-end activities sharing one cause — and the per-activity remedy (edge
    to the deliverable) was impossible, since there was none to point at."""
    from cp_engine.spine_lint import lint_lifecycle

    acts = [_lrow(f"a{i}", layer="Activity", framing=f"Activity {i}") for i in range(4)]
    out = lint_lifecycle(acts, [])
    assert len(out) == 1, out
    assert "no deliverable element" in out[0]
    assert "4 active activities" in out[0]
    assert "dead-end activity" not in out[0]
    # Applying it: add the deliverable card, wire each activity to it → clean.
    fixed = acts + [_lrow("video", layer="Deliverables")]
    edges = [_edge("informs", f"a{i}", "video") for i in range(4)]
    assert lint_lifecycle(fixed, edges) == []
    # Half-applied (card added, not yet wired) → the ordinary per-activity
    # findings, which are now satisfiable.
    half = lint_lifecycle(fixed, [])
    assert len(half) == 4 and all("dead-end activity" in w for w in half)


def test_undistilled_capture_leaves_iterated_sourced_cards_alone():
    """08-25 slt-5196 `what-winning-means-by-role`: 10.8k chars at v7 with four
    sources attached — authored analysis. "Distill it" would destroy work."""
    authored = _crow(framing="What winning means by role", layer="Synthesis",
                     body="z" * 10_800, version_label="v7",
                     sources=[{"type": "rag_asset", "id": f"s{i}"} for i in range(4)])
    assert not any("undistilled" in w for w in lint_curation([authored], today=_TODAY))
    # A v1 with sources is still flagged — a paste of what it attached.
    pasted = dict(authored, version_label="v1")
    assert any("undistilled" in w for w in lint_curation([pasted], today=_TODAY))


def test_undistilled_capture_without_a_source_names_a_possible_remedy():
    """08-25 ibx-5192 / sap-5174: the raw email was never ingested, so "attach
    the raw text as a source" was impossible and the card was the only copy."""
    row = _crow(framing="Feedback from Janet + Mehul r3", layer="ClientFeedback",
                body="y" * 17_000, version_label="v1", sources=[])
    (msg,) = [w for w in lint_curation([row], today=_TODAY) if "undistilled" in w]
    assert "only copy" in msg
    assert "push_to_dropbox" in msg and "add_element_source" in msg
    # Applying it: the raw text lands as a source, the card is distilled.
    done = dict(row, body="d" * 2_000, version_label="v2",
                sources=[{"type": "rag_asset", "id": "raw"}])
    assert not any("undistilled" in w for w in lint_curation([done], today=_TODAY))


def test_unauthored_brief_is_skipped_on_an_initiative():
    """08-25 mission-control: an internal workstream has no agreement, so a
    scaffolded SOW there is a warning nothing can satisfy."""
    sow = _crow(framing="Statement of Work", layer="Agreement", body="- _<fill>_")
    assert not any("unauthored standing Brief" in w
                   for w in lint_curation([sow], today=_TODAY, label="initiative"))
    # A job — and an unresolved label — keep the check.
    for label in ("job", None):
        assert any("unauthored standing Brief" in w
                   for w in lint_curation([sow], today=_TODAY, label=label))


def test_unauthored_brief_runs_on_jobs_only():
    """Since v0.124.3 an account never carries an agreement, and a program is
    a grouping whose jobs hold the SOWs — the same unsatisfiable warning the
    initiative skip removed. The labels come from `state.WorkstreamLabel`, so
    a new label is covered (and skipped) without editing this list."""
    from typing import get_args

    from cp_engine.state import WorkstreamLabel

    labels = set(get_args(WorkstreamLabel))
    assert {"account", "program", "job", "initiative"} <= labels
    sow = _crow(framing="Statement of Work", layer="Agreement", body="- _<fill>_")
    for label in labels - {"job"}:
        assert not any("unauthored standing Brief" in w
                       for w in lint_curation([sow], today=_TODAY, label=label)), label
    assert any("unauthored standing Brief" in w
               for w in lint_curation([sow], today=_TODAY, label="job"))


class _Q:
    def __init__(self, table, filters, db):
        self._t, self._f, self._db = table, dict(filters), db

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        return _Q(self._t, {**self._f, col: val}, self._db)

    def in_(self, col, vals):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        rows = [r for r in self._db.get(self._t, [])
                if all(r.get(k) == v for k, v in self._f.items()
                       if k in ("id", "parent_id"))]
        return type("R", (), {"data": rows})()


class _Client:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        return _Q(name, {}, self._db)


def _projects_db(**project):
    base = {"id": "p1", "parent_id": None, "deal_stage": None,
            "companies": {"kind": "internal"}}
    base.update(project)
    return {"projects": [base]}


def test_workstream_label_comes_from_the_engines_derive_label():
    """Derived with `state.derive_label` over the row's shape — not a second
    spelling of "initiative"."""
    from cp_engine.spine_lint import _workstream_label

    rows = [{"project_id": "p1"}]
    assert _workstream_label(_Client(_projects_db()), rows) == "initiative"
    assert _workstream_label(
        _Client(_projects_db(deal_stage="Won", parent_id="acct",
                             companies={"kind": "client"})), rows) == "job"
    # Unresolvable → None, which keeps every check on.
    assert _workstream_label(_Client({"projects": []}), rows) is None
    assert _workstream_label(_Client(_projects_db()), []) is None


def test_run_all_lints_skips_the_brief_check_for_an_initiative_end_to_end():
    """The CLI and the hosted verb share `run_all_lints`; the skip must happen
    there, or one surface keeps the unsatisfiable warning."""
    from cp_engine.spine_lint import run_all_lints

    sow = {"est_item_id": "_authored/sow", "framing": "Statement of Work",
           "layer": "Agreement", "binding": "unbound", "serves": [],
           "important": False, "body": "- _<fill>_", "sources": [],
           "status": "live", "archived": False, "project_id": "p1",
           "version_label": "v1", "version_date": "2026-08-01"}

    def run(db):
        db = dict(db, spine_substance=[sow], spine_relations=[])
        return run_all_lints(_Client(db), ["x"])

    assert not any("unauthored standing Brief" in w for w in run(_projects_db()))
    assert any("unauthored standing Brief" in w for w in run(_projects_db(
        deal_stage="Won", parent_id="acct", companies={"kind": "client"})))
    # An account (parentless, client, no agreement) is skipped end to end too.
    assert not any("unauthored standing Brief" in w for w in run(_projects_db(
        companies={"kind": "client"})))
