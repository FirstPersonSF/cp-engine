"""Tests for `cp_engine.render`.

Two halves:
- Full-file renderers — assert structural invariants on the rendered body.
- splice_managed_region — round-trip + every failure mode + hand-written
  content preservation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cp_engine import (
    Issue,
    LinkedRepo,
    MarkerDuplicated,
    MarkerInverted,
    MarkerMissing,
    ProjectConfig,
    ProjectState,
    SyncConfig,
    TenantConfig,
    render_claude_md,
    render_linked_repo_md,
    render_master_cp,
    render_project_cp,
    render_weekly_cp,
    splice_managed_region,
)
from cp_engine.state import (
    CarryForward,
    ClientAsk,
    HorizonItem,
    PersonHours,
    Risk,
    SprintFacts,
    SprintFile,
    WhereItStands,
)

# ──────────────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────────────


def make_tenant(name: str = "1p", with_project: bool = True) -> TenantConfig:
    projects: tuple[ProjectConfig, ...] = ()
    if with_project:
        projects = (
            ProjectConfig(code="ggl-5168", github="FirstPersonSF/ggl-5168", local_path=None),
        )
    return TenantConfig(
        name=name,
        display=f"{name.title()} Test",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"),
        projects=projects,
        root=Path("/tmp/fake-tenant"),
    )


def make_state(
    code: str = "ggl-5168",
    name: str = "Playbooks (Activation)",
    status: str = "Open",
    is_internal: bool = False,
    days_ago: int | None = 1,
    summary: str | None = "Storyboards in flight; client review Wed.",
    source: str = "engagement",
    company_kind: str = "client",
) -> ProjectState:
    last_touched = (
        datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        if days_ago is not None
        else None
    )
    if last_touched is not None and days_ago:
        last_touched = last_touched.replace(day=max(1, last_touched.day - days_ago))
    return ProjectState(
        code=code,
        name=name,
        source=source,  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code="GGL",
        company_name="Google",
        status=status,
        is_internal=is_internal,
        owner="drew",
        last_touched=last_touched,
        deadline=None,
        one_line_summary=summary,
    )


# ──────────────────────────────────────────────────────────────────────
#  Renderer tests — master-cp
# ──────────────────────────────────────────────────────────────────────


def test_master_cp_includes_only_active_projects() -> None:
    tenant = make_tenant()
    projects = (
        make_state("ggl-5168", "Open Project", "Open"),
        make_state("ggl-9999", "Holding Project", "Holding"),
        make_state("ggl-1111", "Closed Project", "Closed"),
        make_state("ggl-2222", "Archived Project", "Archived"),
        make_state("ggl-3333", "Internal Project", "Open", is_internal=True),
    )

    out = render_master_cp(tenant, projects, last_sync=datetime.now(timezone.utc))

    assert "ggl-5168" in out  # Open, surfaces in Active table
    assert "ggl-9999" in out  # Holding shows in collapsed subtable
    assert "ggl-1111" in out  # Closed-recent shows in collapsed list
    assert "ggl-2222" not in out  # Archived not surfaced
    assert "ggl-3333" not in out  # is_internal filtered

    # Engine-managed regions are present (v0.2: three sections per kind)
    assert "<!-- cp-engine:start active-1p -->" in out
    assert "<!-- cp-engine:end active-1p -->" in out
    assert "<!-- cp-engine:start active-fpsf -->" in out
    assert "<!-- cp-engine:start active-canonic -->" in out
    assert "<!-- cp-engine:start holding-subtable -->" in out
    assert "<!-- cp-engine:start closed-recent -->" in out
    assert "<!-- cp-engine:start last-sync-timestamp -->" in out


def test_master_cp_one_line_summary_appears() -> None:
    tenant = make_tenant()
    projects = (make_state(summary="Custom one-line summary text."),)
    out = render_master_cp(tenant, projects, last_sync=datetime.now(timezone.utc))
    assert "Custom one-line summary text." in out


def test_master_cp_handles_no_projects() -> None:
    tenant = make_tenant(with_project=False)
    out = render_master_cp(tenant, (), last_sync=datetime.now(timezone.utc))
    # All three section regions render even with zero projects
    assert "<!-- cp-engine:start active-1p -->" in out
    assert "<!-- cp-engine:start active-fpsf -->" in out
    assert "<!-- cp-engine:start active-canonic -->" in out


def test_master_cp_exceptions_summary_line_when_count_positive() -> None:
    tenant = make_tenant()
    out = render_master_cp(
        tenant,
        (),
        last_sync=datetime.now(timezone.utc),
        exceptions_count=3,
    )
    assert "<!-- cp-engine:start exceptions-summary -->" in out
    assert "**Exceptions:** 3 this week" in out


def test_master_cp_exceptions_summary_omits_line_when_zero() -> None:
    tenant = make_tenant()
    out = render_master_cp(
        tenant,
        (),
        last_sync=datetime.now(timezone.utc),
        exceptions_count=0,
    )
    # The region is always present (so splicer can find it next sync) but
    # the body line is omitted when there's nothing to surface.
    assert "<!-- cp-engine:start exceptions-summary -->" in out
    assert "<!-- cp-engine:end exceptions-summary -->" in out
    assert "**Exceptions:**" not in out


# ──────────────────────────────────────────────────────────────────────
#  Renderer tests — weekly-cp
# ──────────────────────────────────────────────────────────────────────


def test_weekly_cp_is_pure_skeleton() -> None:
    out = render_weekly_cp(make_tenant())
    # No engine-managed markers — sync never touches this file
    assert "cp-engine:start" not in out
    assert "Quick Resume" in out
    assert "Decisions" in out
    assert "Active research" in out


# ──────────────────────────────────────────────────────────────────────
#  Renderer tests — project-cp
# ──────────────────────────────────────────────────────────────────────


def test_project_cp_renders_with_no_issues() -> None:
    tenant = make_tenant()
    project = make_state()
    out = render_project_cp(tenant, project)

    assert "Playbooks (Activation)" in out
    assert "<!-- cp-engine:start tracked-issues -->" in out
    assert "<!-- cp-engine:end tracked-issues -->" in out
    # Hand-written sections present in skeleton
    assert "## Quick Resume" in out
    assert "## Decisions" in out
    assert "## Stakeholders" in out


def test_project_cp_renders_with_issues() -> None:
    tenant = make_tenant()
    project = make_state()
    issues = (
        Issue(
            number=42,
            title="Auth bug",
            status="Open",
            owner="drew",
            updated=datetime(2026, 5, 6, tzinfo=timezone.utc),
        ),
        Issue(
            number=43,
            title="Migration order",
            status="Closed",
            owner=None,
            updated=None,
        ),
    )
    out = render_project_cp(tenant, project, issues)

    assert "#42" in out
    assert "Auth bug" in out
    assert "#43" in out
    assert "Migration order" in out


# ──────────────────────────────────────────────────────────────────────
#  Renderer tests — CLAUDE.md
# ──────────────────────────────────────────────────────────────────────


def test_claude_md_for_non_canonic_omits_sprint_mode() -> None:
    out = render_claude_md(make_tenant(name="1p"))
    # Mode 3 (sprint) is canonic-only; Mode 4 numbering shifts to 3 in non-canonic tenants
    assert "update sprint" not in out
    assert "canonic/sprint-cp.md" not in out
    # All four-mode language present (with the renumbering)
    assert "Index-only" in out
    assert "Single-project" in out
    assert "Weekly review" in out


def test_claude_md_for_canonic_includes_sprint_mode() -> None:
    out = render_claude_md(make_tenant(name="canonic"))
    assert "update sprint" in out
    assert "canonic/sprint-cp.md" in out


def test_claude_md_includes_gatekeeper_rule() -> None:
    out = render_claude_md(make_tenant())
    assert "auto-glob" in out.lower() or "auto-globbing" in out
    assert "connected tenant repositories" in out


def test_claude_md_includes_status_vocabulary() -> None:
    out = render_claude_md(make_tenant())
    for status in ("Deal", "Open", "Holding", "Closed", "Archived"):
        assert status in out
    # Old vocab must NOT appear
    assert "`Active`" not in out
    assert "`Complete`" not in out


def test_claude_md_documents_sprint_file_deepening_path() -> None:
    body = render_claude_md(make_tenant())
    assert "sprints/" in body
    assert "deepen from transcript" in body
    assert "sprint file" in body.lower()
    assert "wrap up" in body


# ──────────────────────────────────────────────────────────────────────
#  Splice tests — happy path
# ──────────────────────────────────────────────────────────────────────


SAMPLE_FILE = """\
# Project CP

