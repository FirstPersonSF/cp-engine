"""Tests for the sprint-planning pipeline fixes (post-Phase-4 follow-up).

Four fixes under test:
  1. Bare ``cp prep-planning`` refuses (exit 2) — the deprecated
     engine-rendered inventory needs an explicit ``--legacy-render``.
  2. The cross-cutting decisions window is enforced with visible
     accounting (stale dropped + counted; undated kept + counted) and the
     parser tolerates the real-world entry shapes (auto-ingest hash
     markers, date-only meta, no meta at all).
  3. Planning-week allocations flow into ``tenant_hours_planned`` (and an
     explicit empty-week note).
  4. Capacity binding uses the planned-allocations basis when the planning
     week has rows, falling back to the labeled owner-of-record count.
"""

from __future__ import annotations

from datetime import date

from click.testing import CliRunner

from cp_engine.agenda import parse_weekly_decisions
from cp_engine.cli import main
from cp_engine.prep_planning import (
    PlanningResult,
    _detect_capacity_binding_planned,
    _load_cross_cutting_decisions,
    _render_cross_cutting,
    build_planning_result,
)
from cp_engine.state import (
    PersonHours,
    PersonRollup,
    ProjectAllocation,
    WeeklyAllocations,
)
from tests.test_prep_planning_cross_cutting import (
    _DECISIONS_HEADER,
    _write_weekly_cp,
    make_config,
    make_state,
)

# ──────────────────────────────────────────────────────────────────────
#  Fix 1 — bare invocation refuses; --legacy-render is the escape hatch
# ──────────────────────────────────────────────────────────────────────


def test_bare_prep_planning_exits_nonzero_with_pointer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no tenant needed — refusal comes first
    result = CliRunner().invoke(main, ["prep-planning"])
    assert result.exit_code == 2
    assert "--bundle" in result.output
    assert "--legacy-render" in result.output


def test_out_without_render_flag_also_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main, ["prep-planning", "--out", str(tmp_path / "_planning.md")]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "_planning.md").exists()


def test_legacy_render_header_stamps_provenance():
    r = PlanningResult(
        week_iso="2026-W28",
        week_dates="Jul 6 – Jul 12",
        project_count=0,
        estimated_minutes=60,
        tenant_hours_last_week={},
        blocks_by_account={},
        milestone_counts={"total": 0, "fetched": 0, "errored": 0},
        urgent_counts={},
        generated_at="2026-07-03 21:00",
    )
    from cp_engine.prep_planning import render_planning_doc_markdown

    doc = render_planning_doc_markdown(r)
    assert "by `cxp prep-planning --legacy-render`" in doc


# ──────────────────────────────────────────────────────────────────────
#  Fix 2 — parser tolerance + window accounting
# ──────────────────────────────────────────────────────────────────────


def test_parser_tolerates_hash_marker_and_date_only_and_undated():
    body = (
        _DECISIONS_HEADER
        + "3. **Auto-ingested** — from an account meeting. "
        "(2026-06-20, source: account: google) <!-- cp:hash=acadcc82 -->\n\n"
        "2. **Date only** — no source field. (2026-06-21)\n\n"
        "1. **Undated** — someone forgot the meta. "
        "<!-- cp:hash=9fc5ef1a -->\n"
    )
    ds = parse_weekly_decisions(body)
    assert len(ds) == 3
    by_num = {d.number: d for d in ds}
    assert by_num[3].date == "2026-06-20"
    assert by_num[3].sources == ("account: google",)
    assert "<!--" not in by_num[3].text
    assert by_num[2].date == "2026-06-21"
    assert by_num[2].sources == ()
    assert by_num[1].date == ""
    assert "<!--" not in by_num[1].text


def test_parser_scopes_to_decisions_section():
    """Numbered lists outside the decisions section must not parse."""
    body = (
        "## Quick Resume\n\n"
        "1. **Not a decision** — just a resume bullet.\n\n"
        + _DECISIONS_HEADER
        + "1. **Real decision** — in section. (2026-06-20, source: x)\n\n"
        "## Account summaries\n\n"
        "2. **Also not a decision** — summary bullet.\n"
    )
    ds = parse_weekly_decisions(body)
    assert len(ds) == 1
    assert "Real decision" in ds[0].text


