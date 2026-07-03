"""Golden-markdown tests for `cp_engine.render` (arch-phase-3, issue #26).

Where `test_render.py` asserts structural fragments, these tests lock the
ENTIRE rendered file byte-for-byte against committed fixtures under
`tests/fixtures/golden/render/`. Any change to a template, region
ordering, marker placement, or spacing fails here with a unified diff.

Regenerate after an intentional template change:

    UPDATE_GOLDENS=1 uv run pytest tests/test_golden_render.py

All tests take the `golden_clock` fixture (conftest.py), which freezes
`_today_iso`, `ENGINE_VERSION`, and sprints' `date.today` — see
tests/golden_utils.py for the determinism contract.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine import (
    Issue,
    LinkedRepo,
    ProjectConfig,
    ProjectState,
    SyncConfig,
    TenantConfig,
    render_claude_md,
    render_linked_repo_md,
    render_master_cp,
    render_project_cp,
    render_weekly_cp,
)
from cp_engine.render import (
    render_account_cp,
    render_dropbox_md,
    render_exceptions_readme,
    render_project_strip_bodies,
    render_repo_md,
    render_weekly_strip_bodies,
)
from cp_engine.state import (
    CarryForward,
    ClientAsk,
    HorizonItem,
    PersonHours,
    PersonRollup,
    ProjectAllocation,
    Risk,
    SprintFacts,
    SprintFile,
    WeeklyAllocations,
    WhereItStands,
)
from tests.golden_utils import GOLDEN_TODAY, assert_matches_golden

# Every datetime is fixed. GOLDEN_TODAY (2026-05-13, Wednesday) sits in
# ISO week 2026-W20; "yesterday" is 2026-05-12.
_LAST_SYNC = datetime(2026, 5, 13, 9, 30, 0, tzinfo=timezone.utc)
_TOUCHED_YESTERDAY = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
_TOUCHED_LAST_WEEK = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def make_tenant(name: str = "1p", root: Path = Path("/tmp/fake-tenant")) -> TenantConfig:
    return TenantConfig(
        name=name,
        display=f"{name.title()} Test",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"),
        projects=(
            ProjectConfig(code="ggl-5168", github="FirstPersonSF/ggl-5168", local_path=None),
        ),
        root=root,
    )


def make_engagement(
    code: str,
    name: str,
    *,
    status: str = "Open",
    deal_stage: str | None = None,
    company_code: str = "GGL",
    company_name: str = "Google",
    owner: str | None = "Drew",
    last_touched: datetime | None = _TOUCHED_YESTERDAY,
    summary: str | None = None,
    budget: float | None = None,
    dropbox_folder_url: str | None = None,
    is_internal: bool = False,
    mc2_id: str | None = None,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name,
        source="engagement",
        company_kind="client",
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=is_internal,
        owner=owner,
        last_touched=last_touched,
        deadline=None,
        one_line_summary=summary,
        deal_stage=deal_stage,
        budget=budget,
        dropbox_folder_url=dropbox_folder_url,
        mc2_id=mc2_id,
    )


def make_initiative(
    code: str,
    name: str,
    *,
    status: str = "Active",
    company_kind: str = "self-fpsf",
    company_code: str = "1PI",
    company_name: str = "First Person",
    owner: str | None = "Tony",
    summary: str | None = None,
    mc2_id: str | None = None,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name,
        source="initiative",
        company_kind=company_kind,
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=True,
        owner=owner,
        last_touched=_TOUCHED_YESTERDAY,
        deadline=None,
        one_line_summary=summary,
        mc2_id=mc2_id,
    )


def make_repo(
    code: str,
    *,
    status: str = "Active",
    company_kind: str = "self-fpsf",
    company_code: str = "1PI",
    company_name: str = "First Person",
    description: str | None = None,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=code,
        source="repo",
        company_kind=company_kind,
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=True,
        owner=None,
        last_touched=_TOUCHED_LAST_WEEK,
        deadline=None,
        github_org="FirstPersonSF",
        repo_name=code,
        description=description,
    )


def _mixed_population() -> tuple[ProjectState, ...]:
    """Every kind × every surfaced status: pipeline deals, open client
    engagements across two accounts, holding, closed-recent, initiatives
    (Active on both scopes; On hold filtered), standalone repos on both
    scopes, plus an internal engagement + archived row that must NOT
    render."""
    return (
        # Pipeline (Deal), out of stage order to exercise the stage sort.
        make_engagement(
            "peb-5200", "Pebble Foods Discovery", status="Deal",
            deal_stage="Negotiation", company_code="PEB",
            company_name="Pebble Foods", budget=45000.0,
            summary="Pricing counter-proposal in front of Maria.",
        ),
        make_engagement(
            "sap-5174", "SAP All in with AI", status="Deal",
            deal_stage="Inquiry", company_code="SAP", company_name="SAP",
            summary="Kickoff July 8; workshop July 30.",
        ),
        # Open engagements, two accounts (sort clusters by account slug).
        make_engagement(
            "ibx-5153", "Infoblox AI Campaign", status="Open",
            company_code="IBX", company_name="Infoblox", owner="Tony",
            budget=120000.0, summary="Carol framework deck in client review.",
            mc2_id="00000000-0000-0000-0000-00000000ib53",
        ),
        make_engagement(
            "ggl-5168", "Playbooks (Activation)", status="Open",
            budget=80000.0, summary="Storyboards in flight; client review Wed.",
        ),
        # Holding + closed-recent (collapse blocks).
        make_engagement(
            "ggl-5136", "Go Safety Website", status="Holding",
            summary="Parked pending Q3 budget.",
        ),
        make_engagement(
            "ibx-5100", "Infoblox Rebrand", status="Closed",
            company_code="IBX", company_name="Infoblox",
            last_touched=_TOUCHED_LAST_WEEK,
        ),
        # Filtered rows: internal engagement + archived.
        make_engagement("ggl-9998", "Internal Scratch", is_internal=True),
        make_engagement("ggl-9999", "Old Archived Thing", status="Archived"),
        # Initiatives, both scopes; On hold must not surface.
        make_initiative(
            "mission-control", "Mission Control",
            summary="Workspace IA shipped; integrations registry live.",
        ),
        make_initiative(
            "storyos", "StoryOS", company_kind="self-canonic",
            company_code="CNC", company_name="Canonic", owner="Drew",
            summary="Substrate design in review.",
        ),
        make_initiative("market-scorecard", "Market Scorecard", status="On hold"),
        # Standalone repos, both scopes.
        make_repo("cp-engine", description="The engine behind the cp tenant."),
        make_repo(
            "unf-forge", company_kind="self-canonic", company_code="CNC",
            company_name="Canonic", description="UNF prototyping forge.",
        ),
    )


def _sprint_file(
    *,
    project_code: str,
    allocation: tuple[tuple[str, float], ...] = (),
    risks: tuple[tuple[str, str], ...] = (),
    open_asks: tuple[tuple[str, str], ...] = (),
    horizon_decisions: tuple[tuple[str, str], ...] = (),
) -> SprintFile:
    return SprintFile(
        project_code=project_code,
        week_iso="2026-W20",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint="2026-W19",
        facts=SprintFacts(None, None, None, None, None, 0, 0),
        where_it_stands=WhereItStands(None, None, None, (), ()),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        client_outbound=(),
        client_open_asks=tuple(
            ClientAsk(text=t, asked_date=d, status="open") for t, d in open_asks
        ),
        client_inbound=(),
        risks=tuple(
            Risk(text=t, severity=sev, category="", raised_date="2026-05-04")
            for sev, t in risks
        ),
        allocation=tuple(PersonHours(person_name=n, hours=h) for n, h in allocation),
        deliverables=(),
        definition_of_done="",
        horizon=tuple(
            HorizonItem(text=t, bucket="decision", target_date=td)
            for t, td in horizon_decisions
        ),
        meeting_notes=None,
    )


def _allocations() -> WeeklyAllocations:
    return WeeklyAllocations(
        week_start="2026-05-04",
        by_project={
            "ggl-5168": ProjectAllocation(
                project_code="ggl-5168",
                is_internal=False,
                entries=(
                    PersonHours(person_name="Drew Fiero", hours=6.0),
                    PersonHours(person_name="Tony Rossi", hours=2.5),
                ),
            ),
            "ibx-5153": ProjectAllocation(
                project_code="ibx-5153",
                is_internal=False,
                entries=(PersonHours(person_name="Tony Rossi", hours=8.0),),
            ),
        },
        rollup=(
            PersonRollup(
                person_name="Tony Rossi",
                engagement_hours=10.5,
                engagement_project_count=2,
                internal_hours=4.0,
            ),
            PersonRollup(
                person_name="Drew Fiero",
                engagement_hours=6.0,
                engagement_project_count=1,
                internal_hours=0.0,
            ),
        ),
    )


# ──────────────────────────────────────────────────────────────────────
#  master-cp.md
# ──────────────────────────────────────────────────────────────────────


def test_golden_master_cp_full(golden_clock, tmp_path: Path) -> None:
    """The kitchen-sink master CP: all three population kinds, holding +
    closed-recent collapses, allocations, agenda rollup, sprint-facts
    strip, Slack rollup, exceptions summary, sprint links."""
    tenant = make_tenant(root=tmp_path)

    # Slack digest bullets on disk for two active projects.
    sprints = tmp_path / "sprints" / "2026-W20"
    sprints.mkdir(parents=True)
    (sprints / "ggl-5168.md").write_text(
        "## Client communication\n\n### Slack digest\n"
        "- [2026-W20 · Slack] Tony and Geoff drove a heads-down build week. "
        "<!-- cp:hash=abc123 -->\n"
    )
    (sprints / "ibx-5153.md").write_text(
        "## Client communication\n\n### Slack digest\n"
        "- [2026-W20 · Slack] Carol framework deck shipped to Janet. "
        "<!-- cp:hash=def456 -->\n"
    )

    sf_ggl = _sprint_file(
        project_code="ggl-5168",
        allocation=(("Drew", 6.0), ("Tony", 2.5)),
        risks=(("escalated", "Legal turnaround may slip past May 22"),),
        open_asks=(("Volume forecast from ops team", "2026-05-01"),),
        horizon_decisions=(("Whether to renew for Q3", "by W21"),),
    )
    sf_ibx = _sprint_file(
        project_code="ibx-5153",
        allocation=(("Tony", 8.0),),
        risks=(("watching", "Janet's review window is tight"),),
    )

    out = render_master_cp(
        tenant,
        _mixed_population(),
        last_sync=_LAST_SYNC,
        allocations=_allocations(),
        exceptions_count=3,
        current_sprint_iso="2026-W20",
        prior_sprint_iso="2026-W19",
        parsed_sprint_files=(sf_ggl, sf_ibx),
        today=GOLDEN_TODAY,
    )
    assert_matches_golden("render/master-cp-full.md", out)


def test_golden_master_cp_empty_tenant(golden_clock) -> None:
    """Zero projects: every section region still renders (so the splicer
    can find them next sync), with no rows and no optional blocks."""
    out = render_master_cp(
        make_tenant(),
        (),
        last_sync=_LAST_SYNC,
        today=GOLDEN_TODAY,
    )
    assert_matches_golden("render/master-cp-empty.md", out)


# ──────────────────────────────────────────────────────────────────────
#  project cp.md (engagement + initiative)
# ──────────────────────────────────────────────────────────────────────


def test_golden_project_cp_engagement(golden_clock) -> None:
    """Engagement cp.md with tracked issues, a current-sprint block, and
    populated strip regions — the fullest first-scaffold shape."""
    from cp_engine.sprints import render_current_sprint_block

    project = make_engagement(
        "ggl-5168", "Playbooks (Activation)",
        budget=80000.0, summary="Storyboards in flight; client review Wed.",
        dropbox_folder_url="https://www.dropbox.com/scl/fo/ggl-5168",
        mc2_id="00000000-0000-0000-0000-0000000g5168",
    )
    issues = (
        Issue(number=42, title="Auth bug", status="Open", owner="drew",
              updated=datetime(2026, 5, 6, tzinfo=timezone.utc)),
        Issue(number=43, title="Migration order", status="Closed", owner=None,
              updated=None),
    )
    sprint_block = render_current_sprint_block(
        _sprint_file(
            project_code="ggl-5168",
            allocation=(("Drew", 6.0),),
            open_asks=(("Volume forecast from ops team", "2026-05-01"),),
            risks=(("escalated", "Legal turnaround may slip past May 22"),),
        ),
        link_path="../../sprints/2026-W20/ggl-5168.md",
    )
    strips = SimpleNamespace(
        inbound=(
            SimpleNamespace(date="2026-05-12", who="Maria",
                            text="Tier-2 cap doesn't match our 2H projections."),
        ),
        recent_decisions=(
            SimpleNamespace(date="2026-05-12", cross_cutting=False,
                            text="Hold tier-2 cap firm; widen tier-3 ramp."),
            SimpleNamespace(date="2026-05-11", cross_cutting=True,
                            text="All Google invoices route through Brandon."),
        ),
        open_asks=(
            SimpleNamespace(asked_date="2026-05-01", who="Maria", aged_days=12,
                            text="Volume forecast from ops team"),
        ),
        stakeholders=(
            SimpleNamespace(name="Maria Mraz", role="Director",
                            context="primary decision-maker"),
        ),
    )
    out = render_project_cp(
        make_tenant(), project, issues,
        current_sprint_block=sprint_block,
        project_strips=strips,
    )
    assert_matches_golden("render/project-cp-engagement.md", out)


def test_golden_project_cp_initiative(golden_clock) -> None:
    project = make_initiative(
        "mission-control", "Mission Control",
        summary="Workspace IA shipped; integrations registry live.",
        mc2_id="00000000-0000-0000-0000-000000000mc2",
    )
    out = render_project_cp(make_tenant(), project)
    assert_matches_golden("render/project-cp-initiative.md", out)


# ──────────────────────────────────────────────────────────────────────
#  account cp.md / repo files / dropbox
# ──────────────────────────────────────────────────────────────────────


def test_golden_account_cp(golden_clock) -> None:
    projects = (
        make_engagement("ggl-5168", "Playbooks (Activation)", budget=80000.0),
        make_engagement(
            "ggl-5200", "Ads Refresh", status="Deal", deal_stage="Contract",
            owner="Tony", last_touched=_TOUCHED_LAST_WEEK,
        ),
    )
    out = render_account_cp("google", "Google", projects)
    assert_matches_golden("render/account-cp.md", out)


def test_golden_repo_md(golden_clock) -> None:
    repo = make_repo("cp-engine", description="The engine behind the cp tenant.")
    out = render_repo_md(
        repo,
        local_clones_by_user={
            "drew": "/Users/drewf/Documents/Python/cp-engine",
            "tony": "/Users/tony/code/cp-engine",
        },
    )
    assert out is not None
    assert_matches_golden("render/repo-md.md", out)


def test_golden_linked_repo_md(golden_clock) -> None:
    repo = LinkedRepo(
        repo_name="ggl-5136-events-calendar",
        github_org="FirstPersonSF",
        status="Active",
        description="Firebase calendar app for the EHS team",
    )
    out = render_linked_repo_md(
        "GGL 5136 go/safety website",
        repo,
        local_clones_by_user={
            "drew": "/Users/drewf/Documents/Python/ggl-5136-events-calendar",
        },
    )
    assert_matches_golden("render/linked-repo-md.md", out)


def test_golden_dropbox_md(golden_clock) -> None:
    project = make_engagement(
        "ggl-5168", "Playbooks (Activation)",
        dropbox_folder_url="https://www.dropbox.com/scl/fo/ggl-5168",
    )
    out = render_dropbox_md(project)
    assert out is not None
    assert_matches_golden("render/dropbox-md.md", out)


# ──────────────────────────────────────────────────────────────────────
#  weekly-cp.md / CLAUDE.md / exceptions README
# ──────────────────────────────────────────────────────────────────────


def test_golden_weekly_cp(golden_clock) -> None:
    out = render_weekly_cp(make_tenant())
    assert_matches_golden("render/weekly-cp.md", out)


def test_golden_claude_md_1p(golden_clock) -> None:
    out = render_claude_md(make_tenant(name="1p"))
    assert_matches_golden("render/claude-md-1p.md", out)


def test_golden_claude_md_canonic(golden_clock) -> None:
    """Canonic tenant gains the sprint mode (mode numbering shifts)."""
    out = render_claude_md(make_tenant(name="canonic"))
    assert_matches_golden("render/claude-md-canonic.md", out)


def test_golden_exceptions_readme(golden_clock, tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions"
    exceptions.mkdir()
    # Two in the 30-day window (newest first in output), one aged out,
    # one hand-renamed file that falls back to mtime (aged out via
    # os.utime so it can't flake into the window).
    (exceptions / "2026-05-12-side-quest-1430-drew.md").write_text("x\n")
    (exceptions / "2026-04-20-old-tool-0900-tony.md").write_text("x\n")
    (exceptions / "2026-03-01-ancient-0900-drew.md").write_text("x\n")
    out = render_exceptions_readme(
        tmp_path, now=datetime(2026, 5, 13, 12, 0, 0), days=30
    )
    assert_matches_golden("render/exceptions-readme.md", out)


# ──────────────────────────────────────────────────────────────────────
#  strip bodies (spliced on every sync — lock both populated + empty)
# ──────────────────────────────────────────────────────────────────────


def _strips_to_text(bodies: dict[str, str]) -> str:
    """Stable text serialization of a strip-bodies dict for goldening."""
    chunks = []
    for region in sorted(bodies):
        chunks.append(f"===== {region} =====\n{bodies[region]}\n")
    return "\n".join(chunks)


def test_golden_project_strip_bodies(golden_clock) -> None:
    strips = SimpleNamespace(
        inbound=(
            SimpleNamespace(date="2026-05-12", who="Maria",
                            text="Tier-2 cap doesn't match our 2H projections."),
        ),
        recent_decisions=(
            SimpleNamespace(date="2026-05-12", cross_cutting=True,
                            text="All Google invoices route through Brandon."),
        ),
        open_asks=(
            SimpleNamespace(asked_date="2026-05-01", who="Maria", aged_days=12,
                            text="Volume forecast from ops team"),
            SimpleNamespace(asked_date="2026-05-11", who=None, aged_days=2,
                            text="Confirm Wednesday review slot"),
        ),
        stakeholders=(
            SimpleNamespace(name="Maria Mraz", role="Director",
                            context="primary decision-maker"),
            SimpleNamespace(name="Sam Ito", role=None, context=None),
        ),
    )
    populated = _strips_to_text(render_project_strip_bodies(strips))
    empty = _strips_to_text(render_project_strip_bodies(None))
    out = f"### populated\n\n{populated}\n### empty\n\n{empty}"
    assert_matches_golden("render/project-strip-bodies.md", out)


def test_golden_weekly_strip_bodies(golden_clock) -> None:
    tenant_strips = SimpleNamespace(
        themes=(
            SimpleNamespace(date="2026-05-12", text="Maria transition dominates."),
        ),
        cross_cutting_decisions=(
            SimpleNamespace(date="2026-05-12", project_code="ggl-5168",
                            text="Drop Claude team plan; move to Max plans."),
        ),
        carry_forward=SimpleNamespace(
            escalated_risks=(
                SimpleNamespace(project_code="peb", text="Legal turnaround risk"),
            ),
            stale_asks=(
                SimpleNamespace(project_code="ggl-5168", aged_days=12,
                                text="Volume forecast"),
            ),
            decisions_due=(
                SimpleNamespace(project_code="orb", target_date="by W21",
                                text="Whether to renew"),
            ),
        ),
    )
    populated = _strips_to_text(render_weekly_strip_bodies(tenant_strips))
    empty = _strips_to_text(render_weekly_strip_bodies(None))
    out = f"### populated\n\n{populated}\n### empty\n\n{empty}"
    assert_matches_golden("render/weekly-strip-bodies.md", out)


# ──────────────────────────────────────────────────────────────────────
#  harness meta-test
# ──────────────────────────────────────────────────────────────────────


_UPDATING = os.environ.get("UPDATE_GOLDENS") == "1"


@pytest.mark.skipif(_UPDATING, reason="tamper test would overwrite the golden")
def test_golden_harness_detects_single_char_drift(golden_clock) -> None:
    """Prove the harness actually bites: a one-character tamper on a
    rendered body must fail with a unified diff naming both sides."""
    out = render_weekly_cp(make_tenant())
    with pytest.raises(AssertionError) as exc:
        assert_matches_golden("render/weekly-cp.md", out.replace("Quick", "Qwick", 1))
    msg = str(exc.value)
    assert "diverges from golden" in msg
    assert "-" in msg and "+" in msg  # unified diff present


@pytest.mark.skipif(_UPDATING, reason="update mode creates fixtures instead")
def test_golden_harness_fails_on_missing_fixture(golden_clock) -> None:
    with pytest.raises(AssertionError, match="golden fixture missing"):
        assert_matches_golden("render/does-not-exist.md", "body")