> Project description.

<!-- cp-engine:start tracked-issues -->
old
content
here
<!-- cp-engine:end tracked-issues -->

## Notes (hand-written)

The auth bug is blocked on Brandon's API change.
This text MUST survive every splice.
"""


def test_splice_replaces_only_the_region() -> None:
    new_body = "fresh\nnew\ncontent"
    out = splice_managed_region(SAMPLE_FILE, "tracked-issues", new_body)

    assert "fresh\nnew\ncontent" in out
    assert "old\ncontent\nhere" not in out
    # Hand-written content survives byte-for-byte
    assert "The auth bug is blocked on Brandon's API change." in out
    assert "This text MUST survive every splice." in out
    # H1 and description survive
    assert "# Project CP" in out
    assert "> Project description." in out
    # Markers themselves preserved
    assert "<!-- cp-engine:start tracked-issues -->" in out
    assert "<!-- cp-engine:end tracked-issues -->" in out


def test_splice_idempotent_when_body_unchanged() -> None:
    # First splice with body B
    body = "stable\nbody"
    once = splice_managed_region(SAMPLE_FILE, "tracked-issues", body)
    twice = splice_managed_region(once, "tracked-issues", body)
    assert once == twice


def test_splice_preserves_other_managed_regions() -> None:
    """A file with multiple managed regions: splicing one doesn't touch others."""
    multi = (
        "<!-- cp-engine:start a -->\n"
        "AAA\n"
        "<!-- cp-engine:end a -->\n"
        "between\n"
        "<!-- cp-engine:start b -->\n"
        "BBB\n"
        "<!-- cp-engine:end b -->\n"
    )
    out = splice_managed_region(multi, "a", "NEW_A")
    assert "NEW_A" in out
    assert "BBB" in out  # b region untouched
    assert "between" in out