def test_window_accounting_stale_and_undated(tmp_path):
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "3. **Fresh** — in window. (2026-06-20, source: x)\n\n"
        "2. **Stale** — long gone. (2026-04-01, source: x)\n\n"
        "1. **Undated** — kept and counted.\n",
    )
    kept, errors, stale, undated = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 25)
    )
    texts = [d.text for d in kept]
    assert any("Fresh" in t for t in texts)
    assert any("Undated" in t for t in texts)
    assert all("Stale" not in t for t in texts)
    assert stale == 1
    assert undated == 1
    assert errors == []


def test_summary_exposes_stale_and_undated_counts(tmp_path):
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "2. **Fresh** — in window. (2026-06-20, source: x)\n\n"
        "1. **Stale** — old. (2026-01-01, source: x)\n",
    )
    result = build_planning_result(
        make_config(tmp_path),
        (make_state("ggl-1"),),
        today=date(2026, 6, 25),
        supabase_client=None,
    )
    s = result.to_summary_dict()
    assert s["cross_cutting_decisions_count"] == 1
    assert s["cross_cutting_decisions_stale_count"] == 1
    assert s["cross_cutting_decisions_undated_count"] == 0


# ──────────────────────────────────────────────────────────────────────
#  Fix 3 — planning-week allocations (forward capacity)
# ──────────────────────────────────────────────────────────────────────


def _allocations(rollup, by_project=None) -> WeeklyAllocations:
    return WeeklyAllocations(
        week_start="2026-07-06", by_project=by_project or {}, rollup=rollup
    )


def _rollup(name: str, hours: float, projects: int = 1) -> PersonRollup:
    return PersonRollup(
        person_name=name,
        engagement_hours=hours,
        engagement_project_count=projects,
        internal_hours=0.0,
    )


def test_planned_allocations_flow_into_summary(tmp_path):
    planned = _allocations((_rollup("Tony Welch", 32.0),))
    result = build_planning_result(
        make_config(tmp_path),
        (make_state("ggl-1"),),
        today=date(2026, 6, 25),
        supabase_client=None,
        planned_allocations=planned,
    )
    assert result.tenant_hours_planned == {"Tony": 32}
    assert result.to_summary_dict()["tenant_hours_planned"] == {"Tony": 32}


def test_empty_planning_week_renders_explicit_note(tmp_path):
    result = build_planning_result(
        make_config(tmp_path),
        (make_state("ggl-1"),),
        today=date(2026, 6, 25),
        supabase_client=None,
        planned_allocations=None,
    )
    body = "\n".join(_render_cross_cutting(result))
    assert "no allocations entered for the planning week yet" in body


def test_planned_hours_render_in_tenant_strip(tmp_path):
    planned = _allocations((_rollup("Drew Fiero", 40.0), _rollup("Tony W", 20.0)))
    result = build_planning_result(
        make_config(tmp_path),
        (make_state("ggl-1"),),
        today=date(2026, 6, 25),
        supabase_client=None,
        planned_allocations=planned,
    )
    body = "\n".join(_render_cross_cutting(result))
    assert "**Planned (this sprint):** Drew 40h, Tony 20h" in body


# ──────────────────────────────────────────────────────────────────────
#  Fix 4 — capacity binding: planned basis + labeled fallback
# ──────────────────────────────────────────────────────────────────────


def _by_project(entries_by_code: dict[str, list[str]]) -> dict:
    return {
        code: ProjectAllocation(
            project_code=code,
            is_internal=False,
            entries=tuple(
                PersonHours(person_name=n, hours=8.0) for n in names
            ),
        )
        for code, names in entries_by_code.items()
    }


