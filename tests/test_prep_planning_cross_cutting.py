"""Tests for Task 8 — cross-cutting block in cp_engine.prep_planning.

Covers:
  - _detect_capacity_binding (>=5-projects floor, sort, empty)
  - _load_cross_cutting_decisions (lookback, [decided|resolved: ...] filter,
    empty file, 28-day boundary inclusive)
  - _render_cross_cutting (omits sub-blocks when empty, full golden, no-op case)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cp_engine import (
    ProjectState,
    SyncConfig,
    TenantConfig,
)
from cp_engine.prep_planning import (
    _detect_capacity_binding,
    _load_cross_cutting_decisions,
    _render_cross_cutting,
    build_planning_result,
    render_planning_doc,
)


# ──────────────────────────────────────────────────────────────────────
#  Fixtures (mirror test_prep_planning.py for consistency)
# ──────────────────────────────────────────────────────────────────────


def make_config(tenant_root: Path) -> TenantConfig:
    return TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tenant_root,
    )


def make_state(
    code: str,
    *,
    name: str | None = None,
    company_kind: str = "client",
    company_code: str | None = "GGL",
    company_name: str | None = "Google",
    owner: str = "drew",
    status: str = "Open",
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name if name is not None else code,
        source="engagement",  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=False,
        owner=owner,
        last_touched=datetime(2026, 6, 1, tzinfo=timezone.utc),
        deadline=None,
        one_line_summary=None,
    )


# Standard weekly-cp.md section header used by the parser.
_DECISIONS_HEADER = "## Decisions (cross-cutting, last 4 weeks)\n\n"


def _write_weekly_cp(tenant_root: Path, body: str) -> None:
    """Lay down a weekly-cp.md at the tenant root."""
    (tenant_root / "weekly-cp.md").write_text(body, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
#  Capacity-binding detection
# ──────────────────────────────────────────────────────────────────────


def test_capacity_binding_flags_owner_with_5plus_projects():
    """An owner credited on 5 projects of record clears the floor."""
    projects = tuple(
        make_state(f"ggl-{i}", owner="tony") for i in range(5)
    )
    binding = _detect_capacity_binding(projects)
    assert ("tony", 5) in binding


def test_capacity_binding_no_binding_when_all_owners_under_5():
    """Two owners with 4 and 3 projects → no capacity binding."""
    projects = (
        *(make_state(f"a-{i}", owner="drew") for i in range(4)),
        *(make_state(f"b-{i}", owner="tony") for i in range(3)),
    )
    binding = _detect_capacity_binding(projects)
    assert binding == ()


def test_capacity_binding_orders_by_count_desc():
    """Highest count first; ties resolved alphabetically by name."""
    projects = (
        *(make_state(f"a-{i}", owner="tony") for i in range(6)),
        *(make_state(f"b-{i}", owner="drew") for i in range(5)),
        *(make_state(f"c-{i}", owner="brandon") for i in range(5)),
    )
    binding = _detect_capacity_binding(projects)
    # tony (6) first; brandon/drew tied at 5, alphabetical → brandon, drew.
    assert binding == (("tony", 6), ("brandon", 5), ("drew", 5))


def test_capacity_binding_ignores_unassigned_owners():
    """Owner value of '—' or empty must not count toward binding."""
    projects = (
        *(make_state(f"x-{i}", owner="—") for i in range(7)),
        *(make_state(f"y-{i}", owner="") for i in range(7)),
    )
    binding = _detect_capacity_binding(projects)
    assert binding == ()


# ──────────────────────────────────────────────────────────────────────
#  weekly-cp.md decision loading
# ──────────────────────────────────────────────────────────────────────


def test_cross_cutting_decisions_renders_from_weekly_cp(tmp_path):
    """Decisions in the last 4 weeks surface in numbered order from the file."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "3. **Marcello capacity triage** — defer persona regression. "
        "(2026-05-30, source: sprint planning)\n\n"
        "2. **SAP delivery date conflict** — who calls SAP? "
        "(2026-05-28, source: sap-5100)\n\n"
        "1. **Tony hidden-load** — re-attribute owner on implicit projects. "
        "(2026-05-25, source: weekly account meeting)\n",
    )
    decisions, errors = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 2)
    )
    assert len(decisions) == 3
    assert errors == []
    texts = [d.text for d in decisions]
    assert any("Marcello capacity triage" in t for t in texts)
    assert any("SAP delivery date conflict" in t for t in texts)
    assert any("Tony hidden-load" in t for t in texts)