def test_splice_handles_empty_new_body() -> None:
    """Replacing a region with empty content should leave just the markers."""
    out = splice_managed_region(SAMPLE_FILE, "tracked-issues", "")
    assert "old\ncontent\nhere" not in out
    # The markers should still exist with at most blank line between
    assert "<!-- cp-engine:start tracked-issues -->" in out
    assert "<!-- cp-engine:end tracked-issues -->" in out
    # Hand-written survives
    assert "The auth bug is blocked on Brandon's API change." in out


def test_splice_strips_outer_newlines_from_new_body() -> None:
    """Caller-supplied trailing/leading newlines on new_body don't cause
    accumulating blank lines on repeated splices."""
    once = splice_managed_region(SAMPLE_FILE, "tracked-issues", "\n\nbody\n\n")
    twice = splice_managed_region(once, "tracked-issues", "\n\nbody\n\n")
    assert once == twice  # idempotent regardless of new_body padding


# ──────────────────────────────────────────────────────────────────────
#  Splice tests — failure modes
# ──────────────────────────────────────────────────────────────────────


def test_splice_missing_start_marker() -> None:
    file = "no markers anywhere\n"
    with pytest.raises(MarkerMissing, match="No start marker"):
        splice_managed_region(file, "tracked-issues", "x")


def test_splice_missing_end_marker() -> None:
    file = "<!-- cp-engine:start tracked-issues -->\nbody\n"
    with pytest.raises(MarkerMissing, match="No end marker"):
        splice_managed_region(file, "tracked-issues", "x")


def test_splice_duplicate_start_marker() -> None:
    file = (
        "<!-- cp-engine:start r -->\n"
        "a\n"
        "<!-- cp-engine:start r -->\n"
        "b\n"
        "<!-- cp-engine:end r -->\n"
    )
    with pytest.raises(MarkerDuplicated, match="2 start markers"):
        splice_managed_region(file, "r", "x")


