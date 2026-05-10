from datetime import datetime
from pathlib import Path

import pytest

from cp_engine.sprints import (
    current_sprint_week_iso,
    is_in_sprint_window,
    parse_sprint_file,
    render_sprint_scaffold,
)
from cp_engine.state import CarryForward, ClientAsk, PersonHours, ProjectState


def _fixture_project(*, code: str = "peb", status: str = "active") -> ProjectState:
    """Reusable ProjectState for sprint-file tests.

    Mirrors the inline shape used in the round-trip test below: a Pebble
    Foods engagement in Negotiation. ensure_sprint_file tests assert that
    this project's `deal_stage` ("Negotiation") shows up in the rendered
    sprint-facts region — keep that field stable.

    `code` and `status` are overridable so tests that exercise the
    orchestrator can build distinguishable projects (e.g. an active "peb"
    vs. a holding "apx") without spelling out the full ProjectState shape.
    """
    return ProjectState(
        code=code,
        name="Pebble Foods",
        source="engagement",
        company_kind="client",
        company_code="PEB",
        company_name="Pebble Foods",
        status=status,
        is_internal=False,
        owner="Drew",
        last_touched=None,
        deadline=None,
        deal_stage="Negotiation",
        budget=45000.0,
    )


def test_parse_sprint_file_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_sprint_file(tmp_path / "missing.md")


def test_parse_sprint_file_extracts_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W19\n"
        "PriorSprint: 2026-W18\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
    )
    sf = parse_sprint_file(f)
    assert sf.project_code == "peb"
    assert sf.week_iso == "2026-W19"
    assert sf.prior_sprint == "2026-W18"
    assert sf.week_start == "2026-05-11"
    assert sf.week_end == "2026-05-17"


def test_parse_sprint_file_handles_year_boundary_dates(tmp_path: Path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W53\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W53 (Dec 28 – Jan 3, 2027)\n"
    )
    sf = parse_sprint_file(f)
    # The heading anchors on the year following the date range; the test
    # asserts that BOTH dates parse against that year, regardless of when
    # the test runs. Without the fix, the start date would be stamped as
    # the current year rather than the heading's year (2027).
    assert sf.week_start == "2027-12-28"
    assert sf.week_end == "2027-01-03"


def test_parse_sprint_facts_region(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n\n"
        "<!-- cp-engine:start sprint-facts -->\n"
        "| | |\n|---|---|\n"
        "| Stage | Negotiation |\n"
        "| Owner | Drew |\n"
        "| Budget | $45,000 |\n"
        "| Last touched | 2 days ago |\n"
        "| Last sprint hours | Drew 6.5h · Tony 2h |\n"
        "| Sessions this week | 3 |\n"
        "| Open issues | 3 |\n"
        "<!-- cp-engine:end sprint-facts -->\n"
    )
    sf = parse_sprint_file(f)
    assert sf.facts.stage == "Negotiation"
    assert sf.facts.owner == "Drew"
    assert sf.facts.budget_short == "$45,000"
    assert sf.facts.sessions_this_week == 3
    assert sf.facts.open_issues == 3


def test_parse_client_section_extracts_outbound_asks_inbound(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
        "## Client communication\n\n"
        "### Outbound\n"
        "- [sent · 2026-05-09] Counter-proposal pricing draft sent to Maria + Sam\n"
        "- [draft · queued] Schedule contracting call for week of May 18\n"
        "  Send after their pricing response lands\n"
        "\n### Open asks\n"
        "- [open · 2026-05-04 · Maria] Revised volume forecast from ops team\n"
        "  Asked May 4 · blocking pricing finalization\n"
        "\n### Inbound\n"
        "- [2026-05-09 · Maria] \"Tier-2 cap doesn't match our 2H projections.\"\n"
    )
    sf = parse_sprint_file(f)
    assert len(sf.client_outbound) == 2
    assert sf.client_outbound[0].status == "sent"
    assert sf.client_outbound[0].date == "2026-05-09"
    assert sf.client_open_asks[0].who == "Maria"
    assert sf.client_open_asks[0].asked_date == "2026-05-04"
    assert sf.client_inbound[0].who == "Maria"


