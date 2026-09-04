"""Tests for cp_engine.preflight — the readiness check (RFP build spec §3, §7).

The anchor case is the 2026-09-04 session: a complete production RFP was
drafted against sap-5200, a competitive-messaging project with no video
in it, whose cp.md was an empty scaffold. Two independent gates must
each catch that on their own, because in the real failure only one of
them was available.
"""

from __future__ import annotations

import pytest

from cp_engine.preflight import (
    ARTIFACT_KINDS,
    is_unauthored_scaffold,
    render_report,
    run_preflight,
)

# The literal 5200 scaffold, as sync writes it.
_SCAFFOLD = """<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-09-03

**Last session:** _<date>_
**Objective:** _<one line — what this project delivers>_
**Status:** _<current state in a phrase>_

**Where it stands:**
- _<2-4 dense bullets of current reality>_

**Next up:**
- _<concrete near-term moves, dated where possible>_

**Blockers:**
- _<what's stuck / needed, with who — or "None">_

**Updates:**
- _<dated — first wrap up authors this>_
<!-- cp-engine:end exec-summary -->"""

# 5200 as it would look once someone HAD written it up: real content,
# still the wrong shape for a production RFP.
_STRATEGY_AUTHORED = """<!-- cp-engine:start exec-summary -->
**Objective:** Competitive messaging framework and positioning architecture.
**Status:** Strategy phase — building the messaging framework.

**Where it stands:**
- Competitive audit underway; positioning architecture drafted.
- Research readout scheduled with the client team.

**Blockers:**
- None.
<!-- cp-engine:end exec-summary -->"""

# A production project with real scope.
_PRODUCTION_AUTHORED = """<!-- cp-engine:start exec-summary -->
**Objective:** Deliver the 2027 brand video set — one :30, two :15s and
two :06s with 9x16 and square cuts — for a January 2027 in-market launch.
**Status:** Briefed and won; contracting is the live work.

**Where it stands:**
- Deliverables and audience are known: SMB/mid-market finance decision-makers.
- Shoot Nov 2026, delivery end of Jan 2027.

**Blockers:**
- Full creative brief still owed by the client.
<!-- cp-engine:end exec-summary -->"""


# ──────────────────────────────────────────────────────────────────────
#  Gate 1 — the unauthored scaffold
# ──────────────────────────────────────────────────────────────────────


def test_scaffold_is_detected_as_unauthored() -> None:
    """The 5200 signature: a project that exists but was never written up."""
    from cp_engine.render import slice_exec_summary_region

    assert is_unauthored_scaffold(slice_exec_summary_region(_SCAFFOLD))


def test_authored_summary_is_not_a_scaffold() -> None:
    from cp_engine.render import slice_exec_summary_region

    assert not is_unauthored_scaffold(slice_exec_summary_region(_PRODUCTION_AUTHORED))


def test_missing_and_empty_regions_count_as_unauthored() -> None:
    assert is_unauthored_scaffold(None)
    assert is_unauthored_scaffold("")
    assert is_unauthored_scaffold("   \n  ")


def test_scaffold_blocks_drafting_outright() -> None:
    rep = run_preflight("sap-5200-competitive-personality", "rfp",
                        cp_md_text=_SCAFFOLD)
    assert rep.ready is False
    assert rep.confidence == "none"
    assert any("scaffold" in n.lower() for n in rep.notes)


# ──────────────────────────────────────────────────────────────────────
#  Gate 2 — shape
# ──────────────────────────────────────────────────────────────────────


def test_strategy_project_gets_a_shape_warning_for_a_production_rfp() -> None:
    """The one string that would have ended the 5200 session in seconds."""
    rep = run_preflight("sap-5200-competitive-personality", "rfp",
                        cp_md_text=_STRATEGY_AUTHORED)
    assert rep.shape_warning is not None
    assert "not applicable" in rep.shape_warning
    assert rep.ready is False


def test_production_project_gets_no_shape_warning() -> None:
    rep = run_preflight("sap-5198-2027-ad-videos", "rfp",
                        cp_md_text=_PRODUCTION_AUTHORED)
    assert rep.shape_warning is None


def test_thin_project_is_not_shape_warned() -> None:
    """Absence of signal is not evidence of the wrong shape.

    A thin project must land in `missing`, not `shape_warning` — calling
    it a shape error sends the reader to fix the wrong thing.
    """
    rep = run_preflight("x", "rfp", cp_md_text=_SCAFFOLD)
    assert rep.shape_warning is None
    assert "deliverables" in rep.missing


