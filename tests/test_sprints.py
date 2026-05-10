from pathlib import Path

import pytest

from cp_engine.sprints import parse_sprint_file, render_sprint_scaffold
from cp_engine.state import CarryForward, ClientAsk, PersonHours, ProjectState


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
