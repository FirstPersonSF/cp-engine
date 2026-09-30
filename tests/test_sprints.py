from datetime import datetime
from pathlib import Path

import pytest

from cp_engine.sprints import (
    section_body,
    current_sprint_week_iso,
    is_in_sprint_window,
    parse_sprint_file,
    prior_sprint_week_iso,
    render_sprint_scaffold,
    sprint_week_dates,
)
from cp_engine.state import (
    CarryForward,
    ClientAsk,
    PersonHours,
    ProjectState,
    Risk,
    SprintFacts,
    SprintFile,
    WhereItStands,
)


def _fixture_project(
    *,
    code: str = "peb",
    status: str = "Open",
    has_agreement: bool = True,
    is_internal: bool = False,
    company_kind: str = "client",
) -> ProjectState:
    """Reusable ProjectState for sprint-file tests.

    Mirrors the inline shape used in the round-trip test below: a Pebble
    Foods engagement in Negotiation. ensure_sprint_file tests assert that
    this project's `deal_stage` ("Negotiation") shows up in the rendered
    sprint-facts region — keep that field stable.

    Overridable kwargs let tests build distinguishable projects: an
    active "peb" engagement (defaults), a holding engagement (status=
    "Holding"), or an FPSF/Canonic repo (has_agreement=False, status="Active",
    company_kind="self-fpsf" / "self-canonic").
    """
    return ProjectState(
        code=code,
        name="Pebble Foods",
        has_agreement=has_agreement,
        company_kind=company_kind,
        company_code="PEB",
        company_name="Pebble Foods",
        status=status,
        is_internal=is_internal,
        owner="Drew",
        last_touched=None,
        deadline=None,
        deal_stage="Negotiation",
        budget=45000.0,
    )


def _fixture_sprint_file(
    *,
    project_code: str = "peb",
    asks: tuple[str, ...] = (),
    risks: tuple[str, ...] = (),
    allocation_line: str = "Drew 6h · Tony 2h",
    link: str = "../../sprints/2026-W20/peb.md",
    week_label: str = "W19",
    dates: str = "May 11 – May 17",
) -> SprintFile:
    """Build a minimal SprintFile for renderer tests.

    `asks` are plain strings turned into ClientAsk(open). `risks` entries
    are "<severity>:<text>" pairs. `allocation_line` is a "Name Nh · …"
    string parsed into PersonHours tuples. The unused `link`, `week_label`,
    and `dates` knobs exist so callers can document intent — the renderer
    derives display values from `week_iso`/`week_start`/`week_end` directly.
    """
    parsed_risks: list[Risk] = []
    for entry in risks:
        sev, _, text = entry.partition(":")
        parsed_risks.append(
            Risk(
                text=text,
                severity=sev,
                category="",
                raised_date="2026-05-04",
            )
        )
    parsed_alloc: list[PersonHours] = []
    for chunk in allocation_line.split(" · "):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, hrs = chunk.rpartition(" ")
        parsed_alloc.append(
            PersonHours(person_name=name.strip(), hours=float(hrs.rstrip("h")))
        )
    week_num = week_label.lstrip("W")
    return SprintFile(
        project_code=project_code,
        week_iso=f"2026-W{week_num}",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint=None,
        facts=SprintFacts(None, None, None, None, None, 0, 0),
        where_it_stands=WhereItStands(None, None, None, (), ()),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        client_outbound=(),
        client_open_asks=tuple(
            ClientAsk(text=a, asked_date="2026-05-04", status="open") for a in asks
        ),
        client_inbound=(),
        risks=tuple(parsed_risks),
        allocation=tuple(parsed_alloc),
        deliverables=(),
        definition_of_done="",
        horizon=(),
        meeting_notes=None,
    )


def test_parse_sprint_file_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_sprint_file(tmp_path / "missing.md")


def test_parse_sprint_file_extracts_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W20\n"
        "PriorSprint: 2026-W19\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
    )
    sf = parse_sprint_file(f)
    assert sf.project_code == "peb"
    assert sf.week_iso == "2026-W20"
    assert sf.prior_sprint == "2026-W19"
    assert sf.week_start == "2026-05-11"
    assert sf.week_end == "2026-05-17"


def test_parse_sprint_file_handles_year_boundary_dates(tmp_path: Path) -> None:
    """Cross-year sprint (W53 spanning Dec→Jan): the H1 carries one year for
    both dates, so the legacy regex-only parser stamped the start as the
    H1's trailing year (wrong for the start half of the span). Fix 6
    derives dates from ``week_iso`` (``Sprint: 2026-W53``) via
    ``date.fromisocalendar`` so start = 2026-12-28 and end = 2027-01-03.
    """
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W53\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W53 (Dec 28 – Jan 3, 2027)\n"
    )
    sf = parse_sprint_file(f)
    # week_iso is the authoritative source — H1's single year is ignored
    # for the structural derivation.
    assert sf.week_start == "2026-12-28"
    assert sf.week_end == "2027-01-03"


def test_parse_sprint_file_same_year_week_still_correct(tmp_path: Path) -> None:
    """Regression for Fix 6: same-year weeks (the common case) still
    derive correct dates from ``week_iso``."""
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W22\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W22 (May 25 – May 31, 2026)\n"
    )
    sf = parse_sprint_file(f)
    # ISO 2026-W22 starts Monday May 25 and ends Sunday May 31.
    assert sf.week_start == "2026-05-25"
    assert sf.week_end == "2026-05-31"


def test_parse_sprint_facts_region(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n\n"
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
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
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


def test_parse_client_section_skips_outbound_scaffold_placeholder(tmp_path) -> None:
    """The Outbound scaffold's ``- _<...>_`` placeholder must not turn into
    a ghost Outbound entry. Mirrors ``_parse_horizon``'s placeholder filter.
    """
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
        "## Client communication\n\n"
        "### Outbound\n"
        "- _<message — `[status · date]` prefix>_\n"
    )
    sf = parse_sprint_file(f)
    assert sf.client_outbound == ()


def test_parse_client_section_real_outbound_after_placeholder_survives(tmp_path) -> None:
    """Regression: real Outbound bullets still parse when the scaffold
    placeholder is present (mixed state during the first edits of a
    sprint file)."""
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
        "## Client communication\n\n"
        "### Outbound\n"
        "- _<message — `[status · date]` prefix>_\n"
        "- [sent · 2026-05-11] Pricing follow-up sent\n"
    )
    sf = parse_sprint_file(f)
    assert len(sf.client_outbound) == 1
    assert sf.client_outbound[0].status == "sent"
    assert sf.client_outbound[0].date == "2026-05-11"