def test_cross_cutting_decisions_empty_section_omitted(tmp_path):
    """weekly-cp.md with the header but no entries → empty tuple."""
    _write_weekly_cp(tmp_path, _DECISIONS_HEADER + "\n## Account summaries\n")
    decisions, errors = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 2)
    )
    assert decisions == ()
    assert errors == []


def test_cross_cutting_decisions_missing_file_returns_empty(tmp_path):
    """No weekly-cp.md at all → empty tuple, not a crash."""
    decisions, errors = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 2)
    )
    assert decisions == ()
    assert errors == []


def test_cross_cutting_decisions_resolved_marker_filtered(tmp_path):
    """Entries with '[decided: ...]' markers are dropped."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "2. **Still owed** — partners must decide X. "
        "(2026-05-30, source: sprint planning)\n\n"
        "1. **Already settled** — [decided: 2026-05-29] partners agreed Y. "
        "(2026-05-28, source: sprint planning)\n",
    )
    decisions, _errors = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 2)
    )
    assert len(decisions) == 1
    assert "Still owed" in decisions[0].text


def test_cross_cutting_decisions_old_entries_filtered(tmp_path):
    """Entries older than 4 weeks (28 days) are dropped."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "2. **Recent** — within window. "
        "(2026-05-30, source: sprint planning)\n\n"
        "1. **Stale** — older than 4 weeks. "
        "(2026-04-15, source: weekly account meeting)\n",
    )
    decisions, _errors = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 2)
    )
    assert len(decisions) == 1
    assert "Recent" in decisions[0].text


def test_cross_cutting_decisions_resolved_marker_filtered_with_resolved_verb(tmp_path):
    """[resolved: ...] markers are filtered out same as [decided: ...]."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "2. **Still owed** — partners must decide X. "
        "(2026-05-30, source: sprint planning)\n\n"
        "1. **Already settled** — [resolved: 2026-05-29] partners agreed Y. "
        "(2026-05-28, source: sprint planning)\n",
    )
    decisions, _errors = _load_cross_cutting_decisions(
        tmp_path, today=date(2026, 6, 2)
    )
    assert len(decisions) == 1
    assert "Still owed" in decisions[0].text


def test_cross_cutting_decisions_included_at_exactly_28_days(tmp_path):
    """Decision dated exactly today - 28 days is INCLUDED (boundary inclusive).

    The filter is ``d_date < today - lookback_days`` so an entry whose date
    equals the cutoff sticks around — the "last 4 weeks" header is read as
    inclusive of day 28.
    """
    today = date(2026, 6, 2)
    boundary = today - timedelta(days=28)
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + f"1. **On the boundary** — exactly 28 days back. "
        f"({boundary.isoformat()}, source: sprint planning)\n",
    )
    decisions, _errors = _load_cross_cutting_decisions(tmp_path, today=today)
    assert len(decisions) == 1
    assert "On the boundary" in decisions[0].text


def test_cross_cutting_decisions_excluded_at_29_days(tmp_path):
    """Decision dated today - 29 days is EXCLUDED (just past the window)."""
    today = date(2026, 6, 2)
    past_boundary = today - timedelta(days=29)
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + f"1. **Just past** — 29 days back. "
        f"({past_boundary.isoformat()}, source: sprint planning)\n",
    )
    decisions, _errors = _load_cross_cutting_decisions(tmp_path, today=today)
    assert decisions == ()


def test_cross_cutting_decisions_malformed_date_skipped_with_warning(
    tmp_path, caplog
):
    """Malformed-date entries are SKIPPED (not surfaced with d_date=today)
    AND surface as an error in the summary + a warning in the logs.

    Pre-v0.15 the fallback was ``d_date = today``, which silently kept a
    typo'd 2019-dated decision in scope forever. The new behavior is to
    skip + warn so partners can spot the bad data.
    """
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "2. **Good entry** — within window. "
        "(2026-05-30, source: sprint planning)\n\n"
        "1. **Bad entry** — typo'd date. "
        "(2026-13-99, source: sprint planning)\n",
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="cp_engine.prep_planning"):
        decisions, errors = _load_cross_cutting_decisions(
            tmp_path, today=date(2026, 6, 2)
        )

    # Only the valid entry survives.
    assert len(decisions) == 1
    assert "Good entry" in decisions[0].text
    # The bad entry is dropped entirely, not surfaced with d_date=today.
    assert all("Bad entry" not in d.text for d in decisions)
    # The bad date shows up in the error list (for --summary).
    assert len(errors) == 1
    assert "2026-13-99" in errors[0]
    # And in the warning log.
    assert any(
        "malformed date" in rec.message.lower() for rec in caplog.records
    )


def test_cross_cutting_decisions_valid_date_no_error(tmp_path, caplog):
    """Sanity: a fully valid file produces no errors and no warnings."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "1. **Clean entry** — well-formed. "
        "(2026-05-30, source: sprint planning)\n",
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="cp_engine.prep_planning"):
        decisions, errors = _load_cross_cutting_decisions(
            tmp_path, today=date(2026, 6, 2)
        )

    assert len(decisions) == 1
    assert errors == []
    assert not any(
        "malformed date" in rec.message.lower() for rec in caplog.records
    )