def test_parse_risks_and_horizon(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
        "## Dependencies & risks\n"
        "- [escalated · contract · 2026-05-04] Legal turnaround may slip past May 22.\n"
        "  Why it matters: pushes contract into next sprint.\n"
        "- [watching · pricing · 2026-05-09] Tier-2 pushback may force rebuild.\n"
        "\n## Horizon\n"
        "### Milestones\n"
        "- [2026-05-22] Contract target sign date\n"
        "### Decisions due\n"
        "- [by W21] Whether to staff a third on Pebble for Q3\n"
        "### Opportunities\n"
        "- Stage discovery for Pebble's sister brand\n"
    )
    sf = parse_sprint_file(f)
    assert len(sf.risks) == 2
    assert sf.risks[0].severity == "escalated"
    assert sf.risks[0].category == "contract"
    assert sf.risks[0].why_it_matters and "next sprint" in sf.risks[0].why_it_matters
    assert len(sf.horizon) == 3
    assert sf.horizon[0].bucket == "milestone"
    assert sf.horizon[1].bucket == "decision"
    assert sf.horizon[2].bucket == "opportunity"
    assert sf.horizon[2].target_date is None


def test_parse_this_sprint_section(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
        "## This sprint\n"
        "**Allocation:** Drew · 6h · Tony · 2h\n\n"
        "### Deliverables\n"
        "1. Pricing model finalized\n"
        "2. Discovery deck reviewed\n"
        "3. Legal redline reconciled\n\n"
        "### Definition of done\n"
        "Pricing accepted in principle and contract draft v2 ready for signature.\n"
    )
    sf = parse_sprint_file(f)
    assert sf.allocation == (
        PersonHours(person_name="Drew", hours=6.0),
        PersonHours(person_name="Tony", hours=2.0),
    )
    assert len(sf.deliverables) == 3
    assert sf.deliverables[0].position == 1
    assert "Pricing accepted" in sf.definition_of_done


def test_parse_carry_forward_and_meeting_notes(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\nPriorSprint: 2026-W18\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
        "<!-- cp-engine:start carry-forward -->\n"
        "## Carried over from W18\n"
        "- [ask · 2026-05-04 · Maria] Volume forecast still open\n"
        "- [risk · escalated · contract · 2026-05-04] Legal turnaround\n"
        "<!-- cp-engine:end carry-forward -->\n"
        "## Meeting notes & decisions\n"
        "_From sprint planning · May 11 · Drew + Tony · 22 min_\n\n"
        "### Decisions\n"
        "1. Hold tier-2 cap firm; widen tier-3 ramp.\n"
        "2. Drew owns reconciling §4.2 directly with Sam.\n\n"
        "### Discussion notes\n"
        "Spent most of the time on the volume-forecast pushback.\n"
    )
    sf = parse_sprint_file(f)
    assert len(sf.carry_forward.asks) == 1
    assert sf.carry_forward.asks[0].who == "Maria"
    assert len(sf.carry_forward.risks) == 1
    assert sf.carry_forward.risks[0].category == "contract"
    assert sf.meeting_notes is not None
    assert sf.meeting_notes.attendees == "Drew + Tony"
    assert sf.meeting_notes.duration == "22 min"
    assert len(sf.meeting_notes.decisions) == 2
    assert "volume-forecast" in sf.meeting_notes.discussion_prose


def test_render_sprint_scaffold_round_trips_through_parser(tmp_path: Path) -> None:
    project = ProjectState(
        code="peb",
        name="Pebble Foods",
        source="engagement",
        company_kind="client",
        company_code="PEB",
        company_name="Pebble Foods",
        status="Deal",
        is_internal=False,
        owner="Drew",
        last_touched=None,
        deadline=None,
        deal_stage="Negotiation",
        budget=45000.0,
    )
    body = render_sprint_scaffold(
        project=project,
        week_iso="2026-W19",
        week_label="W19",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint="2026-W18",
        last_sprint_hours_line="Drew 6.5h · Tony 2h",
        sessions_this_week=3,
        last_session_date=None,
        last_session_who=None,
        last_session_summary=None,
        recent_commits=(),
        open_issues=(),
        carry_forward=CarryForward(
            asks=(
                ClientAsk(
                    text="Volume forecast",
                    asked_date="2026-05-04",
                    status="open",
                    who="Maria",
                ),
            ),
            risks=(),
            horizon=(),
        ),
    )
    f = tmp_path / "peb.md"
    f.write_text(body)
    sf = parse_sprint_file(f)
    assert sf.project_code == "peb"
    assert sf.facts.stage == "Negotiation"
    assert sf.facts.owner == "Drew"
    assert len(sf.carry_forward.asks) == 1
    assert sf.carry_forward.asks[0].who == "Maria"