def test_parse_risks_and_horizon(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
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
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
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
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\nPriorSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
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
        has_agreement=True,
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
        week_iso="2026-W20",
        week_label="W19",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint="2026-W19",
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
    prior = tmp_path / "2026-W19" / "peb.md"
    prior.parent.mkdir(parents=True)
    prior.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 4 – May 10, 2026)\n"
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
        week_iso="2026-W20",
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
    out_dir = tmp_path / "sprints" / "2026-W20"
    out_dir.mkdir(parents=True)
    existing = out_dir / "peb.md"
    existing.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W20\n---\n"
        "# peb — Pebble Foods · Sprint W20 (May 11 – May 17, 2026)\n"
        "<!-- cp-engine:start sprint-facts -->\n"
        "| | |\n|---|---|\n| Stage | Stale |\n"
        "<!-- cp-engine:end sprint-facts -->\n"
        "## Client communication\n### Outbound\n"
        "- [sent · 2026-05-11] Custom hand-written note\n"
    )
    ensure_sprint_file(
        project=_fixture_project(),
        sprint_root=tmp_path / "sprints",
        week_iso="2026-W20",
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
        week_iso="2026-W20", week_label="W19",
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


def test_ensure_sprint_file_counts_meetings_from_project_dir(tmp_path) -> None:
    """The sprint-facts 'Meetings' row reflects the project's meetings/
    dir — `ensure_sprint_file` resolves it from `sprint_root.parent`."""
    from cp_engine.sprints import ensure_sprint_file
    from cp_engine.state import dir_slug

    # Project dir lives at <tenant>/1p/<company-slug>/<dir_slug>/ under
    # the account-nested layout; sprint_root is <tenant>/sprints.
    # _fixture_project keeps company_name "Pebble Foods" → account slug
    # `pebble-foods`, and code `ggl-5136` → dir_slug `ggl-5136` (the dir is
    # the slugified code; the name no longer contributes a tail).
    project = _fixture_project(code="ggl-5136")
    slug = dir_slug(project.code, project.name)
    meetings = tmp_path / "1p" / "pebble-foods" / slug / "meetings"
    meetings.mkdir(parents=True)
    # One meeting inside the W19 window (May 11–17), one outside it.
    (meetings / "2026-05-13-standup.md").write_text("# in window\n")
    (meetings / "2026-05-13-standup.txt").write_text("t\n")
    (meetings / "2026-05-20-later.md").write_text("# out of window\n")

    out = ensure_sprint_file(
        project=project,
        sprint_root=tmp_path / "sprints",
        week_iso="2026-W20", week_label="W19",
        week_start="2026-05-11", week_end="2026-05-17",
        prior_sprint=None, last_sprint_hours_line=None, sessions_this_week=0,
        last_session_date=None, last_session_who=None, last_session_summary=None,
        recent_commits=(), open_issues=(),
    )
    body = out.read_text()
    assert "| Meetings | [1 this sprint]" in body


def test_ensure_sprint_file_omits_meetings_row_when_none(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_file
    out = ensure_sprint_file(
        project=_fixture_project(code="ggl-5136"),
        sprint_root=tmp_path / "sprints",
        week_iso="2026-W20", week_label="W19",
        week_start="2026-05-11", week_end="2026-05-17",
        prior_sprint=None, last_sprint_hours_line=None, sessions_this_week=0,
        last_session_date=None, last_session_who=None, last_session_summary=None,
        recent_commits=(), open_issues=(),
    )
    # No meetings/ dir exists → no row, no dead link.
    assert "| Meetings |" not in out.read_text()


@pytest.mark.parametrize("dt,expected", [
    (datetime(2026, 5, 11, 8, 0), True),  # Monday
    (datetime(2026, 5, 13, 8, 0), True),  # mid-week
    (datetime(2026, 5, 17, 23, 0), True),  # Sunday end
])
def test_is_in_sprint_window(dt: datetime, expected: bool) -> None:
    assert is_in_sprint_window(dt) is expected


def test_current_sprint_week_iso_planning_anchor() -> None:
    """Planning-week rule (v0.8.7.3, matches MC-2's planningWeekMonday):
    Mon/Tue → this week's Monday. Wed-Sun → next week's Monday.

    Week numbering uses ISO 8601 (v0.10.0+). For 2026, ISO weeks happen
    to equal Python's `%W` + 1 across the entire year — see the
    iso-week-cutover design doc in cp/docs/plans.

    Reference week:  2026-05-11 (Mon) → 2026-05-17 (Sun) = W20 (ISO)
                     2026-05-18 (Mon) → 2026-05-24 (Sun) = W21 (ISO)
    """
    # Mon May 11 → W20 (planning current week, this week)
    assert current_sprint_week_iso(datetime(2026, 5, 11)) == "2026-W20"
    # Tue May 12 → W20 (still planning current week)
    assert current_sprint_week_iso(datetime(2026, 5, 12)) == "2026-W20"
    # Wed May 13 → W21 (rolls forward; planning next week)
    assert current_sprint_week_iso(datetime(2026, 5, 13)) == "2026-W21"
    # Thu May 14 → W21
    assert current_sprint_week_iso(datetime(2026, 5, 14)) == "2026-W21"
    # Fri May 15 → W21
    assert current_sprint_week_iso(datetime(2026, 5, 15)) == "2026-W21"
    # Sat May 16 → W21
    assert current_sprint_week_iso(datetime(2026, 5, 16)) == "2026-W21"
    # Sun May 17 → W21
    assert current_sprint_week_iso(datetime(2026, 5, 17)) == "2026-W21"
    # Mon May 18 → W21 (planning current week again, on the Monday it begins)
    assert current_sprint_week_iso(datetime(2026, 5, 18)) == "2026-W21"
    # Tue May 26 → W22 (the date this fix went in)
    assert current_sprint_week_iso(datetime(2026, 5, 26)) == "2026-W22"


def test_prior_sprint_week_iso_planning_anchor() -> None:
    """Prior sprint = the planning Monday minus 7 days. ISO 8601 numbering.

    On Mon/Tue: prior sprint is the *previous* calendar week.
    On Wed-Sun: prior sprint is *this* calendar week (just-closed).
    """
    # Mon May 11 → planning W20, prior W19
    assert prior_sprint_week_iso(datetime(2026, 5, 11)) == "2026-W19"
    # Tue May 12 → planning W20, prior W19
    assert prior_sprint_week_iso(datetime(2026, 5, 12)) == "2026-W19"
    # Wed May 13 → planning W21, prior W20 (the just-closed sprint)
    assert prior_sprint_week_iso(datetime(2026, 5, 13)) == "2026-W20"
    # Sun May 17 → planning W21, prior W20
    assert prior_sprint_week_iso(datetime(2026, 5, 17)) == "2026-W20"


def test_iso_week_handles_year_boundary() -> None:
    """ISO week + ISO year differ from calendar year at the Jan boundary.

    Jan 1 2027 (Friday) belongs to ISO week 53 of 2026. The helper must
    use isocalendar().year (which respects ISO-year), not date.year.
    """
    # Jan 1 2027 is Friday → planning rolls to Mon Jan 4 2027 = ISO 2027-W01
    assert current_sprint_week_iso(datetime(2027, 1, 1)) == "2027-W01"
    # Mon Dec 28 2026 → ISO 2026-W53 (this Monday is still in 2026)
    assert current_sprint_week_iso(datetime(2026, 12, 28)) == "2026-W53"
    # Tue Dec 29 2026 → still W53
    assert current_sprint_week_iso(datetime(2026, 12, 29)) == "2026-W53"


def test_sprint_week_dates_planning_anchor() -> None:
    """Date range covers the planning week, Mon-Sun."""
    # Tue May 12 → planning W19 = May 11–17
    assert sprint_week_dates(datetime(2026, 5, 12)) == ("2026-05-11", "2026-05-17")
    # Wed May 13 → planning W20 = May 18–24 (rolled)
    assert sprint_week_dates(datetime(2026, 5, 13)) == ("2026-05-18", "2026-05-24")


def test_ensure_sprint_files_for_active_projects_writes_one_per_active(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_files_for_active_projects
    proj_active = _fixture_project(code="peb", status="Open")
    proj_holding = _fixture_project(code="apx", status="Holding")
    paths = ensure_sprint_files_for_active_projects(
        active_projects=(proj_active, proj_holding),
        sprint_root=tmp_path / "sprints",
        now=datetime(2026, 5, 13, 8, 0),
        per_project_data={},
    )
    assert any(p.name == "peb.md" for p in paths)
    assert not any(p.name == "apx.md" for p in paths)


def test_ensure_sprint_files_includes_repo_source_active_projects(tmp_path) -> None:
    """Internal workstreams (has_agreement=False, self company) speak the one
    MC vocabulary (#301): Open gets a sprint file, Holding does not, and
    `is_internal` gates nothing. (Historically v0.8.1: repo-source projects
    with the literal "Active" were dropped by an `is_active_status`-only
    check; that second vocabulary is gone.) The fix
    mirrors render.py's `is_active` rule: engagement → is_active_status
    + not internal; repo → status == "Active".
    """
    from cp_engine.sprints import ensure_sprint_files_for_active_projects
    engagement = _fixture_project(code="peb", status="Open")
    fpsf_repo = _fixture_project(
        code="mc-2", status="Open", has_agreement=False,
        is_internal=True, company_kind="self-fpsf",
    )
    canonic_repo = _fixture_project(
        code="storyos", status="Open", has_agreement=False,
        is_internal=True, company_kind="self-canonic",
    )
    inactive_repo = _fixture_project(
        code="lns", status="Holding", has_agreement=False,
        is_internal=True, company_kind="self-fpsf",
    )
    paths = ensure_sprint_files_for_active_projects(
        active_projects=(engagement, fpsf_repo, canonic_repo, inactive_repo),
        sprint_root=tmp_path / "sprints",
        now=datetime(2026, 5, 13, 8, 0),
        per_project_data={},
    )
    written = {p.name for p in paths}
    assert "peb.md" in written
    assert "mc-2.md" in written
    assert "storyos.md" in written
    assert "lns.md" not in written


def test_render_current_sprint_block_emits_top_3_asks_and_risks() -> None:
    from cp_engine.sprints import render_current_sprint_block
    sf = _fixture_sprint_file(  # 4 asks, 2 risks
        asks=("a1", "a2", "a3", "a4"),
        risks=("escalated:r1", "watching:r2"),
        allocation_line="Drew 6h · Tony 2h",
        link="../../sprints/2026-W20/peb.md",
        week_label="W19", dates="May 11 – May 17",
    )
    block = render_current_sprint_block(sf, link_path="../../sprints/2026-W20/peb.md")
    assert "## Current sprint" in block
    assert "[W19 (May 11 – May 17)]" in block
    assert block.count("\n- ") >= 5  # 3 asks + 2 risks
    assert "a4" not in block  # truncated to top 3


def test_render_sprint_index_lists_each_active_project_with_counts() -> None:
    from cp_engine.sprints import render_sprint_index
    sf_peb = _fixture_sprint_file(
        project_code="peb",
        asks=("a1", "a2"),
        risks=("escalated:r1",),
    )
    sf_orb = _fixture_sprint_file(
        project_code="orb",
        asks=("a1",),
        risks=("watching:r1",),
    )
    body = render_sprint_index(
        week_iso="2026-W20",
        week_dates="May 11 – May 17",
        sprint_files=[sf_peb, sf_orb],
    )
    assert "# Sprint W20 (May 11 – May 17)" in body
    assert "| `peb` |" in body
    assert "| `orb` |" in body


# ──────────────────────────────────────────────────────────────────────
#  v0.8.5 — new parsers (stakeholders, structured decisions, themes)
# ──────────────────────────────────────────────────────────────────────


def test_parse_stakeholders_happy_path() -> None:
    from cp_engine.sprints import _parse_stakeholders
    body = """
## Client communication

### Inbound
- _none_

### Stakeholders
- [Rena Ramos · Director · primary client decision-maker]
- [Carla Smith · PM · day-to-day coordination]
- malformed-line-no-brackets
- [Maria Mraz] (just a name, no role/context)
"""
    out = _parse_stakeholders(body)
    assert len(out) == 3
    assert out[0].name == "Rena Ramos"
    assert out[0].role == "Director"
    assert out[0].context == "primary client decision-maker"
    assert out[1].name == "Carla Smith"
    assert out[2].name == "Maria Mraz"
    assert out[2].role is None
    assert out[2].context is None


def test_parse_stakeholders_no_section_returns_empty() -> None:
    from cp_engine.sprints import _parse_stakeholders
    body = "## Client communication\n\n### Outbound\n- foo"
    assert _parse_stakeholders(body) == ()


def test_parse_decisions_handles_bracketed_format_and_cross_cutting_flag() -> None:
    from cp_engine.sprints import _parse_decisions
    body = """
## Meeting notes & decisions

### Decisions

- [decision · 2026-05-12] Marcello drafts 5 decks Tuesday
- [decision · 2026-05-12][cross-cutting] Drop Claude team plan; move to individual Max plans
- [decision · 2026-05-12][cross-cutting]Marcello hours triage: drop website work this sprint
- 1. Legacy freeform decision (gets ignored by this parser)
"""
    out = _parse_decisions(body)
    assert len(out) == 3
    assert out[0].text == "Marcello drafts 5 decks Tuesday"
    assert out[0].cross_cutting is False
    assert out[1].cross_cutting is True
    assert out[1].text.startswith("Drop Claude team plan")
    assert out[2].cross_cutting is True


def test_parse_themes_from_week_file_happy_path(tmp_path: Path) -> None:
    from cp_engine.sprints import parse_themes_from_week_file
    p = tmp_path / "_week.md"
    p.write_text("""
## Themes

- [theme · 2026-05-12] Maria transition; activation pop-up Round 3
- [theme · 2026-05-13] Infoblox AI workshop downsized
- malformed line
- [theme · 2026-05-14]
""")
    out = parse_themes_from_week_file(p)
    assert len(out) == 2
    assert out[0].text.startswith("Maria transition")
    assert out[0].date == "2026-05-12"


def test_parse_themes_from_week_file_returns_empty_when_missing(tmp_path: Path) -> None:
    from cp_engine.sprints import parse_themes_from_week_file
    out = parse_themes_from_week_file(tmp_path / "missing.md")
    assert out == ()


# ── count_sprint_meetings ──────────────────────────────────────────
# Counts per-meeting artifact .md files (written by the deeper-transcripts
# pipeline) whose YYYY-MM-DD filename prefix falls within a sprint window.
# Drives the "Meetings" row in the sprint file's sprint-facts region.


def _make_meetings_dir(tmp_path: Path, dated_slugs: list[str]) -> Path:
    """Create a meetings/ dir with `<date>-<slug>.md` + .txt pairs."""
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    for name in dated_slugs:
        (meetings / f"{name}.md").write_text("# artifact\n")
        (meetings / f"{name}.txt").write_text("transcript\n")
    return meetings


def test_count_sprint_meetings_counts_only_in_window(tmp_path: Path) -> None:
    from cp_engine.sprints import count_sprint_meetings
    _make_meetings_dir(
        tmp_path,
        [
            "2026-05-17-before-window",   # day before window
            "2026-05-18-monday-edge",     # window start (inclusive)
            "2026-05-21-mid-week",        # inside
            "2026-05-24-sunday-edge",     # window end (inclusive)
            "2026-05-25-after-window",    # day after window
        ],
    )
    n = count_sprint_meetings(
        tmp_path / "meetings", week_start="2026-05-18", week_end="2026-05-24"
    )
    assert n == 3


def test_count_sprint_meetings_returns_zero_when_dir_absent(tmp_path: Path) -> None:
    from cp_engine.sprints import count_sprint_meetings
    n = count_sprint_meetings(
        tmp_path / "meetings", week_start="2026-05-18", week_end="2026-05-24"
    )
    assert n == 0


def test_count_sprint_meetings_ignores_txt_and_unparseable_names(
    tmp_path: Path,
) -> None:
    from cp_engine.sprints import count_sprint_meetings
    meetings = _make_meetings_dir(tmp_path, ["2026-05-21-real-meeting"])
    # A .md whose name has no parseable date prefix — must not crash or count.
    (meetings / "notes.md").write_text("# stray\n")
    (meetings / "README.md").write_text("# readme\n")
    n = count_sprint_meetings(
        meetings, week_start="2026-05-18", week_end="2026-05-24"
    )
    # Only the one real dated .md counts; .txt siblings and stray .md ignored.
    assert n == 1


# ── "Meetings" row in the sprint-facts region ──────────────────────


def _scaffold_project() -> ProjectState:
    # Real-shaped code (`ggl-5136`) so dir_slug → `ggl-5136` (dir is the
    # slugified code; the name no longer contributes a tail).
    return ProjectState(
        code="ggl-5136",
        name="Go Safety",
        has_agreement=True,
        company_kind="client",
        company_code="GGL",
        company_name="Google",
        status="Deal",
        is_internal=False,
        owner="Drew",
        last_touched=None,
        deadline=None,
        deal_stage="Negotiation",
        budget=45000.0,
    )


def _render_with_meetings(count: int) -> str:
    return render_sprint_scaffold(
        project=_scaffold_project(),
        week_iso="2026-W20",
        week_label="W19",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint="2026-W19",
        last_sprint_hours_line="Drew 6.5h",
        sessions_this_week=3,
        last_session_date=None,
        last_session_who=None,
        last_session_summary=None,
        recent_commits=(),
        open_issues=(),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        meetings_this_sprint=count,
    )


def test_sprint_facts_shows_meetings_row_when_count_positive() -> None:
    body = _render_with_meetings(2)
    # The row links to the project's meetings/ dir, relative from the
    # sprint file at sprints/<week>/<code>.md.
    assert "| Meetings |" in body
    # Account-nested layout: client projects live at 1p/<company>/<dir>/,
    # so the link from the sprint file walks up two and back down through
    # the account dir. _scaffold_project's company_name is "Google"; the
    # dir is the slugified code (`ggl-5136`), name no longer appended.
    assert "[2 this sprint](../../1p/google/ggl-5136/meetings/)" in body


def test_sprint_facts_omits_meetings_row_when_count_zero() -> None:
    body = _render_with_meetings(0)
    assert "| Meetings |" not in body


def test_sprint_facts_meetings_row_singular_when_one() -> None:
    body = _render_with_meetings(1)
    assert "[1 this sprint]" in body


# ──────────────────────────────────────────────────────────────────────
#  scaffold_from_prior — Phase 1 of v0.13.0 auto-ingest resilience
# ──────────────────────────────────────────────────────────────────────


def _write_prior_sprint_file(
    *,
    sprints_root: Path,
    week_iso: str,
    project_code: str,
    project_name: str = "Pebble Foods",
    scope_path: str = "1p/pebble/peb-5100-activation",
    cp_link_text: str = "Project CP",
    extra_body: str = "",
) -> Path:
    """Write a minimal but parseable prior sprint file for scaffold_from_prior tests."""
    path = sprints_root / week_iso / f"{project_code}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nProject: {project_code} — {project_name}\n"
        f"Sprint: {week_iso}\n---\n\n"
        f"# {project_code} — {project_name} · Sprint W{week_iso.split('-W')[1]} (May 18 – May 24, 2026)\n\n"
        f"← [{cp_link_text}](../../{scope_path}/cp.md) · "
        f"[Master](../../master-cp.md) · [Prior sprint](../2026-W18/{project_code}.md)\n\n"
        "<!-- cp-engine:start sprint-facts -->\n| | |\n|---|---|\n"
        "| Stage | — |\n| Owner | Drew |\n| Budget | — |\n"
        "<!-- cp-engine:end sprint-facts -->\n\n"
        "<!-- cp-engine:start where-it-stands -->\n## Where it stands\n\nrolling\n"
        "<!-- cp-engine:end where-it-stands -->\n\n"
        "<!-- cp-engine:start carry-forward -->\n## Carried over from prior\n\n"
        "<!-- cp-engine:end carry-forward -->\n\n"
        "## Client communication\n### Open asks\n"
        f"{extra_body}"
    )
    return path


def test_scaffold_from_prior_creates_new_week_from_prior(tmp_path: Path) -> None:
    """When a prior sprint file exists, scaffold the target week from it."""
    from cp_engine.sprints import scaffold_from_prior

    _write_prior_sprint_file(
        sprints_root=tmp_path / "sprints",
        week_iso="2026-W22",
        project_code="peb-5100",
        extra_body=(
            "- [open · 2026-05-25 · Drew] Carry me forward "
            "<!-- cp:hash=aaaaaaaa -->\n"
        ),
    )

    target = tmp_path / "sprints" / "2026-W23" / "peb-5100.md"
    assert not target.exists()

    result = scaffold_from_prior(
        tenant_root=tmp_path,
        project_code="peb-5100",
        target_week_iso="2026-W23",
    )

    assert result == target
    assert target.exists()
    body = target.read_text()
    assert "2026-W23" in body
    # The new file should round-trip through the parser.
    sf = parse_sprint_file(target)
    assert sf.project_code == "peb-5100"
    assert sf.week_iso == "2026-W23"
    # Carry-forward should reflect the prior week.
    assert sf.prior_sprint == "2026-W22"


def test_scaffold_from_prior_returns_none_when_no_prior(tmp_path: Path) -> None:
    """No prior sprint file for the project — return None, don't crash."""
    from cp_engine.sprints import scaffold_from_prior

    result = scaffold_from_prior(
        tenant_root=tmp_path,
        project_code="peb-5100",
        target_week_iso="2026-W23",
    )
    assert result is None


def test_scaffold_from_prior_finds_most_recent_prior(tmp_path: Path) -> None:
    """When MULTIPLE prior weeks exist, pick the most recent one."""
    from cp_engine.sprints import scaffold_from_prior

    sprints_root = tmp_path / "sprints"
    for week in ("2026-W19", "2026-W21", "2026-W22"):
        _write_prior_sprint_file(
            sprints_root=sprints_root,
            week_iso=week,
            project_code="peb-5100",
        )

    scaffold_from_prior(
        tenant_root=tmp_path,
        project_code="peb-5100",
        target_week_iso="2026-W23",
    )

    body = (sprints_root / "2026-W23" / "peb-5100.md").read_text()
    # Carry-forward should reference W22, the most recent prior — not W19 or W21.
    sf = parse_sprint_file(sprints_root / "2026-W23" / "peb-5100.md")
    assert sf.prior_sprint == "2026-W22"


def test_scaffold_from_prior_handles_initiative_source(tmp_path: Path) -> None:
    """Initiative sprint files use the 'Initiative CP' link variant + a slimmer
    template. scaffold_from_prior must detect the source and pick the right
    template; otherwise the new file ends up with a Client communication
    section a real initiative file would not have."""
    from cp_engine.sprints import scaffold_from_prior

    _write_prior_sprint_file(
        sprints_root=tmp_path / "sprints",
        week_iso="2026-W22",
        project_code="first-person-operations",
        project_name="First Person Operations",
        scope_path="firstpersonsf/first-person-operations",
        cp_link_text="Initiative CP",
    )

    result = scaffold_from_prior(
        tenant_root=tmp_path,
        project_code="first-person-operations",
        target_week_iso="2026-W23",
    )

    assert result is not None
    body = result.read_text()
    # Initiative template uses "Team communication", not "Client communication".
    assert "## Team communication" in body
    assert "## Client communication" not in body


# ---------------------------------------------------------------------------
# section_body lenient-heading regression tests
# ---------------------------------------------------------------------------
#
# Real sprint scaffolds carry suffixes after the bare title (e.g.
# ``## Horizon — 4–8 weeks out``). Prior to v0.15, section_body required
# an exact ``## <heading>\s*$`` match, which silently returned "" on real
# files and caused agenda._extract_decisions_due_for_project to drop every
# decisions-due bullet. These tests pin the lenient matcher.


def test_section_body_exact_heading():
    body = "## Horizon\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def test_section_body_em_dash_suffix():
    body = "## Horizon — 4–8 weeks out\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def test_section_body_en_dash_suffix():
    body = "## Horizon – rolling outlook\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def test_section_body_hyphen_suffix():
    body = "## Horizon - rolling outlook\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def test_section_body_colon_suffix():
    body = "## Horizon: rolling outlook\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def test_section_body_multi_word_heading_with_suffix():
    body = (
        "## Dependencies & risks — rolling\nbody line\n\n## Next\nignored\n"
    )
    assert section_body(body, "Dependencies & risks").strip() == "body line"


def test_section_body_no_match():
    body = "## Other\nbody line\n"
    assert section_body(body, "Horizon") == ""


def test_section_body_stops_at_next_h2():
    body = (
        "## Horizon — 4–8 weeks out\n"
        "first\n"
        "second\n"
        "## Next section\n"
        "should not appear\n"
    )
    out = section_body(body, "Horizon")
    assert "first" in out
    assert "second" in out
    assert "should not appear" not in out


def test_section_body_prefix_match_not_greedy():
    """``Horizon`` must not match ``## Horizons & roadmap`` (different word)."""
    body = "## Horizons & roadmap\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon") == ""


# ──────────────────────────────────────────────────────────────────────
#  Scaffold placeholders are HTML comments (invisible rendered, zero
#  ghost content when parsed) — the section HEADERS stay the ingest /
#  deepen contract surface.
# ──────────────────────────────────────────────────────────────────────


_ENGAGEMENT_SECTION_HEADERS = (
    "## Client communication",
    "### Outbound",
    "### Open asks",
    "### Inbound",
    "### Slack digest",
    "### Stakeholders",
    "## Dependencies & risks",
    "## This sprint",
    "### Deliverables",
    "### Definition of done",
    "## Horizon — 4–8 weeks out",
    "### Milestones",
    "### Decisions due",
    "### Opportunities",
    "## Meeting notes & decisions",
    "### Decisions",
    "### Discussion notes",
)


def test_scaffold_placeholders_are_html_comments() -> None:
    body = _render_with_meetings(0)
    # No visible `- _<...>_` / `_<...>_` placeholder text anywhere.
    assert "_<" not in body
    # Guidance survives as HTML comments instead.
    assert "<!-- <message — `[status · date]` prefix> -->" in body
    assert "<!-- <risk — `[severity · category · date]` prefix> -->" in body
    # The header contract auto-ingest + deepen target is unchanged.
    for heading in _ENGAGEMENT_SECTION_HEADERS:
        assert heading in body, f"missing scaffold heading: {heading}"


def test_initiative_scaffold_placeholders_are_html_comments() -> None:
    project = ProjectState(
        code="mission-control",
        name="Mission Control",
        has_agreement=False,
        company_kind="self-fpsf",
        company_code="1PI",
        company_name="First Person",
        status="Active",
        is_internal=True,
        owner="Tony",
        last_touched=None,
        deadline=None,
    )
    body = render_sprint_scaffold(
        project=project,
        week_iso="2026-W20",
        week_label="W20",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint=None,
        last_sprint_hours_line=None,
        sessions_this_week=0,
        last_session_date=None,
        last_session_who=None,
        last_session_summary=None,
        recent_commits=(),
        open_issues=(),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        meetings_this_sprint=0,
    )
    assert "_<" not in body
    assert "## Team communication" in body
    assert "### Open asks" in body
    assert "### Slack digest" in body


def test_fresh_scaffold_parses_with_no_ghost_content(tmp_path: Path) -> None:
    """A just-scaffolded file must parse as EMPTY — comment placeholders
    must not surface as outbound/asks/deliverables/dod ghost entries the
    way the old `- _<...>_` italic bullets could."""
    path = tmp_path / "sprints" / "2026-W20" / "ggl-5136.md"
    path.parent.mkdir(parents=True)
    path.write_text(_render_with_meetings(0))
    sf = parse_sprint_file(path)
    assert sf.client_outbound == ()
    assert sf.client_open_asks == ()
    assert sf.client_inbound == ()
    assert sf.risks == ()
    assert sf.horizon == ()
    assert sf.deliverables == ()
    assert sf.allocation == ()
    assert sf.definition_of_done == ""
    assert sf.decisions == ()


# --- carry-forward risks reach the dashboard surfaces -----------------------
#
# storyos rendered "Active risks (0)" while carrying seven risks, one of them
# ESCALATED. Sync writes a prior week's unresolved risks into the
# engine-managed `carry-forward` region; the strip read only the hand-written
# `## Dependencies & risks` section, so a CARRIED risk silently stopped
# counting — backwards, since surviving a week makes a risk more notable.


def _risk(text, severity="watching", category="delivery", raised="2026-08-18"):
    from cp_engine.sprints import Risk

    return Risk(
        text=text,
        severity=severity,
        category=category,
        raised_date=raised,
    )


class _FakeSF:
    """Minimal stand-in carrying just the fields _active_risks reads."""

    def __init__(self, risks=(), carried=()):
        from cp_engine.sprints import CarryForward

        self.risks = list(risks)
        self.carry_forward = CarryForward(asks=(), risks=tuple(carried), horizon=())


def test_carried_risks_count_as_active():
    from cp_engine.sprints import _active_risks

    sf = _FakeSF(risks=(), carried=(_risk("Deal funds 2 contractors, not 4",
                                          severity="escalated"),))
    active = _active_risks(sf)
    assert len(active) == 1
    assert active[0].severity == "escalated"


def test_live_and_carried_risks_combine():
    from cp_engine.sprints import _active_risks

    sf = _FakeSF(
        risks=(_risk("Client bandwidth"),),
        carried=(_risk("Regional site access"),),
    )
    assert len(_active_risks(sf)) == 2


def test_duplicate_risk_counted_once():
    """A risk both carried forward AND restated by hand this week is ONE risk."""
    from cp_engine.sprints import _active_risks

    same = "Rob Katzenstein stuck — carried from W33."
    sf = _FakeSF(risks=(_risk(same),), carried=(_risk(same),))
    assert len(_active_risks(sf)) == 1


def test_escalated_sorts_ahead_of_watching():
    """The strip previews only 3 — an escalation must never be crowded out."""
    from cp_engine.sprints import _active_risks

    sf = _FakeSF(
        risks=(_risk("w1"), _risk("w2"), _risk("w3")),
        carried=(_risk("the escalated one", severity="escalated"),),
    )
    active = _active_risks(sf)
    assert active[0].severity == "escalated"
    assert active[0].text == "the escalated one"
    # ...and it survives the 3-item preview slice the renderer applies.
    assert any(r.severity == "escalated" for r in active[:3])


def test_dependency_severity_still_excluded():
    """Only escalated + watching are dashboard-worthy; carry-forward is not a
    backdoor for informational dependency risks."""
    from cp_engine.sprints import _active_risks

    sf = _FakeSF(carried=(_risk("informational", severity="dependency"),))
    assert _active_risks(sf) == []


# ──────────────────────────────────────────────────────────────────────
#  #279 — a wrapped bullet is one bullet, not its first line
# ──────────────────────────────────────────────────────────────────────

_WRAPPED = """---
Project: ibx-5153 — Test
Filename: sprints/2026-W38/ibx-5153.md
Sprint: 2026-W38
PriorSprint: 2026-W37
---

# ibx-5153 · Sprint 2026-W38

## Client communication

### Open asks

- [open · 2026-09-03 · Jaime Mehra · by 2026-09-17] **A positive-frame counterpart
  to "Your AI Can't"** set beside the negative lead so he can judge what the
  positive frame loses.

### Inbound

- [2026-09-11 · source ingest] **Marcello's working record ingested** — the
  Legends Room master file: three territories and the static-ad rule.
"""


def _wrapped(tmp_path):
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    p = d / "ibx-5153.md"
    p.write_text(_WRAPPED)
    return p


def test_a_wrapped_open_ask_keeps_its_whole_text(tmp_path):
    """THE #279 CONTROL — the defect that shipped truncations forward.

    `bullets()` always returned (first_line, continuation); the open-asks
    parser bound the continuation to `_cont` and discarded it. So a
    hand-written ask that wrapped was stored as its first line — and
    carry-forward then rendered THAT into the next week's file, faithfully,
    because the template writes `a.text` and the text it got was already short.

    Two ibx-5153 asks propagated as `**A positive-frame counterpart` for weeks.
    A truncated bullet still looks like a bullet, so nobody saw it.
    """
    from cp_engine.sprints import parse_sprint_file

    sf = parse_sprint_file(_wrapped(tmp_path))
    (ask,) = sf.client_open_asks
    assert "positive frame loses" in ask.text, "continuation was dropped"
    assert len(ask.text) > 100


def test_a_wrapped_inbound_keeps_its_whole_text(tmp_path):
    from cp_engine.sprints import parse_sprint_file

    sf = parse_sprint_file(_wrapped(tmp_path))
    (inb,) = sf.client_inbound
    assert "static-ad rule" in inb.text


def test_an_unwrapped_bullet_is_unchanged(tmp_path):
    """The common case must not gain a trailing space or lose its shape."""
    from cp_engine.sprints import parse_sprint_file

    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    p = d / "x.md"
    p.write_text(
        _WRAPPED.replace(
            '''- [open · 2026-09-03 · Jaime Mehra · by 2026-09-17] **A positive-frame counterpart
  to "Your AI Can't"** set beside the negative lead so he can judge what the
  positive frame loses.''',
            "- [open · 2026-09-03 · Jaime Mehra] A single-line ask.",
        )
    )
    sf = parse_sprint_file(p)
    (ask,) = sf.client_open_asks
    assert ask.text == "A single-line ask."


def test_the_join_is_a_space_not_a_newline():
    """Line breaks in a wrapped bullet are typographic, not semantic.

    Preserving them would re-render someone's editor width into the next
    week's file.
    """
    from cp_engine.sprints import _join_bullet

    assert _join_bullet("first", "second\nthird") == "first second third"
    assert _join_bullet("only", "") == "only"


def test_a_risks_why_it_matters_does_not_also_land_in_text(tmp_path):
    """A structured continuation belongs to its own field, not to both.

    `Why it matters:` has a column. Gluing it onto `text` as well renders it
    twice in carry-forward — caught by the golden fixture, which was right and
    stayed unchanged.
    """
    from cp_engine.sprints import parse_sprint_file

    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    p = d / "x.md"
    p.write_text(
        "---\nProject: x — T\nFilename: sprints/2026-W38/x.md\n"
        "Sprint: 2026-W38\nPriorSprint: \n---\n\n# x\n\n"
        "## Dependencies & risks\n\n"
        "- [risk · escalated · contract · 2026-05-04] Legal turnaround may slip\n"
        "  Why it matters: pushes contract into next sprint.\n"
    )
    (risk,) = parse_sprint_file(p).risks
    assert risk.text == "Legal turnaround may slip"
    assert risk.why_it_matters == "pushes contract into next sprint."


def test_a_risks_wrapped_prose_still_joins(tmp_path):
    """Ordinary wrapping is not a structured field and must be kept."""
    from cp_engine.sprints import parse_sprint_file

    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    p = d / "y.md"
    p.write_text(
        "---\nProject: y — T\nFilename: sprints/2026-W38/y.md\n"
        "Sprint: 2026-W38\nPriorSprint: \n---\n\n# y\n\n"
        "## Dependencies & risks\n\n"
        "- [risk · watching · scope · 2026-05-04] The client team keeps\n"
        "  re-briefing on strategy already approved.\n"
    )
    (risk,) = parse_sprint_file(p).risks
    assert "re-briefing on strategy" in risk.text
    assert risk.why_it_matters is None


# ──────────────────────────────────────────────────────────────────────
#  #320 — parsers silently dropping or inventing hand-written content
# ──────────────────────────────────────────────────────────────────────


def test_bare_date_decision_reaches_recent_decisions_strip() -> None:
    """A `### Decisions` bullet written `- [2026-08-18] …` — no `decision · `
    prefix — was dropped by `_parse_decisions`, so five real slt-5196
    decisions rendered as "No structured decisions captured in the last 4
    weeks" while sync exited 0 (#320). #272 fixed only the backticked form.
    Asserted through `aggregate_project_strips`, the path that feeds
    `recent-decisions-strip`, not just the parser."""
    from datetime import date as _date

    from cp_engine.aggregators import aggregate_project_strips
    from cp_engine.sprints import _parse_decisions

    body = """
## Meeting notes & decisions

### Decisions

- [2026-08-18] Shoot stays on Oct 8–9
- `[2026-08-19]` Legal reviews releases before casting
- [2026-08-20 · sprint planning][cross-cutting] Tony owns the narrative pass
- [decision · 2026-08-21] Prefixed form still parses
- [not a date] freeform bracket bullet is left alone
"""
    decs = _parse_decisions(body)
    assert [(d.date, d.text, d.cross_cutting) for d in decs] == [
        ("2026-08-18", "Shoot stays on Oct 8–9", False),
        ("2026-08-19", "Legal reviews releases before casting", False),
        ("2026-08-20", "Tony owns the narrative pass", True),
        ("2026-08-21", "Prefixed form still parses", False),
    ]

    class _SF:
        project_code = "slt-5196"
        week_start = _date(2026, 8, 17)
        client_inbound = ()
        decisions = decs
        client_open_asks = ()
        carry_forward = CarryForward(asks=(), risks=(), horizon=())
        stakeholders = ()

    strips = aggregate_project_strips("slt-5196", (_SF(),), _date(2026, 8, 25))
    assert len(strips.recent_decisions) == 4


def test_allocation_html_comment_is_not_a_person() -> None:
    """An HTML comment in the Allocation slot was parsed as data: the note
    `<!-- W35 was Marcello · 16h -->` became a person named "W35 was
    Marcello" booked for 16h in master-cp.md's roster (#320). The scaffold
    itself ships the slot with a comment, so the comment text here is taken
    from the real template line and then filled the way an author would."""
    from cp_engine.sprints import _parse_this_sprint

    template = (
        Path(__file__).resolve().parents[1]
        / "src/cp_engine/templates/sprint-cp.md.j2"
    ).read_text(encoding="utf-8")
    scaffold_line = next(
        ln for ln in template.splitlines() if ln.startswith("**Allocation:**")
    )
    assert "<!--" in scaffold_line  # the premise: the scaffold teaches it
    annotated = scaffold_line.replace(
        "<!--", "Tony · 16h <!-- W35 was Marcello · 16h ·", 1
    )
    body = f"## This sprint\n{annotated}\n\n### Deliverables\n1. x\n"
    alloc, _deliv, _dod = _parse_this_sprint(body)
    assert alloc == (PersonHours(person_name="Tony", hours=16.0),)


def _cf_kwargs(tmp_path, week_iso, prior):
    return dict(
        project=_fixture_project(),
        sprint_root=tmp_path / "sprints",
        week_iso=week_iso, week_label="W", week_start="2026-05-11",
        week_end="2026-05-17", prior_sprint=prior, last_sprint_hours_line=None,
        sessions_this_week=0, last_session_date=None, last_session_who=None,
        last_session_summary=None, recent_commits=(), open_issues=(),
    )


def _cf_setup(tmp_path):
    """A prior week (W19) with one open ask and one escalated risk written in
    its hand-written sections, and a current week (W20) rendered from it."""
    from cp_engine.sprints import ensure_sprint_file

    prior = ensure_sprint_file(**_cf_kwargs(tmp_path, "2026-W19", None))
    body = prior.read_text()
    body = body.replace(
        "### Open asks\n",
        "### Open asks\n- [open · 2026-05-05 · Rena] Approve the Round 3 pop-up copy\n",
        1,
    ).replace(
        "## Dependencies & risks\n",
        "## Dependencies & risks\n\n- [escalated · budget · 2026-05-06] Fee "
        "increase not yet signed off\n",
        1,
    )
    prior.write_text(body)
    cur = ensure_sprint_file(**_cf_kwargs(tmp_path, "2026-W20", "2026-W19"))
    assert "Approve the Round 3 pop-up copy" in cur.read_text()  # premise
    return prior, cur


def test_render_warns_when_hand_edit_inside_carry_forward_is_discarded(
    tmp_path, caplog
) -> None:
    """A fact written by hand between the `carry-forward` markers was silently
    discarded by the next render (#320; tenant commit fda0b4c8 lost Morgan
    Wright's PTO milestone and an annotation on a standing ask this way).
    Render must still discard it — the region is derived — but must SAY so,
    naming the file, the region and the line, and point at the owning week."""
    import logging

    from cp_engine.sprints import ensure_sprint_file

    _prior, cur = _cf_setup(tmp_path)
    rendered = cur.read_text()
    end = "<!-- cp-engine:end carry-forward -->"
    cur.write_text(rendered.replace(
        end, "- [milestone · 2026-09-08] Morgan on PTO 09-08 → 09-17\n" + end, 1
    ))
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        ensure_sprint_file(**_cf_kwargs(tmp_path, "2026-W20", "2026-W19"))
    assert cur.read_text() == rendered  # warn only: what render writes is unchanged
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(msgs) == 1, msgs
    assert "sprints/2026-W20/peb.md" in msgs[0]
    assert "carry-forward" in msgs[0]
    assert "Morgan on PTO" in msgs[0]
    assert "sprints/2026-W19/peb.md" in msgs[0]


def test_render_warns_on_hand_annotation_of_a_carried_row(tmp_path, caplog) -> None:
    """Annotating an existing carried row is a hand edit too — its cp:hash
    and bracket still match the source, so the check is on the item TEXT."""
    import logging

    from cp_engine.sprints import ensure_sprint_file

    _prior, cur = _cf_setup(tmp_path)
    cur.write_text(cur.read_text().replace(
        "Approve the Round 3 pop-up copy",
        "Approve the Round 3 pop-up copy — **now urgent, Rena out Friday**", 1,
    ))
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        ensure_sprint_file(**_cf_kwargs(tmp_path, "2026-W20", "2026-W19"))
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(msgs) == 1 and "now urgent" in msgs[0], msgs


def test_render_is_quiet_when_carry_forward_changes_from_data_drift(
    tmp_path, caplog
) -> None:
    """The region changes on ordinary renders — an ask closed or a risk
    resolved in the owning week drops out. That is data drift, not a hand
    edit, and a warning here would train the reader to ignore the real one."""
    import logging

    from cp_engine.sprints import ensure_sprint_file

    prior, cur = _cf_setup(tmp_path)
    before = cur.read_text()
    prior.write_text(
        prior.read_text()
        .replace("[open · 2026-05-05", "[closed · 2026-05-05", 1)
        .replace("[escalated · budget", "[resolved · budget", 1)
    )
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        ensure_sprint_file(**_cf_kwargs(tmp_path, "2026-W20", "2026-W19"))
        ensure_sprint_file(**_cf_kwargs(tmp_path, "2026-W20", "2026-W19"))
    after = cur.read_text()
    assert after != before  # premise: the region really did change
    assert "Approve the Round 3 pop-up copy" not in after
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ──────────────────────────────────────────────────────────────────────
#  #326 — open asks carry until resolved, not for one week
# ──────────────────────────────────────────────────────────────────────

_ASK_326 = (
    "- [open · 2026-09-08 · Rena] Approve the Round 3 pop-up copy "
    "<!-- cp:hash=3260a5c1 -->\n"
)


def _week(tmp_path, week_iso, prior, *, own_asks=""):
    """Render one peb sprint week, then write `own_asks` into its hand-written
    `### Open asks` — the way an ask is really raised or restated."""
    from cp_engine.sprints import ensure_sprint_file

    path = ensure_sprint_file(**_cf_kwargs(tmp_path, week_iso, prior))
    if own_asks:
        path.write_text(
            path.read_text().replace("### Open asks\n", "### Open asks\n" + own_asks, 1)
        )
    return path


def _carried_texts(path) -> list[str]:
    from cp_engine.sprints import parse_sprint_file

    return [a.text for a in parse_sprint_file(path).carry_forward.asks]


def test_unanswered_ask_still_carried_two_weeks_later(tmp_path) -> None:
    """An ask raised in W37 and never answered was carried into W38 and then
    vanished from W39: carry-forward read only the prior week's OWN asks, and
    W38 had carried it, not owned it (#326). Nothing resolved it, nothing
    warned. It must still be carried in W39, dated when it was first raised."""
    from cp_engine.sprints import compute_carry_forward, parse_sprint_file

    _week(tmp_path, "2026-W37", None, own_asks=_ASK_326)
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert any("Round 3 pop-up copy" in t for t in _carried_texts(w38))  # premise
    carried = [a for a in parse_sprint_file(w39).carry_forward.asks
               if "Round 3 pop-up copy" in a.text]
    assert len(carried) == 1
    assert carried[0].asked_date == "2026-09-08"  # age stays legible
    assert "[ask · 2026-09-08 · Rena]" in w39.read_text()
    # compute_carry_forward is the one derivation, for ingest's scaffold too.
    assert [a.text for a in compute_carry_forward(w39).asks] == [carried[0].text]


def test_ask_resolved_in_w38_is_gone_from_w39(tmp_path) -> None:
    """Resolution stops the carry: restated `closed` in W38's own section, the
    ask must not come back in W39 from W37's older open copy."""
    _week(tmp_path, "2026-W37", None, own_asks=_ASK_326)
    _week(tmp_path, "2026-W38", "2026-W37",
          own_asks=_ASK_326.replace("[open ·", "[closed ·"))
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert not any("Round 3 pop-up copy" in t for t in _carried_texts(w39))


def test_ask_closed_at_origin_stops_carrying_despite_stale_projection(tmp_path) -> None:
    """Every close is written at the ask's ORIGIN (`_origin_sprint_path`), and
    past weeks are never re-rendered, so W38's carry-forward region still says
    open after W37 closes it. Reading that frozen projection would carry a
    resolved ask forever; the owning file's word must win."""
    w37 = _week(tmp_path, "2026-W37", None, own_asks=_ASK_326)
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    w37.write_text(w37.read_text().replace("[open · 2026-09-08", "[closed · 2026-09-08", 1))
    assert "Round 3 pop-up copy" in w38.read_text()  # premise: stale projection
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert not any("Round 3 pop-up copy" in t for t in _carried_texts(w39))


def test_restated_ask_keeps_first_raised_date_and_carries_once(tmp_path) -> None:
    """Restated in a later week, the ask carries ONCE (deduped by cp:hash) with
    the newest wording but the date it was first raised."""
    from cp_engine.sprints import parse_sprint_file

    _week(tmp_path, "2026-W37", None, own_asks=_ASK_326)
    _week(tmp_path, "2026-W38", "2026-W37", own_asks=_ASK_326.replace(
        "2026-09-08 · Rena] Approve", "2026-09-15 · Rena] Approve (v2)"))
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    carried = parse_sprint_file(w39).carry_forward.asks
    assert len(carried) == 1
    assert "(v2)" in carried[0].text and carried[0].asked_date == "2026-09-08"


def test_snoozed_ask_still_carries_across_weeks(tmp_path) -> None:
    """Snooze is about escalation, not state (see `snooze`): a snoozed ask is
    still open and keeps carrying, marker and all."""
    _week(tmp_path, "2026-W37", None, own_asks=_ASK_326.replace(
        "copy <!--", "copy <!-- cp:snoozed-until=2026-10-01 --> <!--"))
    _week(tmp_path, "2026-W38", "2026-W37")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    carried = [t for t in _carried_texts(w39) if "Round 3 pop-up copy" in t]
    assert carried and "cp:snoozed-until=2026-10-01" in carried[0]


def test_open_client_asks_honours_a_close_in_this_weeks_own_section(tmp_path) -> None:
    """The count must agree with next week's carry-forward: an ask carried in
    but restated `closed` in this week's own section is not open."""
    from cp_engine.aggregators import open_client_asks
    from cp_engine.sprints import parse_sprint_file

    _week(tmp_path, "2026-W37", None, own_asks=_ASK_326)
    w38 = _week(tmp_path, "2026-W38", "2026-W37",
                own_asks=_ASK_326.replace("[open ·", "[closed ·"))
    sf = parse_sprint_file(w38)
    assert sf.carry_forward.asks  # premise: carried in from W37
    assert open_client_asks(sf) == []


def test_render_quiet_when_multi_week_ask_drops_out(tmp_path, caplog) -> None:
    """#320's discard warning looks for a dropped row's text in the files the
    region is derived from. Once asks carry from ANY earlier week, a row whose
    origin is two weeks back — and absent from last week's file, as it is in
    every file rendered before #326 — must still count as derived, or closing
    it would read as a hand edit being lost."""
    import logging

    w37 = _week(tmp_path, "2026-W37", None, own_asks=_ASK_326)
    _week(tmp_path, "2026-W38", None)  # rendered without it (pre-#326 shape)
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert "Round 3 pop-up copy" in w39.read_text()  # premise
    w37.write_text(w37.read_text().replace("[open · 2026-09-08", "[closed · 2026-09-08", 1))
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert "Round 3 pop-up copy" not in w39.read_text()
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# ──────────────────────────────────────────────────────────────────────
#  #326 age cap — old still-open items roll up into one line per kind
# ──────────────────────────────────────────────────────────────────────


def _ask(date_s, text, h):
    return f"- [open · {date_s} · Rena] {text} <!-- cp:hash={h} -->\n"


def _cutoff_for(week_iso):
    """The oldest first-raised date that still carries in full into
    ``week_iso`` — derived from the engine's constant, not restated."""
    from datetime import date, timedelta

    from cp_engine.sprints import CARRY_FORWARD_MAX_AGE_WEEKS

    year, week = week_iso.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return monday - timedelta(weeks=CARRY_FORWARD_MAX_AGE_WEEKS)


def _region(path) -> str:
    body = path.read_text()
    return body[body.index("<!-- cp-engine:start carry-forward -->"):
                body.index("<!-- cp-engine:end carry-forward -->")]


def test_ask_past_the_age_cap_rolls_up_instead_of_carrying(tmp_path) -> None:
    """#326 brought back 724 asks nobody had closed since May. An ask first
    raised more than CARRY_FORWARD_MAX_AGE_WEEKS before the sprint does NOT
    carry as a bullet; the region gets one rollup line with the count, the
    oldest date and a link that opens the week file it lives in. An ask
    raised ON the cutoff day still carries in full; one day older does not."""
    import re
    from datetime import timedelta

    from cp_engine.sprints import parse_sprint_file

    cutoff = _cutoff_for("2026-W39")
    on_cap = cutoff.isoformat()
    past_cap = (cutoff - timedelta(days=1)).isoformat()
    _week(tmp_path, "2026-W30", None, own_asks=_ask(past_cap, "Old logo question", "a0000001"))
    _week(tmp_path, "2026-W32", None, own_asks=_ask(on_cap, "Edge-of-cap question", "a0000002"))
    w38 = _week(tmp_path, "2026-W38", None, own_asks=_ask("2026-09-15", "Fresh question", "a0000003"))
    w39 = _week(tmp_path, "2026-W39", "2026-W38")

    region = _region(w39)
    carried = _carried_texts(w39)
    assert any("Fresh question" in t for t in carried)
    assert any("Edge-of-cap question" in t for t in carried)
    assert not any("Old logo question" in t for t in carried)
    assert "Old logo question" not in region
    line = next(ln for ln in region.splitlines() if "stale" in ln)
    assert line == (f"- 1 stale ask (oldest {past_cap}) — triage in "
                    f"[2026-W30](../2026-W30/{w38.name})")
    # The link resolves, relative to the file it is rendered into.
    target = re.search(r"\]\(([^)]+)\)", line).group(1)
    assert (w39.parent / target).resolve().is_file()
    # And it parses back as a rollup, not as an ask.
    cf = parse_sprint_file(w39).carry_forward
    assert cf.stale_count("asks") == 1 and cf.stale_count("risks") == 0


def test_stale_rollup_counts_weeks_and_drops_when_closed_where_it_lives(
    tmp_path, caplog
) -> None:
    """Stale asks stay resolvable in their own week: closing one there drops
    it from the count on the next render. The rollup line changes every time
    that happens and no sprint file holds it, so #320's hand-edit check must
    not read the change as a hand edit being discarded."""
    import logging

    w30 = _week(tmp_path, "2026-W30", None, own_asks=_ask("2026-07-20", "Old A", "b0000001"))
    _week(tmp_path, "2026-W31", None, own_asks=_ask("2026-07-28", "Old B", "b0000002"))
    w38 = _week(tmp_path, "2026-W38", None)
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert (f"- 2 stale asks (oldest 2026-07-20) — triage in "
            f"[2026-W30](../2026-W30/{w38.name}) and 1 other week") in _region(w39)

    w30.write_text(w30.read_text().replace("[open · 2026-07-20", "[closed · 2026-07-20", 1))
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert (f"- 1 stale ask (oldest 2026-07-28) — triage in "
            f"[2026-W31](../2026-W31/{w38.name})") in _region(w39)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_current_sprint_header_names_stale_asks_separately(tmp_path) -> None:
    """The list shows live asks; the header counts them and names the stale
    backlog beside them, as it names snoozed ones: `(1 · 1 stale)`."""
    from cp_engine.aggregators import open_client_asks
    from cp_engine.sprints import parse_sprint_file, render_current_sprint_block

    _week(tmp_path, "2026-W30", None, own_asks=_ask("2026-07-20", "Old logo question", "c0000001"))
    _week(tmp_path, "2026-W38", None, own_asks=_ask("2026-09-15", "Fresh question", "c0000002"))
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    sf = parse_sprint_file(w39)
    assert [a.text.split(" <!--")[0] for a in open_client_asks(sf)] == ["Fresh question"]
    block = render_current_sprint_block(sf, "x.md")
    assert "**Open client asks** (1 · 1 stale):" in block
    assert "Old logo question" not in block


# ──────────────────────────────────────────────────────────────────────
#  #331 — risks and horizon items carry until resolved, too
# ──────────────────────────────────────────────────────────────────────


def _record_risk(path, text, *, date_s, severity="escalated"):
    """Record a risk with the real `record-risk` writer; returns its hash."""
    from cp_engine.ingest import _content_hash, _write_risk

    assert _write_risk("peb", {"text": text, "severity": severity,
                               "category": "budget", "date": date_s}, path)
    return _content_hash("peb", "record-risk", text)


def _horizon(path, bullet, sub="Milestones"):
    path.write_text(path.read_text().replace(f"### {sub}\n", f"### {sub}\n{bullet}\n", 1))


def test_unresolved_risk_still_carried_two_weeks_later(tmp_path) -> None:
    """A risk raised in W37 was carried into W38 and gone from W39 — the same
    one-week defect as #326. It must carry until resolved, dated when first
    raised, and count as active in W39."""
    from cp_engine.sprints import _active_risks, parse_sprint_file

    w37 = _week(tmp_path, "2026-W37", None)
    _record_risk(w37, "Fee increase not yet signed off", date_s="2026-09-09")
    _week(tmp_path, "2026-W38", "2026-W37")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    sf = parse_sprint_file(w39)
    carried = [r for r in sf.carry_forward.risks if "Fee increase" in r.text]
    assert len(carried) == 1 and carried[0].raised_date == "2026-09-09"
    assert "[risk · escalated · budget · 2026-09-09] Fee increase" in w39.read_text()
    assert any("Fee increase" in r.text for r in _active_risks(sf))


def test_risk_resolved_from_a_carried_copy_stops_carrying(tmp_path) -> None:
    """The Slack Resolve button targets the week the digest scanned — for a
    carried risk, the projection. `_origin_sprint_path` sends the flip to the
    week that owns it (W37); the next render must then let it go, even though
    W38's frozen region still says escalated."""
    from cp_engine.ingest import _write_resolve_risk

    w37 = _week(tmp_path, "2026-W37", None)
    h = _record_risk(w37, "Fee increase not yet signed off", date_s="2026-09-09")
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert "Fee increase" in _region(w39)  # premise
    assert _write_resolve_risk("peb", {"hash": h}, w39)
    assert "[resolved · budget" in w37.read_text()  # landed at the origin
    assert "Fee increase" in _region(w38)  # premise: stale projection
    w40 = _week(tmp_path, "2026-W40", "2026-W39")
    assert "Fee increase" not in _region(w40)


def test_risk_resolved_in_this_weeks_own_section_is_not_active(tmp_path) -> None:
    """A carried risk restated `resolved` by hand this week is no longer
    active — the rule open_client_asks applies to a closed ask."""
    from cp_engine.sprints import _active_risks, parse_sprint_file

    w37 = _week(tmp_path, "2026-W37", None)
    _record_risk(w37, "Fee increase not yet signed off", date_s="2026-09-09")
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    line = next(ln for ln in w37.read_text().splitlines() if "Fee increase" in ln)
    w38.write_text(w38.read_text().replace(
        "## Dependencies & risks\n",
        "## Dependencies & risks\n" + line.replace("[escalated ·", "[resolved ·") + "\n", 1))
    sf = parse_sprint_file(w38)
    assert sf.carry_forward.risks  # premise: carried in from W37
    assert _active_risks(sf) == []


def test_horizon_item_carries_until_marked_done(tmp_path) -> None:
    """Horizon items carried one week "because they are unresolved by
    nature" — and so vanished after one. They now carry until marked settled:
    a status token in the bracket or the item struck through. A section
    placeholder (`_None tracked._`) is not an item and never carries."""
    w37 = _week(tmp_path, "2026-W37", None)
    _horizon(w37, "- `[2026-10-20]` Round 4 review with the client.")
    _horizon(w37, "- `[by W41]` Open the production budget conversation.", "Decisions due")
    _horizon(w37, "- _None tracked._", "Opportunities")
    _week(tmp_path, "2026-W38", "2026-W37")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    region = _region(w39)
    assert "[milestone · 2026-10-20] Round 4 review" in region
    assert "[decision · by W41] Open the production budget" in region
    assert "None tracked" not in region

    body = w37.read_text()
    body = body.replace("`[2026-10-20]` Round 4", "`[done · 2026-10-20]` Round 4", 1)
    body = body.replace("`[by W41]` Open the production budget conversation.",
                        "~~Open the production budget conversation.~~", 1)
    w37.write_text(body)
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    region = _region(w39)
    assert "Round 4 review" not in region
    assert "production budget" not in region


def test_old_risks_and_horizon_items_roll_up_like_asks(tmp_path) -> None:
    """The age cap applies to every carried kind, one rollup line per kind. A
    horizon item states no raised date (its bracket is a target), so it ages
    from the earliest week that holds it. The header names stale risks."""
    from cp_engine.sprints import parse_sprint_file, render_current_sprint_block

    w30 = _week(tmp_path, "2026-W30", None)
    _record_risk(w30, "Vendor capacity for the shoot", date_s="2026-07-21")
    _horizon(w30, "- `[2026-12-01]` Year-end campaign launch.")
    w38 = _week(tmp_path, "2026-W38", None)
    _record_risk(w38, "Legal review timing", date_s="2026-09-15", severity="watching")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    region = _region(w39)
    assert "Vendor capacity" not in region and "Year-end campaign" not in region
    assert "Legal review timing" in region
    assert (f"- 1 stale risk (oldest 2026-07-21) — triage in "
            f"[2026-W30](../2026-W30/{w38.name})") in region
    assert (f"- 1 stale horizon item (oldest 2026-07-20) — triage in "
            f"[2026-W30](../2026-W30/{w38.name})") in region  # W30's Monday
    block = render_current_sprint_block(parse_sprint_file(w39), "x.md")
    assert "**Active risks** (1 · 1 stale):" in block
