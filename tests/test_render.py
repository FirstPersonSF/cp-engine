"""Tests for `cp_engine.render`.

Two halves:
- Full-file renderers — assert structural invariants on the rendered body.
- splice_managed_region — round-trip + every failure mode + hand-written
  content preservation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cp_engine import (
    Issue,
    MarkerDuplicated,
    MarkerInverted,
    MarkerMissing,
    ProjectConfig,
    ProjectState,
    SyncConfig,
    TenantConfig,
    render_claude_md,
    render_master_cp,
    render_project_cp,
    render_weekly_cp,
    splice_managed_region,
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

    # Engine-managed regions are present
    assert "<!-- cp-engine:start active-table -->" in out
    assert "<!-- cp-engine:end active-table -->" in out
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
    # The skeleton renders even with zero projects
    assert "<!-- cp-engine:start active-table -->" in out
    assert "## Active" in out


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