def test_sow_has_no_shape_opinion() -> None:
    """A SOW fits any engagement shape, including strategy work."""
    rep = run_preflight("sap-5200-competitive-personality", "sow",
                        cp_md_text=_STRATEGY_AUTHORED)
    assert rep.shape_warning is None


# ──────────────────────────────────────────────────────────────────────
#  Reading broadly — the sprint-file lesson
# ──────────────────────────────────────────────────────────────────────


def test_scope_is_found_in_sprint_bullets_when_cp_md_is_thin() -> None:
    """The 5198 lesson: the deliverables card was empty, the sprint wasn't.

    A reader that only trusts the structured field sees an empty project
    and asks the human for something CP already knows.
    """
    thin = """<!-- cp-engine:start exec-summary -->
**Objective:** 2027 ad videos.
**Status:** Won.
<!-- cp-engine:end exec-summary -->"""
    sprint = """# sap-5198 — Sprint W36
## Client communication
### Inbound
- Client deck confirms one :30, two :15s and two :06s plus 9x16 cuts.
- Audience is SMB/mid-market finance decision-makers.
- Delivery targeted for Jan 2027; shoot in Nov 2026.
"""
    rep = run_preflight("sap-5198-2027-ad-videos", "rfp", cp_md_text=thin,
                        sprint_texts=[("sprint 2026-W36", sprint)])
    assert "deliverables" in rep.found
    assert "audience" in rep.found
    assert "schedule" in rep.found
    assert "sprint 2026-W36" in rep.sources_read


def test_template_placeholders_in_sprint_bullets_are_ignored() -> None:
    sprint = """## Client communication
### Inbound
- _<message — `[status · date]` prefix>_
"""
    rep = run_preflight("x", "rfp", cp_md_text=_SCAFFOLD,
                        sprint_texts=[("sprint W36", sprint)])
    assert rep.found == {}


# ──────────────────────────────────────────────────────────────────────
#  The budget conflation — the error the spec cares most about
# ──────────────────────────────────────────────────────────────────────


def test_partner_budget_is_always_missing_and_never_inferred() -> None:
    """The $425k engagement fee is NOT the partner budget.

    Even with a fee clearly present, partner_budget stays missing — the
    tool must never derive one from the other.
    """
    cp = _PRODUCTION_AUTHORED.replace(
        "**Status:** Briefed and won; contracting is the live work.",
        "**Status:** Won at $425,000 fixed fee, travel on top.",
    )
    rep = run_preflight("sap-5198-2027-ad-videos", "rfp", cp_md_text=cp)
    assert "engagement_fee" in rep.found
    assert "partner_budget" in rep.missing
    assert any("NOT the" in n for n in rep.notes)


def test_engagement_fee_is_not_named_budget() -> None:
    """A field called `budget` would invite exactly the conflation."""
    cp = _PRODUCTION_AUTHORED.replace(
        "**Status:** Briefed and won; contracting is the live work.",
        "**Status:** Won at $425,000 fixed fee.",
    )
    rep = run_preflight("x", "rfp", cp_md_text=cp)
    assert "budget" not in rep.found


# ──────────────────────────────────────────────────────────────────────
#  Conflicts
# ──────────────────────────────────────────────────────────────────────


def test_disagreeing_delivery_years_surface_as_a_conflict() -> None:
    """Dec 15 2026 vs post-into-2027 — real, unresolved, load-bearing."""
    cp = """<!-- cp-engine:start exec-summary -->
**Objective:** 2027 brand videos — one :30 and two :15s for enterprise buyers.
**Status:** Won. Our position is shoot 2026, deliver Jan 15, 2027.
<!-- cp-engine:end exec-summary -->"""
    sprint = """## Client communication
### Inbound
- Client deck says final delivery Dec 15, 2026 for the campaign videos.
"""
    rep = run_preflight("sap-5198-2027-ad-videos", "rfp", cp_md_text=cp,
                        sprint_texts=[("sprint W36", sprint)])
    assert rep.conflicts
    assert "disagree" in rep.conflicts[0]


def test_conflict_downgrades_confidence_but_does_not_block() -> None:
    """A conflict is a decision someone owes, not a reason to refuse."""
    cp = """<!-- cp-engine:start exec-summary -->
**Objective:** Brand videos — one :30, two :15s, 9x16 cuts, for SMB buyers.
**Status:** Won; deliver Jan 15, 2027 after a Nov 2026 shoot.
<!-- cp-engine:end exec-summary -->"""
    sprint = """## Client communication
### Inbound
- Client deck says final delivery Dec 15, 2026.
- Audience confirmed as mid-market finance decision-makers.
"""
    rep = run_preflight("x", "rfp", cp_md_text=cp,
                        sprint_texts=[("sprint W36", sprint)])
    assert rep.conflicts
    assert rep.confidence == "partial"