def test_compute_carry_forward_from_prior_sprint_file(tmp_path) -> None:
    from cp_engine.sprints import compute_carry_forward
    prior = tmp_path / "2026-W18" / "peb.md"
    prior.parent.mkdir(parents=True)
    prior.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W18\n---\n"
        "# peb — Pebble Foods · Sprint W18 (May 4 – May 10, 2026)\n"
        "## Client communication\n### Open asks\n"
        "- [open · 2026-05-04 · Maria] Volume forecast\n"
        "- [answered · 2026-05-06 · Sam] Contract sign-off\n"
        "## Dependencies & risks\n"
        "- [escalated · contract · 2026-05-04] Legal slip risk\n"
        "- [resolved · pricing · 2026-05-02] Tier discussion\n"
        "## Horizon\n### Decisions due\n"
        "- [by W21] Staff a third on Pebble for Q3\n"
    )
    cf = compute_carry_forward(prior)
    assert len(cf.asks) == 1  # only `open`
    assert cf.asks[0].who == "Maria"
    assert len(cf.risks) == 1  # only `escalated`/`watching`
    assert len(cf.horizon) == 1


def test_ensure_sprint_file_creates_new(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_file
    out = ensure_sprint_file(
        project=_fixture_project(),
        sprint_root=tmp_path / "sprints",
        week_iso="2026-W19",
        week_label="W19",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint=None,
        last_sprint_hours_line=None,
        sessions_this_week=0,
        last_session_date=None, last_session_who=None, last_session_summary=None,
        recent_commits=(), open_issues=(),
    )
    assert out.exists()
    assert "<!-- cp-engine:start sprint-facts -->" in out.read_text()


def test_ensure_sprint_file_preserves_handwritten_when_present(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_file
    out_dir = tmp_path / "sprints" / "2026-W19"
    out_dir.mkdir(parents=True)
    existing = out_dir / "peb.md"
    existing.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
        "<!-- cp-engine:start sprint-facts -->\n"
        "| | |\n|---|---|\n| Stage | Stale |\n"
        "<!-- cp-engine:end sprint-facts -->\n"
        "## Client communication\n### Outbound\n"
        "- [sent · 2026-05-11] Custom hand-written note\n"
    )
    ensure_sprint_file(
        project=_fixture_project(),
        sprint_root=tmp_path / "sprints",
        week_iso="2026-W19",
        week_label="W19", week_start="2026-05-11", week_end="2026-05-17",
        prior_sprint=None, last_sprint_hours_line=None, sessions_this_week=0,
        last_session_date=None, last_session_who=None, last_session_summary=None,
        recent_commits=(), open_issues=(),
    )
    body = existing.read_text()
    assert "Custom hand-written note" in body  # preserved
    assert "Stage | Negotiation" in body  # engine region refreshed
    assert "Stage | Stale" not in body


def test_ensure_sprint_file_is_idempotent(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_file
    kwargs = dict(
        project=_fixture_project(),
        sprint_root=tmp_path / "sprints",
        week_iso="2026-W19", week_label="W19",
        week_start="2026-05-11", week_end="2026-05-17",
        prior_sprint=None, last_sprint_hours_line=None, sessions_this_week=0,
        last_session_date=None, last_session_who=None, last_session_summary=None,
        recent_commits=(), open_issues=(),
    )
    p1 = ensure_sprint_file(**kwargs)
    body1 = p1.read_text()
    p2 = ensure_sprint_file(**kwargs)
    body2 = p2.read_text()
    assert body1 == body2


@pytest.mark.parametrize("dt,expected", [
    (datetime(2026, 5, 11, 8, 0), True),  # Monday
    (datetime(2026, 5, 13, 8, 0), True),  # mid-week
    (datetime(2026, 5, 17, 23, 0), True),  # Sunday end
])
def test_is_in_sprint_window(dt: datetime, expected: bool) -> None:
    assert is_in_sprint_window(dt) is expected


def test_current_sprint_week_iso() -> None:
    assert current_sprint_week_iso(datetime(2026, 5, 13)) == "2026-W19"


def test_ensure_sprint_files_for_active_projects_writes_one_per_active(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_files_for_active_projects
    proj_active = _fixture_project(code="peb", status="active")
    proj_holding = _fixture_project(code="apx", status="holding")
    paths = ensure_sprint_files_for_active_projects(
        active_projects=(proj_active, proj_holding),
        sprint_root=tmp_path / "sprints",
        now=datetime(2026, 5, 13, 8, 0),
        per_project_data={},
    )
    assert any(p.name == "peb.md" for p in paths)
    assert not any(p.name == "apx.md" for p in paths)