def test_splice_duplicate_end_marker() -> None:
    file = (
        "<!-- cp-engine:start r -->\n"
        "a\n"
        "<!-- cp-engine:end r -->\n"
        "b\n"
        "<!-- cp-engine:end r -->\n"
    )
    with pytest.raises(MarkerDuplicated, match="2 end markers"):
        splice_managed_region(file, "r", "x")


def test_splice_inverted_markers() -> None:
    file = (
        "<!-- cp-engine:end r -->\n"
        "wrong order\n"
        "<!-- cp-engine:start r -->\n"
    )
    with pytest.raises(MarkerInverted):
        splice_managed_region(file, "r", "x")


def test_splice_doesnt_match_partial_region_names() -> None:
    """Region name 'a' should not match a marker for 'active-table'."""
    file = (
        "<!-- cp-engine:start active-table -->\n"
        "table content\n"
        "<!-- cp-engine:end active-table -->\n"
    )
    with pytest.raises(MarkerMissing):
        splice_managed_region(file, "active", "x")


def test_master_cp_includes_sprint_link_per_active_row() -> None:
    tenant = make_tenant()
    projects = (make_state("peb-1234", "PEB Project", "Open"),)
    body = render_master_cp(
        tenant,
        projects,
        last_sync=datetime.now(timezone.utc),
        current_sprint_iso="2026-W19",
    )
    assert "sprints/2026-W19/peb-1234.md" in body
    assert "[W19 →]" in body


# ──────────────────────────────────────────────────────────────────────
#  Renderer tests — agenda rollup
# ──────────────────────────────────────────────────────────────────────


def _fixture_sprint_file_for_render(
    *,
    project_code: str = "peb",
    risks_with_severity: tuple[tuple[str, str], ...] = (),
    open_asks: tuple[tuple[str, str], ...] = (),
    horizon_decisions: tuple[tuple[str, str], ...] = (),
    allocation: tuple[tuple[str, float], ...] = (),
) -> SprintFile:
    """Minimal SprintFile populated only with the fields the agenda
    rollup and sprint-facts strip read (risks, client_open_asks, horizon,
    allocation).

    `risks_with_severity` is a tuple of (severity, text); `open_asks` is
    (text, asked_date_iso); `horizon_decisions` is (text, target_date);
    `allocation` is (person_name, hours). Everything else defaults to
    empty so we don't have to spell out the full sprint shape just to
    exercise the rollup.
    """
    risks = tuple(
        Risk(
            text=text,
            severity=sev,
            category="",
            raised_date="2026-05-04",
        )
        for sev, text in risks_with_severity
    )
    asks = tuple(
        ClientAsk(text=text, asked_date=asked, status="open")
        for text, asked in open_asks
    )
    horizon = tuple(
        HorizonItem(text=text, bucket="decision", target_date=target)
        for text, target in horizon_decisions
    )
    alloc = tuple(
        PersonHours(person_name=name, hours=hours)
        for name, hours in allocation
    )
    return SprintFile(
        project_code=project_code,
        week_iso="2026-W19",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint=None,
        facts=SprintFacts(None, None, None, None, None, 0, 0),
        where_it_stands=WhereItStands(None, None, None, (), ()),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        client_outbound=(),
        client_open_asks=asks,
        client_inbound=(),
        risks=risks,
        allocation=alloc,
        deliverables=(),
        definition_of_done="",
        horizon=horizon,
        meeting_notes=None,
    )