# ──────────────────────────────────────────────────────────────────────
#  Contract
# ──────────────────────────────────────────────────────────────────────


def test_unknown_artifact_kind_raises_rather_than_passing() -> None:
    """A typo must not silently degrade to 'everything is fine'."""
    with pytest.raises(ValueError, match="unknown artifact_kind"):
        run_preflight("x", "rpf", cp_md_text=_PRODUCTION_AUTHORED)


def test_artifact_kinds_are_exposed_for_the_cli_choice_list() -> None:
    assert "rfp" in ARTIFACT_KINDS
    assert set(ARTIFACT_KINDS) >= {"rfp", "sow", "brief", "estimate"}


def test_missing_cp_md_degrades_rather_than_raising() -> None:
    rep = run_preflight("ghost-project", "rfp", cp_md_text=None)
    assert rep.ready is False
    assert any("No cp.md" in n for n in rep.notes)


def test_report_is_json_serialisable() -> None:
    import json

    rep = run_preflight("x", "rfp", cp_md_text=_PRODUCTION_AUTHORED)
    assert json.loads(json.dumps(rep.to_dict()))["artifact_kind"] == "rfp"


def test_ready_and_confidence_never_disagree() -> None:
    """ready=True with confidence='none' would be incoherent."""
    for cp in (_SCAFFOLD, _STRATEGY_AUTHORED, _PRODUCTION_AUTHORED):
        rep = run_preflight("x", "rfp", cp_md_text=cp)
        if rep.ready:
            assert rep.confidence in ("partial", "good")
        else:
            assert rep.confidence in ("none", "partial")


def test_render_report_shows_the_verdict_and_the_shape_warning() -> None:
    rep = run_preflight("sap-5200-competitive-personality", "rfp",
                        cp_md_text=_STRATEGY_AUTHORED)
    out = render_report(rep)
    assert "NOT READY" in out
    assert "Wrong shape" in out


# ──────────────────────────────────────────────────────────────────────
#  Extraction quality — all four regressions from the first live 5198 run
# ──────────────────────────────────────────────────────────────────────


def test_a_line_lands_in_one_field_not_every_field_it_brushes() -> None:
    """The headline defect: one paragraph filed under three headings.

    A sentence that mentions scope, audience AND usage was previously
    echoed verbatim under all three, so a reader scanning `audience` got
    a sentence about scope.
    """
    from cp_engine.preflight import assign_fields

    line = (
        "Scope is defined by the client's deck. Deliverables, audience "
        "(SMB/mid-market finance decision-makers) and one-year global "
        "usage are known; the creative brief is not."
    )
    got = assign_fields([line])
    landed = [f for f, rows in got.items() if any(line[:40] in r for r in rows)]
    assert len(landed) == 1, f"line landed in {landed}"


def test_action_items_are_not_deliverables() -> None:
    """'Email Drew the deck' matched \\bdeck\\b and was filed as a deliverable."""
    from cp_engine.preflight import assign_fields

    got = assign_fields([
        "Email Drew Fiero (FirstPerson) the deck",
        "Send the revised playbook to Rina",
        "Confirm the video cut with the client",
    ])
    assert got.get("deliverables", []) == []


def test_bracketed_tracking_rows_are_not_facts() -> None:
    """A stakeholder card is a record about a person, not project scope."""
    from cp_engine.preflight import assign_fields

    got = assign_fields([
        "[Tara Haney · SAP Concur brand lead · Delivered the rough "
        "schedule and research walkthrough on 2026-09-01.]"
    ])
    assert got.get("schedule", []) == []


def test_internal_deliberation_is_not_scope() -> None:
    """'Drew and Marcello agreed to pitch X' is a decision, not a deliverable."""
    from cp_engine.preflight import assign_fields

    got = assign_fields([
        "Drew and Marcello agreed not to bring Stefan back as creative "
        "partner on 5198 — want a director versed in AI-enhanced production.",
        "Marcello flagged that the November shoot is unrealistic.",
    ])
    assert got.get("deliverables", []) == []
    assert got.get("schedule", []) == []


def test_concrete_specifics_outrank_topic_mentions() -> None:
    """A line naming :30 and 9x16 is a better answer than one saying 'video'."""
    from cp_engine.preflight import assign_fields

    vague = "The video campaign is the main workstream this quarter."
    precise = "Deliverables: one :30, two :15s, 16x9 primary plus 9x16 cuts."
    rows = assign_fields([vague, precise])["deliverables"]
    assert precise[:30] in rows[0], f"precise line should rank first, got {rows[0]!r}"