# ──────────────────────────────────────────────────────────────────────
#  _render_cross_cutting
# ──────────────────────────────────────────────────────────────────────


def _make_result(
    *,
    project_count: int = 3,
    tenant_hours: dict[str, int] | None = None,
    capacity_binding=(),
    cross_cutting_decisions=(),
):
    """Build a minimal PlanningResult for renderer tests."""
    from cp_engine.prep_planning import PlanningResult

    return PlanningResult(
        week_iso="2026-W23",
        week_dates="Jun 1 – Jun 7",
        project_count=project_count,
        estimated_minutes=60,
        tenant_hours_last_week=tenant_hours or {},
        blocks_by_account={},
        milestone_counts={"total": 0, "fetched": 0, "errored": 0},
        urgent_counts={"slip_risk": 0, "decision_due": 0,
                       "past_due_ask": 0, "escalated_risk": 0},
        capacity_binding=capacity_binding,
        cross_cutting_decisions=cross_cutting_decisions,
    )


def test_render_cross_cutting_returns_tenant_strip_when_no_cross_cutting_signals():
    """Minimum-output case: tenant strip + 'no signals' line."""
    result = _make_result(project_count=4, tenant_hours={"Drew": 40, "Tony": 30})
    lines = _render_cross_cutting(result)
    body = "\n".join(lines)
    assert "## Tenant strip" in body
    assert "**Active:** 4" in body
    assert "Drew 40h" in body
    assert "## Cross-cutting (read before walking projects)" in body
    assert "no cross-cutting signals" in body
    # Sub-blocks must not appear when both lists are empty.
    assert "Capacity binding constraints" not in body
    assert "Decisions partners owe" not in body


def test_render_cross_cutting_full_output():
    """Both sub-blocks render when capacity binding + decisions present."""
    from cp_engine.agenda import WeeklyDecision

    decisions = (
        WeeklyDecision(
            number=2,
            text="**Marcello capacity** — defer persona regression OR cut StoryOS scope?",
            date="2026-05-30",
            sources=("sprint planning",),
        ),
        WeeklyDecision(
            number=1,
            text="**Tony hidden-load** — re-attribute owner on 6 implicit projects.",
            date="2026-05-28",
            sources=("weekly account meeting",),
        ),
    )
    result = _make_result(
        project_count=10,
        tenant_hours={"Drew": 52, "Tony": 52, "Marcello": 42},
        capacity_binding=(("tony", 6), ("marcello", 5)),
        cross_cutting_decisions=decisions,
    )
    lines = _render_cross_cutting(result)
    body = "\n".join(lines)
    # Tenant strip + cross-cutting header always present.
    assert "## Tenant strip" in body
    assert "**Active:** 10" in body
    assert "## Cross-cutting (read before walking projects)" in body
    # Capacity binding renders both owners with correct counts.
    assert "**Capacity binding constraints:**" in body
    assert "**tony** — owner-of-record on 6 projects" in body
    assert "**marcello** — owner-of-record on 5 projects" in body
    # Decisions render numbered, preserving partners' input order.
    assert "**Decisions partners owe each other this week:**" in body
    assert "1. **Marcello capacity**" in body
    assert "2. **Tony hidden-load**" in body
    # The placeholder no-signals line must not leak in.
    assert "no cross-cutting signals" not in body