def test_binding_planned_basis_by_hours():
    planned = _allocations(
        (_rollup("Tony Welch", 46.0), _rollup("Derek", 8.0)),
        by_project=_by_project({"a": ["Tony Welch"], "b": ["Tony Welch"]}),
    )
    owners = _detect_capacity_binding_planned(planned)
    assert owners == [
        {"owner": "Tony Welch", "planned_hours": 46, "project_count": 2}
    ]


def test_binding_planned_basis_by_project_spread():
    """Under the hours floor but on 5+ projects still binds."""
    planned = _allocations(
        (_rollup("Marcello", 25.0),),
        by_project=_by_project(
            {f"p{i}": ["Marcello"] for i in range(5)}
        ),
    )
    owners = _detect_capacity_binding_planned(planned)
    assert owners == [
        {"owner": "Marcello", "planned_hours": 25, "project_count": 5}
    ]


def test_binding_uses_planned_basis_when_allocations_exist(tmp_path):
    planned = _allocations(
        (_rollup("Tony Welch", 46.0),),
        by_project=_by_project({"a": ["Tony Welch"]}),
    )
    result = build_planning_result(
        make_config(tmp_path),
        tuple(make_state(f"ggl-{i}", owner="brandon") for i in range(6)),
        today=date(2026, 6, 25),
        supabase_client=None,
        planned_allocations=planned,
    )
    assert result.capacity_binding["basis"] == "planned_allocations"
    body = "\n".join(_render_cross_cutting(result))
    assert "**Tony Welch** — 46h planned across 1 project this sprint" in body
    # Owner-of-record fact must NOT surface when the planned basis is active.
    assert "owner-of-record" not in body


def test_binding_falls_back_to_owner_of_record_labeled(tmp_path):
    result = build_planning_result(
        make_config(tmp_path),
        tuple(make_state(f"ggl-{i}", owner="brandon") for i in range(6)),
        today=date(2026, 6, 25),
        supabase_client=None,
        planned_allocations=None,
    )
    assert result.capacity_binding["basis"] == "owner_of_record"
    body = "\n".join(_render_cross_cutting(result))
    assert "**brandon** — owner-of-record on 6 projects" in body
    assert "not planned hours" in body


# --- #190: freshness verdict vs. partially-authored regions -----------------


def test_placeholder_fields_flags_full_scaffold():
    """A pristine scaffold reports every authored field as unfilled."""
    from cp_engine.render import exec_summary_placeholder_fields

    region = (
        "## Exec Summary  ·  updated 2026-08-10\n\n"
        "**Last session:** _<date>_\n"
        "**Objective:** _<one line — what this project delivers>_\n"
        "**Status:** _<current state in a phrase>_\n\n"
        "**Where it stands:**\n- _<2-4 dense bullets of current reality>_\n\n"
        "**Next up:**\n- _<concrete near-term moves>_\n\n"
        "**Blockers:**\n- _<what's stuck>_\n\n"
        "**Updates:**\n- _<dated>_\n"
    )
    assert exec_summary_placeholder_fields(region) == (
        "Objective",
        "Status",
        "Where it stands",
        "Next up",
        "Blockers",
        "Updates",
    )


def test_placeholder_fields_flags_partial_authoring():
    """The #190 case: one real field passes exec_summary_is_authored, so the
    `· updated` stamp would render the region FRESH — but five fields are
    still scaffold. Those five must be reported."""
    from cp_engine.render import (
        exec_summary_is_authored,
        exec_summary_placeholder_fields,
    )

    region = (
        "**Last session:** _<date>_\n"
        "**Objective:** _<one line>_\n"
        "**Status:** Actively rebuilding the GRR sheet.\n\n"
        "**Where it stands:**\n- _<2-4 dense bullets>_\n\n"
        "**Next up:**\n- Ship the Firebase requirements doc.\n\n"
        "**Blockers:**\n- _<what's stuck>_\n\n"
        "**Updates:**\n- 2026-07-01 — migrated from Quick Resume\n"
    )
    # Reads as authored — that is exactly why the stamp was trusted.
    assert exec_summary_is_authored(region) is True
    assert exec_summary_placeholder_fields(region) == (
        "Objective",
        "Where it stands",
        "Blockers",
        "Updates",
    )