def test_master_cp_includes_agenda_rollup_when_sprint_files_provided() -> None:
    tenant = make_tenant()
    projects = (make_state("peb-1234", "PEB Project", "Open"),)
    sf_peb = _fixture_sprint_file_for_render(
        project_code="peb",
        risks_with_severity=(("escalated", "Legal turnaround risk"),),
        # asked 2026-05-01, today 2026-05-13 → 12 days old (>7)
        open_asks=(("Volume forecast", "2026-05-01"),),
    )
    sf_orb = _fixture_sprint_file_for_render(
        project_code="orb",
        # current sprint is W19; W21 is within +2 sprints
        horizon_decisions=(("Whether to renew", "by W21"),),
    )

    body = render_master_cp(
        tenant,
        projects,
        last_sync=datetime.now(timezone.utc),
        current_sprint_iso="2026-W19",
        parsed_sprint_files=(sf_peb, sf_orb),
        today=date(2026, 5, 13),
    )

    assert "## Agenda — what needs attention this week" in body
    assert "**Risks needing decision**" in body
    assert "Legal turnaround risk" in body
    assert "**Stale client asks**" in body
    assert "Volume forecast" in body
    assert "12d" in body
    assert "**Horizon items maturing**" in body
    assert "Whether to renew" in body


def test_master_cp_omits_agenda_section_when_no_sprint_files() -> None:
    tenant = make_tenant()
    body = render_master_cp(
        tenant,
        (),
        last_sync=datetime.now(timezone.utc),
    )
    # The region markers exist (so splicer can find them next sync) but
    # the section heading is absent when there's nothing to surface.
    assert "<!-- cp-engine:start agenda -->" in body
    assert "<!-- cp-engine:end agenda -->" in body
    assert "## Agenda — what needs attention this week" not in body


def test_master_cp_agenda_omits_when_all_lists_empty() -> None:
    tenant = make_tenant()
    # Sprint file with no escalated risks, no stale asks, no decisions due.
    sf = _fixture_sprint_file_for_render(project_code="peb")
    body = render_master_cp(
        tenant,
        (),
        last_sync=datetime.now(timezone.utc),
        parsed_sprint_files=(sf,),
        today=date(2026, 5, 13),
    )
    assert "## Agenda — what needs attention this week" not in body


def test_master_cp_agenda_passes_through_unparseable_target_date() -> None:
    """Decisions with target_dates that don't match the W## pattern get
    surfaced unconditionally — better to over-include than to silently drop.
    """
    tenant = make_tenant()
    sf = _fixture_sprint_file_for_render(
        project_code="peb",
        horizon_decisions=(("Pick a launch date", "TBD"),),
    )
    body = render_master_cp(
        tenant,
        (),
        last_sync=datetime.now(timezone.utc),
        parsed_sprint_files=(sf,),
        today=date(2026, 5, 13),
    )
    assert "Pick a launch date" in body


def test_master_cp_agenda_skips_unparseable_ask_dates() -> None:
    """Asks with malformed asked_date strings shouldn't crash the rollup."""
    tenant = make_tenant()
    sf = _fixture_sprint_file_for_render(
        project_code="peb",
        open_asks=(("Bad-date ask", "not-a-date"),),
    )
    body = render_master_cp(
        tenant,
        (),
        last_sync=datetime.now(timezone.utc),
        parsed_sprint_files=(sf,),
        today=date(2026, 5, 13),
    )
    # Stale-asks list filtered the bad date out → no agenda surfaces.
    assert "## Agenda — what needs attention this week" not in body


# ──────────────────────────────────────────────────────────────────────
#  Renderer tests — sprint-facts strip
# ──────────────────────────────────────────────────────────────────────


def test_master_cp_includes_sprint_facts_strip_when_sprint_files_provided() -> None:
    tenant = make_tenant()
    projects = (make_state("peb-1234", "PEB Project", "Open"),)
    sf_peb = _fixture_sprint_file_for_render(
        project_code="peb",
        allocation=(("Drew", 6.0), ("Tony", 2.0)),
        risks_with_severity=(("escalated", "Legal turnaround risk"),),
        open_asks=(("Volume forecast", "2026-05-01"),),  # 12d old, stale
        horizon_decisions=(("Whether to renew", "by W21"),),  # within 2 sprints
    )
    sf_orb = _fixture_sprint_file_for_render(
        project_code="orb",
        allocation=(("Drew", 4.0), ("Tony", 8.0)),
    )
    body = render_master_cp(
        tenant,
        projects,
        last_sync=datetime.now(timezone.utc),
        current_sprint_iso="2026-W19",
        prior_sprint_iso="2026-W18",
        parsed_sprint_files=(sf_peb, sf_orb),
        today=date(2026, 5, 13),
    )
    assert "**Total hours** 20" in body  # 6+2+4+8
    assert "**Drew** 10" in body          # 6+4
    assert "**Tony** 10" in body          # 2+8
    assert "**Active** 2" in body
    assert "**Stale asks** 1" in body
    assert "**Escalated** 1" in body
    assert "**Decisions due** 1" in body
    assert "**Prior sprint** 2026-W18" in body


