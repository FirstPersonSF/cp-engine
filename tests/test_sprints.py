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
    source: str = "engagement",
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
    "Holding"), or an FPSF/Canonic repo (source="repo", status="Active",
    company_kind="self-fpsf" / "self-canonic").
    """
    return ProjectState(
        code=code,
        name="Pebble Foods",
        source=source,
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
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W53\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W54 (Dec 28 – Jan 3, 2027)\n"
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
    # `pebble-foods`, and code `ggl-5136` → dir_slug `ggl-5136-pebble-foods`.
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
    """Regression test for v0.8.1: FPSF/Canonic projects (source="repo",
    status="Active") were silently filtered out in v0.8.0 because the
    orchestrator only checked `is_active_status` (MC-2 Deal/Open vocab),
    which doesn't recognize the literal "Active" used for repos. The fix
    mirrors render.py's `is_active` rule: engagement → is_active_status
    + not internal; repo → status == "Active".
    """
    from cp_engine.sprints import ensure_sprint_files_for_active_projects
    engagement = _fixture_project(code="peb", status="Open")
    fpsf_repo = _fixture_project(
        code="mc-2", status="Active", source="repo",
        is_internal=True, company_kind="self-fpsf",
    )
    canonic_repo = _fixture_project(
        code="storyos", status="Active", source="repo",
        is_internal=True, company_kind="self-canonic",
    )
    inactive_repo = _fixture_project(
        code="lns", status="Inactive", source="repo",
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
    # Real-shaped code (`ggl-5136`) so dir_slug → `ggl-5136-go-safety`,
    # matching how production project codes look.
    return ProjectState(
        code="ggl-5136",
        name="Go Safety",
        source="engagement",
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
    # the account dir. _scaffold_project's company_name is "Google".
    assert "[2 this sprint](../../1p/google/ggl-5136-go-safety/meetings/)" in body


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


def testsection_body_exact_heading():
    body = "## Horizon\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def testsection_body_em_dash_suffix():
    body = "## Horizon — 4–8 weeks out\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def testsection_body_en_dash_suffix():
    body = "## Horizon – rolling outlook\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def testsection_body_hyphen_suffix():
    body = "## Horizon - rolling outlook\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def testsection_body_colon_suffix():
    body = "## Horizon: rolling outlook\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon").strip() == "body line"


def testsection_body_multi_word_heading_with_suffix():
    body = (
        "## Dependencies & risks — rolling\nbody line\n\n## Next\nignored\n"
    )
    assert section_body(body, "Dependencies & risks").strip() == "body line"


def testsection_body_no_match():
    body = "## Other\nbody line\n"
    assert section_body(body, "Horizon") == ""


def testsection_body_stops_at_next_h2():
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


def testsection_body_prefix_match_not_greedy():
    """``Horizon`` must not match ``## Horizons & roadmap`` (different word)."""
    body = "## Horizons & roadmap\nbody line\n\n## Next\nignored\n"
    assert section_body(body, "Horizon") == ""