def test_render_cross_cutting_capacity_only_no_decisions():
    """Capacity present but decisions empty → only capacity sub-block shown."""
    result = _make_result(
        project_count=5,
        tenant_hours={"Drew": 40},
        capacity_binding=(("tony", 5),),
        cross_cutting_decisions=(),
    )
    body = "\n".join(_render_cross_cutting(result))
    assert "**Capacity binding constraints:**" in body
    assert "**tony** — owner-of-record on 5 projects" in body
    assert "Decisions partners owe each other" not in body
    assert "no cross-cutting signals" not in body


def test_render_cross_cutting_decisions_only_no_capacity():
    """Decisions present but no capacity binding → only decisions sub-block."""
    from cp_engine.agenda import WeeklyDecision

    decisions = (
        WeeklyDecision(
            number=1,
            text="**SAP delivery date conflict** — who calls SAP, when?",
            date="2026-05-30",
            sources=("sap-5100",),
        ),
    )
    result = _make_result(
        capacity_binding=(),
        cross_cutting_decisions=decisions,
    )
    body = "\n".join(_render_cross_cutting(result))
    assert "Capacity binding constraints" not in body
    assert "**Decisions partners owe each other this week:**" in body
    assert "1. **SAP delivery date conflict**" in body


def test_render_cross_cutting_singular_project_when_count_is_one():
    """Singular 'project' when exactly 1 (regression guard)."""
    result = _make_result(
        capacity_binding=(("tony", 1),),
    )
    # An owner with 1 project would not normally clear the floor — but the
    # renderer must still grammar-correctly if that data ever arrives.
    body = "\n".join(_render_cross_cutting(result))
    assert "**tony** — owner-of-record on 1 project" in body
    # Make sure the 's' suffix isn't accidentally appended.
    assert "1 projects" not in body


# ──────────────────────────────────────────────────────────────────────
#  End-to-end: build_planning_result wires capacity + decisions
# ──────────────────────────────────────────────────────────────────────


def test_build_planning_result_populates_capacity_and_decisions(tmp_path):
    """build_planning_result threads weekly-cp.md + owner counts into the result."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "1. **Open partners decision** — must resolve. "
        "(2026-05-30, source: sprint planning)\n",
    )
    projects = tuple(
        make_state(f"ggl-{i}", name=f"GGL {i}", owner="tony")
        for i in range(5)
    )
    config = make_config(tmp_path)
    result = build_planning_result(
        config,
        projects,
        today=date(2026, 6, 2),
        tenant_hours_last_week={"Tony": 40},
        supabase_client=None,
    )
    # Owner "tony" has 5 of 5 projects → clears floor.
    assert ("tony", 5) in result.capacity_binding
    # Decision parsed from weekly-cp.md.
    assert len(result.cross_cutting_decisions) == 1
    assert "Open partners decision" in result.cross_cutting_decisions[0].text


def test_render_planning_doc_includes_cross_cutting_section(tmp_path):
    """Golden: full doc renders the cross-cutting block with real content."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "1. **Settle SAP date** — partners owe an answer. "
        "(2026-05-30, source: sprint planning)\n",
    )
    projects = tuple(
        make_state(f"p-{i}", name=f"P {i}", owner="marcello") for i in range(5)
    )
    config = make_config(tmp_path)
    doc = render_planning_doc(
        config,
        projects,
        today=date(2026, 6, 2),
        tenant_hours_last_week={"Marcello": 50},
        supabase_client=None,
    )
    assert "## Cross-cutting (read before walking projects)" in doc
    assert "**marcello** — owner-of-record on 5 projects" in doc
    assert "**Decisions partners owe each other this week:**" in doc
    assert "Settle SAP date" in doc


def test_summary_dict_exposes_cross_cutting_counts(tmp_path):
    """to_summary_dict surfaces capacity + decisions count for the JSON mode."""
    _write_weekly_cp(
        tmp_path,
        _DECISIONS_HEADER
        + "1. **Open** — needs decision. (2026-05-30, source: x)\n",
    )
    projects = tuple(
        make_state(f"q-{i}", name=f"Q {i}", owner="drew") for i in range(5)
    )
    config = make_config(tmp_path)
    result = build_planning_result(
        config,
        projects,
        today=date(2026, 6, 2),
        tenant_hours_last_week={"Drew": 40},
        supabase_client=None,
    )
    summary = result.to_summary_dict()
    assert summary["capacity_binding"] == [{"owner": "drew", "count": 5}]
    assert summary["cross_cutting_decisions_count"] == 1