def test_placeholder_fields_empty_for_fully_authored():
    """A real summary reports nothing, so the freshness verdict is unqualified."""
    from cp_engine.render import exec_summary_placeholder_fields

    region = (
        "**Last session:** 2026-08-18 (Drew) — shipped the close loop.\n"
        "**Objective:** Keep MC-2 running as the canonical store.\n"
        "**Status:** The close loop is complete and visible.\n\n"
        "**Where it stands:**\n- MC-2 is the canonical store.\n\n"
        "**Next up:**\n- Verify the webhook reports 0.100.0.\n\n"
        "**Blockers:**\n- None acute.\n\n"
        "**Updates:**\n- 2026-08-18 — the close loop shipped.\n"
    )
    assert exec_summary_placeholder_fields(region) == ()


def test_migration_bullet_does_not_count_as_authored():
    """The auto-stamped migration line is not human authoring."""
    from cp_engine.render import exec_summary_placeholder_fields

    region = (
        "**Objective:** _<one line>_\n\n"
        "**Updates:**\n- 2026-07-01 — migrated from Quick Resume\n"
    )
    assert "Updates" in exec_summary_placeholder_fields(region)


# --- #191: urgent counters read commitments + drift -------------------------


def _milestone(**kw):
    from cp_engine.prep_planning import Milestone

    base = dict(
        id="c1",
        task_type="milestone",
        deliverable="Findings deck to Louise",
        date="",
        owner="Tony Welch",
        confidence="",
        depends_on=[],
        status="open",
        linked_to=[],
        source="commitments",
    )
    base.update(kw)
    return Milestone(**base)


def test_overdue_commitment_raises_past_due_ask():
    """#191: an open commitment past its due date must count, even with no
    sprint-file ask and no milestone."""
    from datetime import date as _date

    from cp_engine.prep_planning import _detect_urgent

    flags = _detect_urgent(
        project=None,
        milestones=(),
        sprint_asks=(),
        today=_date(2026, 8, 19),
        commitments=(_milestone(date="2026-07-22"),),
    )
    past_due = [f for f in flags if f["type"] == "past_due_ask"]
    assert len(past_due) == 1
    assert "28d past due" in past_due[0]["text"]
    # 28 days out is well beyond the horizon — escalate, don't just warn.
    assert past_due[0]["severity"] == "alert"


def test_future_and_closed_commitments_do_not_flag():
    from datetime import date as _date

    from cp_engine.prep_planning import _detect_urgent

    flags = _detect_urgent(
        project=None,
        milestones=(),
        sprint_asks=(),
        today=_date(2026, 8, 19),
        commitments=(
            _milestone(date="2026-09-30"),  # future
            _milestone(date="2026-07-22", status="done"),  # already closed
            _milestone(date=""),  # undated
        ),
    )
    assert [f for f in flags if f["type"] == "past_due_ask"] == []


def test_drift_warning_raises_slip_risk():
    """#191: the bundle showed 18 drift lines while reporting 0 slip risks."""
    from datetime import date as _date

    from cp_engine.prep_planning import _detect_urgent

    flags = _detect_urgent(
        project=None,
        milestones=(),
        sprint_asks=(),
        today=_date(2026, 8, 19),
        drift=(
            "⚠ Creative Concept Development — past due ~2026-08-17, no done-mark",
            "⚠ Narrative Messaging Architecture — linked meeting 2026-06-30 vs "
            "estimate ~2026-04-27 (+64d)",
        ),
    )
    slips = [f for f in flags if f["type"] == "slip_risk"]
    # Only the past-due line counts; the linked-meeting skew is drift, not slip.
    assert len(slips) == 1
    assert slips[0]["text"].startswith("Creative Concept Development")


def test_no_commitments_or_drift_is_still_quiet():
    """A genuinely calm project must not gain phantom flags."""
    from datetime import date as _date

    from cp_engine.prep_planning import _detect_urgent

    assert (
        _detect_urgent(
            project=None,
            milestones=(),
            sprint_asks=(),
            today=_date(2026, 8, 19),
        )
        == []
    )