def test_assignment_is_stable_regardless_of_input_order() -> None:
    from cp_engine.preflight import assign_fields

    lines = [
        "Deliverables: one :30 and two :15s with 9x16 cuts.",
        "Audience is SMB and mid-market, 100-1,000 employees, CFO primary.",
        "Fee set at $425,000 fixed, not to exceed.",
    ]
    a = assign_fields(lines)
    b = assign_fields(list(reversed(lines)))
    assert {k: sorted(v) for k, v in a.items()} == {k: sorted(v) for k, v in b.items()}


def test_the_fee_line_ranks_above_incidental_dollar_amounts() -> None:
    """A contractor's billed hours is not the engagement fee."""
    from cp_engine.preflight import assign_fields

    incidental = (
        "Rob (contract writer) billed $8,043 across two payroll periods "
        "for concepting and produced only one usable output."
    )
    real = "Fee set at $425,000 fixed, not to exceed, travel billed separately."
    rows = assign_fields([incidental, real])["engagement_fee"]
    assert "425,000" in rows[0]


# ──────────────────────────────────────────────────────────────────────
#  Second live pass — the slt-5196 defects
# ──────────────────────────────────────────────────────────────────────


def test_citation_years_are_not_a_delivery_conflict() -> None:
    """A source's vintage is evidence metadata, not a schedule claim.

    slt-5196 was told its years disagreed because a 3M case study was
    marked "(2021, pre-rebrand)". A false conflict is worse than a
    missed one: after one, nobody reads the section again.
    """
    cp = """<!-- cp-engine:start exec-summary -->
**Objective:** VOC brand campaign — one 60s compilation plus 30s and 15s cuts.
**Status:** Shoot Oct 15, 2026; public launch Nov 4, 2026.
<!-- cp-engine:end exec-summary -->"""
    sprint = """## Dependencies & risks
- 3M's "60% reduction in time to close" is from a 2021 case study, pre-rebrand.
- MOTOR's figure comes from an unpublished client draft, flagged on the source.
"""
    rep = run_preflight("slt-5196-brand-campaign-26", "rfp", cp_md_text=cp,
                        sprint_texts=[("sprint W35", sprint)])
    assert rep.conflicts == [], f"false conflict: {rep.conflicts}"


def test_a_real_delivery_disagreement_still_fires() -> None:
    """The 5198 case must survive the citation filter."""
    cp = """<!-- cp-engine:start exec-summary -->
**Objective:** Brand videos — one :30 and two :15s for finance buyers.
**Status:** Our position is shoot 2026, deliver Jan 15, 2027.
<!-- cp-engine:end exec-summary -->"""
    sprint = """## Client communication
### Inbound
- Client deck says final delivery Dec 15, 2026.
"""
    rep = run_preflight("sap-5198-2027-ad-videos", "rfp", cp_md_text=cp,
                        sprint_texts=[("sprint W36", sprint)])
    assert rep.conflicts, "the real Dec-2026-vs-2027 conflict must still fire"


def test_years_outside_a_scheduling_context_are_ignored() -> None:
    from cp_engine.preflight import _date_conflicts

    assert _date_conflicts([
        ("src", "The 2021 Gartner statistic and the 2024 benchmark study."),
    ]) == []


def test_logistics_do_not_lead_the_deliverables_field() -> None:
    """slt-5196 filed call times and release paperwork as deliverables.

    The actual deliverable — durations and counts — sat fifth under a
    heading full of per diem and LED-wall logistics.
    """
    from cp_engine.preflight import assign_fields

    logistics = (
        "Production day plan + equipment list to Leah — the ~10-hour day "
        "(7am-7pm incl. setup), LED wall + ceiling with matched lighting."
    )
    real = (
        "Hold the deliverable structure as agreed in the SOW: one 60-second "
        "compilation, plus 30s and 15s per customer."
    )
    rows = assign_fields([logistics, real]).get("deliverables", [])
    assert rows, "the real deliverable must be present"
    assert "60-second" in rows[0], f"logistics led the field: {rows[0]!r}"


def test_pure_logistics_with_no_spec_is_dropped_from_deliverables() -> None:
    from cp_engine.preflight import assign_fields

    got = assign_fields([
        "Even with company names withheld, some employers may still require "
        "approval for an employee to appear on camera; Leah is working "
        "identification + releases with legal.",
    ])
    assert got.get("deliverables", []) == []