def test_master_cp_section_summary_uses_count_and_state_phrase() -> None:
    """Each active-section h2 gets an auto-generated one-line summary
    sentence under the heading, like "Three deals in flight" or
    "Four engagements in delivery". Spelled-out numbers up to ten.
    """
    tenant = make_tenant()
    # 3 pipeline (client + Deal status), 4 active client engagements
    # (client + Open status). Construct ProjectState directly so we can
    # set deal_stage; make_state doesn't expose it.
    pipeline_projects = tuple(
        ProjectState(
            code=f"ggl-pipe{i}",
            name=f"Pipeline {i}",
            source="engagement",
            company_kind="client",
            company_code="GGL",
            company_name="Google",
            status="Deal",
            is_internal=False,
            owner="drew",
            last_touched=None,
            deadline=None,
            one_line_summary=None,
            deal_stage="Inquiry",
        )
        for i in range(3)
    )
    active_clients = tuple(
        make_state(code=f"ggl-act{i}", name=f"Active {i}", status="Open")
        for i in range(4)
    )
    projects = pipeline_projects + active_clients

    body = render_master_cp(tenant, projects, last_sync=datetime.now(timezone.utc))

    assert "Three deals in flight" in body
    assert "Four engagements in delivery" in body


# ──────────────────────────────────────────────────────────────────────
#  render_linked_repo_md
# ──────────────────────────────────────────────────────────────────────


def test_linked_repo_md_includes_github_link_and_engagement_context() -> None:
    repo = LinkedRepo(
        repo_name="ggl-5136-events-calendar",
        github_org="FirstPersonSF",
        status="Active",
        description="Firebase calendar app for the EHS team",
    )
    body = render_linked_repo_md("GGL 5136 go/safety website", repo)

    # GitHub link
    assert "[FirstPersonSF/ggl-5136-events-calendar]" in body
    assert "https://github.com/FirstPersonSF/ggl-5136-events-calendar" in body
    # Linked-relationship framing (vs. standalone repo's "Source repository" header)
    assert "Linked source repository" in body
    # Engagement name surfaced for context
    assert "GGL 5136 go/safety website" in body
    # Description rendered as a blockquote
    assert "> Firebase calendar app for the EHS team" in body
    # Frontmatter names the file correctly
    assert "Filename: _repo-ggl-5136-events-calendar.md" in body


def test_linked_repo_md_renders_local_clone_lines_when_provided() -> None:
    repo = LinkedRepo(
        repo_name="ggl-5136-events-calendar",
        github_org="FirstPersonSF",
        status="Active",
    )
    body = render_linked_repo_md(
        "GGL 5136 go/safety website",
        repo,
        local_clones_by_user={"drew": "/Users/drewf/Documents/Python/ggl-5136-events-calendar"},
    )
    assert "**Local clone (Drew):**" in body
    assert "/Users/drewf/Documents/Python/ggl-5136-events-calendar" in body
    # Phrase wraps across a line in the template; collapse whitespace to assert.
    assert "read git history" in " ".join(body.split())


def test_linked_repo_md_omits_local_clones_section_when_none() -> None:
    repo = LinkedRepo(
        repo_name="x",
        github_org="Org",
        status="Active",
    )
    body = render_linked_repo_md("Project Name", repo)
    assert "**Local clone" not in body
    assert "If you have the repo cloned locally" in body


def test_linked_repo_md_omits_description_blockquote_when_missing() -> None:
    repo = LinkedRepo(repo_name="x", github_org="Org", status="Active")
    body = render_linked_repo_md("Project Name", repo)
    assert "> " not in body  # no blockquote when description is None
