"""Tests for `cp_engine.sync` orchestration.

Tests inject a fake Backend instead of hitting MC-2 — these tests are
about the orchestration layer (read → render → splice → write), not the
backend.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cp_engine import (
    Issue,
    ProjectConfig,
    ProjectState,
    SyncConfig,
    TenantConfig,
    UnknownBackend,
    sync_tenant,
)
from cp_engine.sync import (
    Backend,
    _collect_sprint_per_project_data,
    _deactivate_stale_cps,
    _ensure_mc_id_stamp,
    _find_project_dir,
    _last_week_monday,
    _read_mc_id,
    _rename_sprint_files,
)


class FakeBackend(Backend):
    """In-memory backend that returns whatever ProjectStates we hand it."""

    def __init__(self, states: tuple[ProjectState, ...]) -> None:
        self._states = states
        self.read_calls = 0

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        self.read_calls += 1
        return self._states


class AllocationRecordingBackend(Backend):
    """Backend that records which week `read_allocations` is asked for, so we
    can pin WHICH week sync surfaces as 'last week's workload' (bug #11)."""

    def __init__(self, states: tuple[ProjectState, ...]) -> None:
        self._states = states
        self.allocations_weeks: list[str] = []

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        return self._states

    def read_allocations(self, config: TenantConfig, week_start: str):
        self.allocations_weeks.append(week_start)
        return None


def test_sync_reads_prior_completed_week_for_workload(tmp_path: Path) -> None:
    """Bug #11: on a Tuesday, sync must read allocations for the PRIOR completed
    week (the one with logged hours), not the current just-started sprint week.
    `_last_week_monday` returns the upcoming-planning-window Monday (this week),
    which is empty at the start of a sprint — emptying the workload section."""
    config = make_config(tmp_path)
    fake = AllocationRecordingBackend((make_state(),))

    # Tuesday 2026-06-09. Prior completed week starts Monday 2026-06-01.
    sync_tenant(
        config,
        backend_factory=lambda _: fake,
        now=datetime(2026, 6, 9, 14, 0, 0, tzinfo=timezone.utc),
    )

    assert fake.allocations_weeks, "sync never read allocations"
    assert fake.allocations_weeks[0] == "2026-06-01"


def make_config(tenant_root: Path, backend: str = "mc-2") -> TenantConfig:
    return TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend=backend, cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(
            ProjectConfig(code="mc-2", github="FirstPersonSF/mc-2", local_path=None),
        ),
        root=tenant_root,
    )


def make_state(
    code: str = "mc-2",
    name: str | None = None,
    status: str = "Open",
    is_internal: bool = False,
    summary: str | None = None,
    source: str = "engagement",
    company_kind: str = "client",
    company_code: str = "GGL",
    company_name: str = "Google",
) -> ProjectState:
    """Default name == code so the working-dir slug equals the bare code,
    keeping path assertions in older tests simple. Tests that need to
    exercise the name-slug path pass `name="Some Real Name"` explicitly.

    Default company is Google so client projects land at `1p/google/` —
    the account-nested layout (v0.8.17+). Tests that need a different
    account (multi-account scenarios) override `company_code` and
    `company_name`."""
    return ProjectState(
        code=code,
        name=name if name is not None else code,
        source=source,  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=is_internal,
        owner="drew",
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
        one_line_summary=summary,
    )


# ──────────────────────────────────────────────────────────────────────
#  Orchestration — first-time scaffold
# ──────────────────────────────────────────────────────────────────────


def test_first_sync_creates_master_claude_and_project_cp(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(name="Mission Control v2"),))

    result = sync_tenant(config, backend_factory=lambda _: fake)

    assert result.projects_seen == 1
    assert not result.no_op
    written_names = {p.name for p in result.files_written}
    # Account-nested layout: project working dir at
    # <scope>/<company-slug>/<dir_slug>/cp.md. Default make_state has
    # company_kind="client" + company_name="Google" → scope "1p/google".
    # The code is now the canonical full slug, so dir_slug IS the slugified
    # code: bare `mc-2` (the name no longer contributes a tail).
    # Sprint file is `mc-2.md` under sprints/<YYYY-W##>/ since
    # make_state defaults to status="Open" (active subset). The per-week
    # sprint-index README.md is generated alongside the sprint file.
    # v0.8.5 adds `_week.md` for week-scope handwritten notes.
    # The engine-managed `.claude/` SessionStart hook adds settings.json +
    # the hook script on first sync (self-heals a stale `cp` CLI), and a
    # `.mcp.json` registering the `cp-sources` MCP server.
    assert written_names == {
        "master-cp.md", "CLAUDE.md", ".gitignore",
        "cp.md", "mc-2.md", "README.md", "_week.md",
        "settings.json", "check-cp-engine-version.py", ".mcp.json",
    }

    # Files actually exist + reference the project
    master = (tmp_path / "master-cp.md").read_text()
    assert "mc-2" in master
    assert "Mission Control v2" in master

    cp_path = tmp_path / "1p" / "google" / "mc-2" / "cp.md"
    project_cp = cp_path.read_text()
    assert "Mission Control v2" in project_cp
    assert "<!-- cp-engine:start tracked-issues -->" in project_cp


def test_dry_run_sync_reports_changes_without_writing(tmp_path: Path) -> None:
    """Bug #9: `cp status` should report what sync WOULD change without writing.
    A dry-run sync on a fresh tenant reports the files it would create but
    leaves the disk untouched."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(name="Mission Control v2"),))

    result = sync_tenant(config, backend_factory=lambda _: fake, dry_run=True)

    # It reports what WOULD be written...
    written_names = {p.name for p in result.files_written}
    assert "master-cp.md" in written_names
    assert not result.no_op
    # ...but nothing is actually on disk.
    assert not (tmp_path / "master-cp.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "1p").exists()


def test_dry_run_sync_on_synced_tenant_is_noop(tmp_path: Path) -> None:
    """Bug #9: a dry-run against an already-synced tenant reports no changes."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)  # real sync first

    result = sync_tenant(config, backend_factory=lambda _: fake, dry_run=True)
    assert result.no_op
    assert result.files_written == ()


def test_sync_invokes_backend_exactly_once(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)
    assert fake.read_calls == 1


# ──────────────────────────────────────────────────────────────────────
#  Orchestration — second sync (idempotency + no-op)
# ──────────────────────────────────────────────────────────────────────


def test_resync_with_unchanged_state_is_a_noop(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    fixed_now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

    # Two syncs with the same state and same `now` — the first creates files,
    # the second should be a no-op.
    first = sync_tenant(config, backend_factory=lambda _: fake, now=fixed_now)
    files = ["master-cp.md", "CLAUDE.md", "1p/google/mc-2/cp.md"]
    mtimes_after_first = {f: (tmp_path / f).stat().st_mtime_ns for f in files}

    second = sync_tenant(config, backend_factory=lambda _: fake, now=fixed_now)
    mtimes_after_second = {f: (tmp_path / f).stat().st_mtime_ns for f in files}

    assert second.no_op
    assert second.files_written == ()
    assert mtimes_after_first == mtimes_after_second


def test_resync_with_only_timestamp_difference_is_a_noop(tmp_path: Path) -> None:
    """The hourly cron's most common case: nothing in MC-2 changed since the
    last sync, but the wall clock advanced. master-cp.md should NOT be
    rewritten just because the timestamp would change."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))

    sync_tenant(
        config,
        backend_factory=lambda _: fake,
        now=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )
    master_mtime_before = (tmp_path / "master-cp.md").stat().st_mtime_ns
    master_text_before = (tmp_path / "master-cp.md").read_text()

    # Same state, but the wall clock ticked an hour. The only meaningful
    # diff would be the last-sync-timestamp region. Engine should skip.
    result = sync_tenant(
        config,
        backend_factory=lambda _: fake,
        now=datetime(2026, 5, 7, 13, 0, 0, tzinfo=timezone.utc),
    )

    assert result.no_op
    assert (tmp_path / "master-cp.md") not in result.files_written
    assert (tmp_path / "master-cp.md").stat().st_mtime_ns == master_mtime_before
    # Timestamp on disk reflects the FIRST sync — not refreshed.
    assert (tmp_path / "master-cp.md").read_text() == master_text_before
    assert "12:00:00" in master_text_before


def test_resync_with_real_change_refreshes_timestamp_too(tmp_path: Path) -> None:
    """When a real change forces a write, the timestamp gets refreshed in
    the same write — never stale alongside refreshed content."""
    config = make_config(tmp_path)
    fake1 = FakeBackend((make_state(status="Open"),))
    sync_tenant(
        config,
        backend_factory=lambda _: fake1,
        now=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )

    fake2 = FakeBackend((make_state(status="Holding"),))
    result = sync_tenant(
        config,
        backend_factory=lambda _: fake2,
        now=datetime(2026, 5, 7, 13, 0, 0, tzinfo=timezone.utc),
    )

    assert (tmp_path / "master-cp.md") in result.files_written
    master_text = (tmp_path / "master-cp.md").read_text()
    # Content reflects the new state
    assert "Holding" in master_text
    # Timestamp reflects the new sync clock, not the old one
    assert "13:00:00" in master_text
    assert "12:00:00" not in master_text


def test_resync_refreshes_stale_provenance_header(tmp_path: Path) -> None:
    """Bug #10: the master-cp.md anchor `Provenance: Version <NN> | <date>`
    header lives outside engine-managed regions, so splice-mode sync never
    refreshed it — the file looked stale (old version + date) even right after
    a clean regeneration. A real-change resync must bring the header current."""
    from cp_engine import __version__ as ENGINE_VERSION

    config = make_config(tmp_path)
    fake1 = FakeBackend((make_state(status="Open"),))
    sync_tenant(
        config,
        backend_factory=lambda _: fake1,
        now=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )

    import re

    master_path = tmp_path / "master-cp.md"
    # Simulate a header frozen from a much older engine version (the real bug:
    # a months-old `Provenance:` line surviving every subsequent sync).
    text = re.sub(
        r"Provenance: Version [^\n]+",
        "Provenance: Version 0.8.16.4 | 2026-05-18",
        master_path.read_text(),
        count=1,
    )
    master_path.write_text(text)
    assert "Version 0.8.16.4 | 2026-05-18" in master_path.read_text()

    # Resync with a real change forces a write.
    fake2 = FakeBackend((make_state(status="Holding"),))
    sync_tenant(
        config,
        backend_factory=lambda _: fake2,
        now=datetime(2026, 5, 8, 9, 0, 0, tzinfo=timezone.utc),
    )

    final = master_path.read_text()
    # Header now reflects the running engine version + today's date, not the
    # stale stamp. (The date tracks the render's `today`, currently date.today().)
    from datetime import date as _date

    assert f"Provenance: Version {ENGINE_VERSION} | {_date.today().isoformat()}" in final
    assert "0.8.16.4" not in final
    assert "2026-05-18" not in final


def test_resync_with_changed_status_updates_master_and_project_facts(
    tmp_path: Path,
) -> None:
    """A status change updates master-cp.md AND the project CP's Facts row.
    (Pre-cp-engine#15 the Facts table was scaffold-only and went stale; sync
    now re-splices the `project-facts` region from the current ProjectState,
    so Status flips Open→Holding in the project cp.md too — while every
    hand-written section stays put.)"""
    config = make_config(tmp_path)
    fake1 = FakeBackend((make_state(status="Open"),))
    sync_tenant(config, backend_factory=lambda _: fake1)

    cp_path = tmp_path / "1p" / "google" / "mc-2" / "cp.md"
    project_cp_before = cp_path.read_text()
    assert "| **Status** | Open |" in _facts_region(project_cp_before)

    fake2 = FakeBackend((make_state(status="Holding"),))
    result = sync_tenant(config, backend_factory=lambda _: fake2)

    # Both master-cp.md and the project CP changed.
    assert (tmp_path / "master-cp.md") in result.files_written
    assert cp_path in result.files_written

    facts_after = _facts_region(cp_path.read_text())
    assert "| **Status** | Holding |" in facts_after
    assert "| **Status** | Open |" not in facts_after


# ──────────────────────────────────────────────────────────────────────
#  Engine-managed splice preserves hand-written content
# ──────────────────────────────────────────────────────────────────────


def test_resync_preserves_hand_written_master_cp_content(tmp_path: Path) -> None:
    """Critical: a hand-edit outside engine-managed regions must survive sync."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)

    # User adds a hand-written section to master-cp.md
    master_path = tmp_path / "master-cp.md"
    edited = master_path.read_text() + "\n## My hand-written notes\n\nThese must survive.\n"
    master_path.write_text(edited)

    # Re-sync with new state — engine-managed regions update, hand notes stay
    fake2 = FakeBackend((make_state(name="Mission Control RENAMED"),))
    sync_tenant(config, backend_factory=lambda _: fake2)

    final = master_path.read_text()
    assert "These must survive." in final
    assert "## My hand-written notes" in final
    assert "Mission Control RENAMED" in final


def test_resync_preserves_hand_edits_outside_engine_regions(tmp_path: Path) -> None:
    """Hand-written content outside engine-managed regions survives every
    sync. v0.8 added the `current-sprint` engine region that gets spliced
    into project cp.md on every sync — but anything outside that region
    (and the `project-facts` / `tracked-issues` regions) is byte-stable."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)

    # User adds a hand-written tail to the project CP. The engine regions
    # remain in place (so the splice has somewhere to land); the tail is
    # outside them and must survive.
    project_path = tmp_path / "1p" / "google" / "mc-2" / "cp.md"
    original = project_path.read_text()
    edited = original + "\n## My hand notes\n\nShould survive.\n"
    project_path.write_text(edited)

    sync_tenant(config, backend_factory=lambda _: fake)

    final = project_path.read_text()
    assert "## My hand notes" in final
    assert "Should survive." in final


# ──────────────────────────────────────────────────────────────────────
#  project-facts re-splice (cp-engine#15)
# ──────────────────────────────────────────────────────────────────────


def _facts_region(cp_body: str) -> str:
    start = cp_body.index("<!-- cp-engine:start project-facts -->")
    end = cp_body.index("<!-- cp-engine:end project-facts -->")
    return cp_body[start:end]


def test_resync_resplices_stale_project_facts_code(tmp_path: Path) -> None:
    """cp-engine#15: the Facts table's `Code` row is re-spliced on every sync
    from the current ProjectState. A cp.md scaffolded under an old short code
    gets its Facts Code updated to the new canonical full slug — without
    touching hand-written content elsewhere."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)

    cp_path = tmp_path / "1p" / "google" / "mc-2" / "cp.md"
    original = cp_path.read_text()
    assert "| **Code** | `mc-2` |" in _facts_region(original)

    # Simulate a stale Facts region carrying an OLD code, plus a hand-written
    # tail outside any engine region that must survive the re-splice.
    stale = original.replace("| **Code** | `mc-2` |", "| **Code** | `old-short` |")
    stale += "\n## My hand notes\n\nMust survive the facts re-splice.\n"
    cp_path.write_text(stale)

    sync_tenant(config, backend_factory=lambda _: fake)

    final = cp_path.read_text()
    facts = _facts_region(final)
    # Code row re-spliced back to the canonical code from ProjectState.
    assert "| **Code** | `mc-2` |" in facts
    assert "old-short" not in facts
    # Hand-written content outside the region untouched.
    assert "## My hand notes" in final
    assert "Must survive the facts re-splice." in final


def test_resync_facts_carries_all_rows_from_project_state(tmp_path: Path) -> None:
    """All seven Facts rows render from ProjectState, not just Code. An
    engagement with stage/budget/owner/client/last-touched populated produces
    each row with the value sourced from the state."""
    config = make_config(tmp_path)
    state = make_state(
        code="ibx-5192-platform-sales-readiness-summit",
        name="Platform Sales Readiness Summit",
        status="Deal",
        company_code="IBX",
        company_name="Infoblox",
    )
    # Populate engagement-only fields the default factory leaves bare.
    import dataclasses
    state = dataclasses.replace(state, deal_stage="Proposal", budget=38450.0)
    fake = FakeBackend((state,))
    sync_tenant(config, backend_factory=lambda _: fake)

    slug = "ibx-5192-platform-sales-readiness-summit"
    cp_path = tmp_path / "1p" / "infoblox" / slug / "cp.md"
    facts = _facts_region(cp_path.read_text())

    assert "| **Code** | `ibx-5192-platform-sales-readiness-summit` |" in facts
    assert "| **Status** | Deal |" in facts
    assert "| **Stage** | Proposal |" in facts
    assert "| **Budget** | $38k |" in facts
    assert "| **Owner** | drew |" in facts
    assert "| **Client** | Infoblox (IBX) |" in facts
    assert "| **Last touched** | 2026-05-07 |" in facts


def test_resync_facts_idempotent_when_unchanged(tmp_path: Path) -> None:
    """Re-rendering an already-correct facts region is a no-op: the cp.md is
    byte-stable across a second sync with the same ProjectState."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)

    cp_path = tmp_path / "1p" / "google" / "mc-2" / "cp.md"
    before = cp_path.read_text()

    result = sync_tenant(config, backend_factory=lambda _: fake)

    after = cp_path.read_text()
    assert after == before
    assert cp_path not in result.files_written


# ──────────────────────────────────────────────────────────────────────
#  Multiple projects + filtering
# ──────────────────────────────────────────────────────────────────────


def test_sync_with_mixed_statuses_renders_correct_subtables(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fake = FakeBackend(
        (
            make_state(code="open-1", name="Open one", status="Open"),
            make_state(code="hold-1", name="Held one", status="Holding"),
            make_state(code="closed-1", name="Closed one", status="Closed"),
            make_state(
                code="internal-1", name="Internal one", status="Open", is_internal=True
            ),
        )
    )

    sync_tenant(
        config,
        backend_factory=lambda _: fake,
        now=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    master = (tmp_path / "master-cp.md").read_text()
    assert "open-1" in master  # active
    assert "hold-1" in master  # holding subtable
    assert "closed-1" in master  # closed-recent subtable
    assert "internal-1" not in master  # is_internal filtered

    # Project CP scaffolding matches master-CP visibility: internal projects
    # are NOT scaffolded into client/public tenants. They belong in their
    # own (cp-firstpersonsf) tenant. v0.3: each project is a working dir
    # under <scope>/<dir_slug>/, where dir_slug is now the slugified code.
    # Under the account-nested layout (v0.8.17+), client projects live one
    # level deeper at `1p/<company-slug>/<dir>/` — these test states all
    # default to company_name="Google", so they nest under `1p/google/`.
    account_dir = tmp_path / "1p" / "google"
    dirs = sorted(p.name for p in account_dir.iterdir() if p.is_dir())
    assert dirs == ["closed-1", "hold-1", "open-1"]


# ──────────────────────────────────────────────────────────────────────
#  Archive sweep — projects that drop out of sync's view
# ──────────────────────────────────────────────────────────────────────


def test_archived_project_cp_moves_to_inactive_dir(tmp_path: Path) -> None:
    """A project that disappears from sync output (archived in MC-2)
    has its working dir moved to its account's inactive/<code>/ bin
    (account-nested layout: `1p/<company>/inactive/<dir>/`), not deleted."""
    config = make_config(tmp_path)

    # First sync: project exists. Default make_state has
    # company_name="Google", so the live dir lands at 1p/google/.
    fake1 = FakeBackend((make_state(code="going-away"),))
    sync_tenant(config, backend_factory=lambda _: fake1)
    live_dir = tmp_path / "1p" / "google" / "going-away"
    assert (live_dir / "cp.md").exists()

    # Second sync: project is gone (e.g. archived in MC-2)
    fake2 = FakeBackend(())
    result = sync_tenant(config, backend_factory=lambda _: fake2)

    assert not live_dir.exists()
    inactive_dir = tmp_path / "1p" / "google" / "inactive" / "going-away"
    assert (inactive_dir / "cp.md").exists()
    assert inactive_dir in result.files_deactivated
    assert not result.no_op


def test_archive_preserves_hand_edited_content(tmp_path: Path) -> None:
    """Hand-edited content survives the move because we rename, not regenerate.
    v0.3: the whole working dir moves, so transcripts and other hand-added
    files travel with the cp.md."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="my-project"),)),
    )

    # User adds notes to the project CP and drops a transcript file alongside.
    # Default fixture nests under 1p/google/ per the account-nested layout.
    work_dir = tmp_path / "1p" / "google" / "my-project"
    cp_path = work_dir / "cp.md"
    edited = cp_path.read_text() + "\n## My notes\n\nImportant stuff.\n"
    cp_path.write_text(edited)
    (work_dir / "transcript.md").write_text("# 2026-05-08 call\n\n…\n")

    # Project disappears from MC-2
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))

    archived = tmp_path / "1p" / "google" / "inactive" / "my-project"
    assert (archived / "cp.md").exists()
    assert "Important stuff." in (archived / "cp.md").read_text()
    # Transcript travelled with the dir
    assert (archived / "transcript.md").exists()


def test_resync_after_archive_is_a_noop_for_that_project(tmp_path: Path) -> None:
    """Once archived, the project's working dir stays in inactive/ on subsequent syncs."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="dead-project"),)),
    )

    # Archive — per-account inactive bin under 1p/google/inactive/ for
    # the default fixture's company_name="Google".
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    archived = tmp_path / "1p" / "google" / "inactive" / "dead-project" / "cp.md"
    mtime_after_archive = archived.stat().st_mtime_ns

    # Second post-archive sync — should leave the archived dir alone.
    result = sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    assert archived.exists()
    assert archived.stat().st_mtime_ns == mtime_after_archive
    assert result.files_deactivated == ()


def test_archive_collision_logs_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If <scope>/inactive/<code>/ already exists (e.g. unarchive-then-
    re-archive cycle), the engine logs a warning and leaves both dirs in
    place rather than silently overwriting."""
    import logging

    config = make_config(tmp_path)
    # Pre-populate an existing archived working dir under 1p/google/inactive/
    # (the per-account inactive bin for client projects). Sync iterates
    # `_project_parent_dirs("1p")`, which yields existing per-account
    # subdirs — so the parent dir must exist before sync runs for the
    # sweep to consider it. Default fixture's company is Google.
    archived_old = tmp_path / "1p" / "google" / "inactive" / "ghost"
    archived_old.mkdir(parents=True)
    (archived_old / "cp.md").write_text("# Old archive\n\nFrom an earlier life.\n")

    # And a current live working dir for the same code
    live_dir = tmp_path / "1p" / "google" / "ghost"
    live_dir.mkdir(parents=True)
    (live_dir / "cp.md").write_text("# Current\n\nIn flight.\n")

    # Sync with no projects — engine wants to archive ghost/ but the
    # collision blocks it.
    with caplog.at_level(logging.WARNING, logger="cp_engine.sync"):
        result = sync_tenant(config, backend_factory=lambda _: FakeBackend(()))

    # Both dirs survive
    assert (live_dir / "cp.md").exists()
    assert (archived_old / "cp.md").read_text() == "# Old archive\n\nFrom an earlier life.\n"
    # Warning logged
    assert any("ghost" in m and "already exists" in m for m in caplog.messages)
    # No dir actually moved
    assert result.files_deactivated == ()


def test_archive_dir_itself_not_archived(tmp_path: Path) -> None:
    """<scope>/inactive/ is the archive subdir, not a project. The
    sweep must not treat it as a stale project."""
    config = make_config(tmp_path)

    # First sync creates 1p/google/keep/ (default fixture → company Google,
    # per the account-nested layout).
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="keep"),)),
    )
    # Manually create per-account inactive/ with an unrelated entry.
    inactive_root = tmp_path / "1p" / "google" / "inactive"
    inactive_root.mkdir(parents=True, exist_ok=True)
    (inactive_root / "previous").mkdir(exist_ok=True)
    (inactive_root / "previous" / "cp.md").write_text("# Old\n")

    # Re-sync with `keep` still alive — inactive/ should not be touched
    result = sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="keep"),)),
    )
    assert (tmp_path / "1p" / "google" / "keep" / "cp.md").exists()
    assert (inactive_root / "previous" / "cp.md").exists()
    assert result.files_deactivated == ()


# ──────────────────────────────────────────────────────────────────────
#  Backend resolution
# ──────────────────────────────────────────────────────────────────────


def test_unknown_backend_raises(tmp_path: Path) -> None:
    """A backend name the engine doesn't know → UnknownBackend."""
    config_unknown = TenantConfig(
        name="x",
        display="X",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="not-real", cron="0 * * * *"),
        projects=(),
        root=tmp_path,
    )
    with pytest.raises(UnknownBackend, match="not-real"):
        sync_tenant(config_unknown)


def test_github_issues_backend_not_implemented_yet(tmp_path: Path) -> None:
    config = TenantConfig(
        name="canonic",
        display="Canonic",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="github-issues", cron="0 * * * *"),
        projects=(),
        root=tmp_path,
    )
    with pytest.raises(UnknownBackend, match="v0.2"):
        sync_tenant(config)


# ──────────────────────────────────────────────────────────────────────
#  v0.3 — scope-aware tree, un-archive, _dropbox.md
# ──────────────────────────────────────────────────────────────────────


def test_mixed_scopes_land_under_correct_dirs(tmp_path: Path) -> None:
    """Three projects with three different company_kinds → three different scopes."""
    config = make_config(tmp_path)
    fake = FakeBackend(
        (
            make_state(code="ggl-5168-playbooks", name="Playbooks", company_kind="client"),
            make_state(
                code="mc-2",
                name="MC-2",
                company_kind="self-fpsf",
                source="repo",
                status="Active",
            ),
            make_state(
                code="storyos",
                name="StoryOS",
                company_kind="self-canonic",
                source="repo",
                status="Active",
            ),
        )
    )

    sync_tenant(config, backend_factory=lambda _: fake)

    # Working dirs use the slugified code (the code is now the canonical
    # full slug, so the dir IS the code). Client projects nest under their
    # account (1p/<company>/) per the account-nested layout; the company
    # defaults to "Google".
    # FPSF/Canonic projects are unchanged — they already nest by self-
    # company at the scope level.
    assert (tmp_path / "1p" / "google" / "ggl-5168-playbooks" / "cp.md").exists()
    assert (tmp_path / "firstpersonsf" / "mc-2" / "cp.md").exists()
    assert (tmp_path / "canonic" / "storyos" / "cp.md").exists()

    # Master CP links use the slugged paths (including the per-account
    # layer for client projects).
    master = (tmp_path / "master-cp.md").read_text()
    assert "1p/google/ggl-5168-playbooks/cp.md" in master
    assert "firstpersonsf/mc-2/cp.md" in master
    assert "canonic/storyos/cp.md" in master


def test_un_archive_restores_working_dir_with_hand_content(tmp_path: Path) -> None:
    """A project that's archived, then re-enters the live set, comes back
    with all its hand-written content (not a fresh scaffold)."""
    config = make_config(tmp_path)
    state = make_state(code="resurrected", name="Resurrected")

    # First sync: live. Default fixture's company is Google, so the
    # working dir lands under 1p/google/ per the account-nested layout.
    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))
    work_dir = tmp_path / "1p" / "google" / "resurrected"

    # Hand-add a transcript and edit cp.md
    (work_dir / "transcript-2026-05-08.md").write_text("# Call notes\n\nSecret sauce.\n")
    cp_path = work_dir / "cp.md"
    cp_path.write_text(cp_path.read_text() + "\n## Hand notes\n\nKeep me.\n")

    # Project drops out — gets archived to the per-account inactive bin.
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    assert not work_dir.exists()
    inactive_dir = tmp_path / "1p" / "google" / "inactive" / "resurrected"
    assert (inactive_dir / "transcript-2026-05-08.md").exists()

    # Project comes back — un-archive should restore (not re-scaffold)
    result = sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    assert (work_dir / "transcript-2026-05-08.md").read_text() == "# Call notes\n\nSecret sauce.\n"
    assert "Keep me." in (work_dir / "cp.md").read_text()
    assert not inactive_dir.exists()  # archive slot is now empty
    # The restored files appear in files_written so the caller's commit picks
    # them up.
    restored_paths = {p.name for p in result.files_written}
    assert "transcript-2026-05-08.md" in restored_paths


def test_dropbox_md_scaffolded_when_url_present(tmp_path: Path) -> None:
    """Engagements with a dropbox_folder_url get a _dropbox.md file."""
    config = make_config(tmp_path)
    state = ProjectState(
        code="ggl-5168-playbooks",
        name="Playbooks",
        source="engagement",  # type: ignore[arg-type]
        company_kind="client",  # type: ignore[arg-type]
        company_code="GGL",
        company_name="Google",
        status="Open",
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
        dropbox_folder_url="https://www.dropbox.com/scl/fo/abc123/h?dl=0",
    )

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    dropbox_path = tmp_path / "1p" / "google" / "ggl-5168-playbooks" / "_dropbox.md"
    assert dropbox_path.exists()
    body = dropbox_path.read_text()
    assert "https://www.dropbox.com/scl/fo/abc123/h?dl=0" in body
    assert "Playbooks" in body


def test_dropbox_md_omitted_when_no_url(tmp_path: Path) -> None:
    """Projects without a dropbox_folder_url (most repos, some engagements)
    get no _dropbox.md."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="no-dropbox"),)),
    )

    dropbox_path = tmp_path / "1p" / "google" / "no-dropbox" / "_dropbox.md"
    assert not dropbox_path.exists()


def test_dropbox_md_re_renders_on_url_change(tmp_path: Path) -> None:
    """When MC-2's dropbox_folder_url changes, _dropbox.md updates on next sync."""
    config = make_config(tmp_path)

    def state_with(url: str) -> ProjectState:
        return ProjectState(
            code="mover",
            name="Mover",
            source="engagement",  # type: ignore[arg-type]
            company_kind="client",  # type: ignore[arg-type]
            company_code=None,
            company_name=None,
            status="Open",
            is_internal=False,
            owner=None,
            last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
            deadline=None,
            dropbox_folder_url=url,
        )

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((state_with("https://dropbox.com/old"),)),
    )
    # state_with(...) leaves company_name=None, which company_slug maps to
    # "unknown" — so the per-account dir is `1p/unknown/` under the
    # account-nested layout.
    dropbox_path = tmp_path / "1p" / "unknown" / "mover" / "_dropbox.md"
    assert "old" in dropbox_path.read_text()

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((state_with("https://dropbox.com/new"),)),
    )
    assert "new" in dropbox_path.read_text()


def test_gitignore_written_at_root(tmp_path: Path) -> None:
    """v0.3 tenants get a .gitignore that blocks binary content."""
    config = make_config(tmp_path)
    sync_tenant(
        config, backend_factory=lambda _: FakeBackend((make_state(),))
    )
    gitignore = (tmp_path / ".gitignore").read_text()
    assert "*.mp4" in gitignore
    assert "*.pdf" in gitignore
    assert ".DS_Store" in gitignore
    assert ".cp-engine.local.toml" in gitignore


# ──────────────────────────────────────────────────────────────────────
#  Account cp.md scaffolding (v0.8.17+, 1p-only)
# ──────────────────────────────────────────────────────────────────────


def test_account_cp_scaffolded_for_active_client_company(tmp_path: Path) -> None:
    """When a client project lands under `1p/<company>/`, sync also creates
    `1p/<company>/cp.md` from the account template. Account list is
    derived from active client projects' company_name; no separate
    backend query."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5168", name="Playbooks"),)
        ),
    )

    account_cp = tmp_path / "1p" / "google" / "cp.md"
    assert account_cp.exists()
    body = account_cp.read_text()
    # Anchor block + heading reflect the company name.
    assert "Google" in body
    # Engine-managed regions for facts + project list are spliced in.
    assert "<!-- cp-engine:start account-facts -->" in body
    assert "<!-- cp-engine:end account-facts -->" in body
    assert "<!-- cp-engine:start projects -->" in body
    assert "<!-- cp-engine:end projects -->" in body


def test_account_cp_not_scaffolded_for_self_company_scopes(tmp_path: Path) -> None:
    """FPSF and Canonic already nest by self-company at the scope level —
    they don't get an account cp.md layer. Only `1p/` accounts do."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (
                make_state(
                    code="mc-2", name="mc-2", source="repo", status="Active",
                    company_kind="self-fpsf",
                ),
                make_state(
                    code="storyos", name="storyos", source="repo", status="Active",
                    company_kind="self-canonic",
                ),
            )
        ),
    )

    # No spurious account-cp files at the FPSF/Canonic scope roots.
    assert not (tmp_path / "firstpersonsf" / "cp.md").exists()
    assert not (tmp_path / "canonic" / "cp.md").exists()


def test_account_cp_lists_all_active_projects_for_the_account(tmp_path: Path) -> None:
    """The `projects` engine region in `1p/<company>/cp.md` enumerates
    every active client project under that account, linking each to its
    nested project cp.md."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (
                make_state(code="ggl-5168-playbooks", name="Playbooks"),
                make_state(code="ggl-5177-event-safety", name="Event Safety"),
                # Different company → different account file, not listed here.
                make_state(
                    code="ibx-5153", name="AI Campaign",
                    company_code="IBX", company_name="Infoblox",
                ),
            )
        ),
    )

    google_cp = (tmp_path / "1p" / "google" / "cp.md").read_text()
    # Both Google projects are listed with their codes + links to the
    # nested project cp.md (literal relative paths since projects live
    # one level under their account).
    assert "ggl-5168" in google_cp
    assert "ggl-5168-playbooks/cp.md" in google_cp
    assert "ggl-5177" in google_cp
    assert "ggl-5177-event-safety/cp.md" in google_cp
    # Infoblox project does NOT bleed into Google's account.
    assert "ibx-5153" not in google_cp

    # Infoblox account file exists and lists only the Infoblox project.
    infoblox_cp = (tmp_path / "1p" / "infoblox" / "cp.md").read_text()
    assert "Infoblox" in infoblox_cp
    assert "ibx-5153" in infoblox_cp
    assert "ggl-5168" not in infoblox_cp


def test_master_cp_active_1p_table_has_account_column(tmp_path: Path) -> None:
    """The `active-1p` table gains a leading Account column. Rows are
    sorted by (account_slug, code) so projects from the same account
    cluster together. The account cell links to that account's cp.md."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (
                # Intentionally out of code order so we can verify the
                # (account_slug, code) sort puts Google's two together,
                # then Infoblox's one.
                make_state(code="ibx-5153", name="AI Campaign",
                           company_code="IBX", company_name="Infoblox"),
                make_state(code="ggl-5177", name="Event Safety"),
                make_state(code="ggl-5168", name="Playbooks"),
            )
        ),
    )

    master = (tmp_path / "master-cp.md").read_text()
    # Account header is present
    assert "| Account |" in master
    # Account cell links to the account cp.md
    assert "[Google](1p/google/cp.md)" in master
    assert "[Infoblox](1p/infoblox/cp.md)" in master
    # Sort order: Google's two rows come before Infoblox's, and within
    # Google ggl-5168 comes before ggl-5177 (code order).
    google_5168 = master.find("ggl-5168")
    google_5177 = master.find("ggl-5177")
    ibx = master.find("ibx-5153")
    assert 0 < google_5168 < google_5177 < ibx


def test_account_cp_preserves_hand_written_content_on_resync(tmp_path: Path) -> None:
    """Account cp.md follows the same hand-vs-engine discipline as
    project cp.md: only the marked regions get rewritten. Hand-edits
    outside engine-managed regions survive every sync."""
    config = make_config(tmp_path)

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5168", name="Playbooks"),)
        ),
    )
    account_cp = tmp_path / "1p" / "google" / "cp.md"
    original = account_cp.read_text()
    account_cp.write_text(
        original + "\n## My account notes\n\nDurable truths about Google.\n"
    )

    # Add a second project under the same account → triggers a re-splice
    # of the projects region but must leave hand content alone.
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (
                make_state(code="ggl-5168", name="Playbooks"),
                make_state(code="ggl-5177", name="Event Safety"),
            )
        ),
    )

    final = account_cp.read_text()
    assert "## My account notes" in final
    assert "Durable truths about Google." in final
    # The engine region picked up the new project.
    assert "ggl-5177" in final


# ──────────────────────────────────────────────────────────────────────
#  Working-dir slugs (v0.3.2+)
# ──────────────────────────────────────────────────────────────────────


def test_working_dir_uses_slugged_code(tmp_path: Path) -> None:
    """An engagement with a descriptive code (the canonical full_job_name
    slug) lands at <slugified-code>/. The dir IS the slugified code; the
    name no longer contributes a tail."""
    config = make_config(tmp_path)
    state = make_state(
        code="ggl-5177-event-safety-playbook",
        name="Event Safety Playbook",
    )

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    # Account-nested layout: client projects live one level deeper under
    # their account dir. The default fixture state has company "Google".
    expected_dir = tmp_path / "1p" / "google" / "ggl-5177-event-safety-playbook"
    assert (expected_dir / "cp.md").exists()


def test_working_dir_falls_back_to_bare_code(tmp_path: Path) -> None:
    """When name == code (typical for repos), the slug is just the code."""
    config = make_config(tmp_path)
    state = make_state(code="mc-2", name="mc-2", source="repo", status="Active",
                        company_kind="self-fpsf")

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    assert (tmp_path / "firstpersonsf" / "mc-2" / "cp.md").exists()


def test_name_drift_does_not_move_dir(tmp_path: Path) -> None:
    """The working dir is keyed on the (now-descriptive) code, so a change
    to MC-2's `name` alone leaves the dir in place — no rename, and
    hand-written content stays put. (Under the new canonical-id contract
    the dir IS the slugified code; the name no longer affects the path.)"""
    config = make_config(tmp_path)

    # First sync: scaffold at ggl-5177-event-safety-playbook/ (the dir is
    # the slugified code).
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5177-event-safety-playbook", name="Event Safety Playbook"),)
        ),
    )
    work_dir = tmp_path / "1p" / "google" / "ggl-5177-event-safety-playbook"
    assert work_dir.exists()

    # Hand-add a transcript so we can verify content survives.
    (work_dir / "transcript.md").write_text("# call notes\n")

    # Sync again with the SAME code but a drifted name.
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5177-event-safety-playbook", name="Activation Playbook"),)
        ),
    )

    # Dir is unchanged and content survives — the name change is inert.
    assert work_dir.exists()
    assert (work_dir / "transcript.md").read_text() == "# call notes\n"


def test_repo_md_scaffolded_for_repo_source_projects(tmp_path: Path) -> None:
    """A repo-source project gets `_repo.md` with the GitHub URL."""
    config = make_config(tmp_path)
    state = ProjectState(
        code="mc-2",
        name="mc-2",
        source="repo",  # type: ignore[arg-type]
        company_kind="self-fpsf",  # type: ignore[arg-type]
        company_code="1P",
        company_name="First Person",
        status="Active",
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
        github_org="FirstPersonSF",
        repo_name="mc-2",
    )

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    repo_path = tmp_path / "firstpersonsf" / "mc-2" / "_repo.md"
    assert repo_path.exists()
    body = repo_path.read_text()
    assert "https://github.com/FirstPersonSF/mc-2" in body
    assert "FirstPersonSF/mc-2" in body


def test_repo_md_includes_local_clone_paths_per_user_when_configured(tmp_path: Path) -> None:
    """When [local-repos.<user>] has an entry for a project's repo_name,
    the rendered `_repo.md` surfaces one **Local clone (User):** line per
    user. Multi-user shape lets the file show everyone's paths so any
    teammate's Claude session can find the right clone."""
    config = TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(
            ProjectConfig(code="mc-2", github="FirstPersonSF/mc-2", local_path=None),
        ),
        root=tmp_path,
        local_repos_by_user={
            "drew": {"mc-2": "/Users/drew/Documents/Python/mc-2"},
            "tony": {"mc-2": "/Users/tony/code/mc-2"},
        },
    )

    state = ProjectState(
        code="mc-2",
        name="mc-2",
        source="repo",  # type: ignore[arg-type]
        company_kind="self-fpsf",  # type: ignore[arg-type]
        company_code="1P",
        company_name="First Person",
        status="Active",
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
        github_org="FirstPersonSF",
        repo_name="mc-2",
    )

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    body = (tmp_path / "firstpersonsf" / "mc-2" / "_repo.md").read_text()
    assert "**Local clone (Drew):** `/Users/drew/Documents/Python/mc-2`" in body
    assert "**Local clone (Tony):** `/Users/tony/code/mc-2`" in body


def test_repo_md_omits_a_users_path_when_they_dont_have_the_repo(
    tmp_path: Path,
) -> None:
    """A user with [local-repos.<user>] entries for OTHER repos but not
    this one shouldn't appear in this _repo.md."""
    config = TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(
            ProjectConfig(code="mc-2", github="FirstPersonSF/mc-2", local_path=None),
        ),
        root=tmp_path,
        local_repos_by_user={
            "drew": {"mc-2": "/Users/drew/code/mc-2"},
            "tony": {"storyos": "/Users/tony/code/storyos"},  # no mc-2
        },
    )

    state = ProjectState(
        code="mc-2",
        name="mc-2",
        source="repo",  # type: ignore[arg-type]
        company_kind="self-fpsf",  # type: ignore[arg-type]
        company_code="1P",
        company_name="First Person",
        status="Active",
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
        github_org="FirstPersonSF",
        repo_name="mc-2",
    )

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    body = (tmp_path / "firstpersonsf" / "mc-2" / "_repo.md").read_text()
    assert "Drew" in body
    assert "Tony" not in body


def test_repo_md_omits_local_clone_path_when_not_configured(tmp_path: Path) -> None:
    """Without a [local-repos.<user>] entry, _repo.md keeps the v0.3.3 shape
    (no local clone surfaced)."""
    config = make_config(tmp_path)
    state = ProjectState(
        code="mc-2",
        name="mc-2",
        source="repo",  # type: ignore[arg-type]
        company_kind="self-fpsf",  # type: ignore[arg-type]
        company_code="1P",
        company_name="First Person",
        status="Active",
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
        github_org="FirstPersonSF",
        repo_name="mc-2",
    )

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    body = (tmp_path / "firstpersonsf" / "mc-2" / "_repo.md").read_text()
    assert "**Local clone:**" not in body


def test_repo_md_omitted_for_engagement_source_projects(tmp_path: Path) -> None:
    """Engagements get `_dropbox.md` (when they have a URL), not `_repo.md`."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="ggl-5168"),)),
    )

    repo_path = tmp_path / "1p" / "google" / "ggl-5168" / "_repo.md"
    assert not repo_path.exists()


def test_exceptions_readme_regenerated_when_dir_exists(tmp_path: Path) -> None:
    """When `<tenant>/exceptions/` exists, sync writes/refreshes its README
    with a splice region listing the recent exception files."""
    config = make_config(tmp_path)

    # Pre-create the exceptions dir with one exception file.
    exceptions = tmp_path / "exceptions"
    exceptions.mkdir()
    (exceptions / "2026-05-09-1p-component-library-1430-drew.md").write_text(
        "## Session\nbody\n", encoding="utf-8"
    )

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(()),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    readme = exceptions / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "<!-- cp-engine:start exceptions-list -->" in text
    assert "1p-component-library" in text


def test_exceptions_readme_not_created_when_no_exceptions_dir(tmp_path: Path) -> None:
    """If no exceptions/ dir, sync doesn't conjure one. The README appears
    only after a real exception lands."""
    config = make_config(tmp_path)

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(()),
    )

    assert not (tmp_path / "exceptions").exists()


def test_existing_canonical_slug_dir_is_reused(tmp_path: Path) -> None:
    """A working dir already at the canonical slugified code is reused on the
    next sync (not re-scaffolded), and hand-written content survives.

    Under the canonical-id contract the dir IS the slugified code, so a code
    like `ggl-5177-event-safety-playbook` already sits at its final path —
    sync is idempotent over it. (The old `<bare-code> → <code>-<name-slug>`
    rename no longer applies: the descriptive code already carries the
    name.)"""
    config = make_config(tmp_path)

    # Pre-create the canonical-slug dir with hand content.
    work_dir = tmp_path / "1p" / "google" / "ggl-5177-event-safety-playbook"
    work_dir.mkdir(parents=True)
    (work_dir / "cp.md").write_text("# legacy content\n")
    (work_dir / "notes.md").write_text("# preserved\n")

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5177-event-safety-playbook", name="Event Safety Playbook"),)
        ),
    )

    # Same dir, content preserved. The active sync may splice in a
    # `current-sprint` block (active status + sprint window), so we assert
    # the original line is still present rather than full-body equality.
    cp_body = (work_dir / "cp.md").read_text()
    assert "# legacy content" in cp_body
    assert (work_dir / "notes.md").read_text() == "# preserved\n"


# ──────────────────────────────────────────────────────────────────────
#  Sprint files — per-project per-sprint markdown wired into sync_tenant
# ──────────────────────────────────────────────────────────────────────


def test_sync_writes_current_sprint_block_into_project_cp(tmp_path: Path) -> None:
    """When a sprint file exists for an active project, sync_tenant splices
    the rendered `## Current sprint` block into the project's cp.md inside
    the engine-managed `current-sprint` region."""
    # account_scope_for includes the per-company layer for clients
    # ("1p/<company>"), which is where the project actually lives on
    # disk under the account-nested layout.
    from cp_engine.state import account_scope_for, dir_slug

    config = make_config(tmp_path)
    project = make_state(code="peb", name="Pebble Foods", status="Open")
    fake = FakeBackend((project,))

    sync_tenant(
        config,
        backend_factory=lambda _: fake,
        now=datetime(2026, 5, 11, 8, 0),  # Mon → planning W19 (v0.8.7.3 anchor)
    )

    week_iso = "2026-W20"
    slug = dir_slug(project.code, project.name)
    scope = account_scope_for(project)
    cp_path = tmp_path / scope / slug / "cp.md"
    body = cp_path.read_text()

    assert "<!-- cp-engine:start current-sprint -->" in body
    assert "<!-- cp-engine:end current-sprint -->" in body
    assert "## Current sprint" in body
    assert f"sprints/{week_iso}/peb.md" in body


def test_sync_splices_current_sprint_into_existing_project_cp(tmp_path: Path) -> None:
    """When a project's cp.md was scaffolded BEFORE the current-sprint marker
    existed (legacy file on disk), the next sync still inserts the markers +
    rendered block. Hand-written content outside the engine regions survives."""
    # account_scope_for handles the per-account layer for clients
    # ("1p/<company>"), matching where sync places project dirs on disk.
    from cp_engine.state import account_scope_for, dir_slug

    config = make_config(tmp_path)
    project = make_state(code="peb", name="Pebble Foods", status="Open")
    slug = dir_slug(project.code, project.name)
    scope = account_scope_for(project)
    project_dir = tmp_path / scope / slug
    project_dir.mkdir(parents=True)
    cp_path = project_dir / "cp.md"
    # Pre-populate a legacy cp.md missing the current-sprint marker but
    # carrying the project-facts and tracked-issues markers + a hand-edit.
    legacy = (
        "# Pebble Foods — Project CP\n\n"
        "<!-- cp-engine:start project-facts -->\n"
        "## Facts\n"
        "<!-- cp-engine:end project-facts -->\n\n"
        "<!-- cp-engine:start tracked-issues -->\n"
        "## Tracked issues\n"
        "<!-- cp-engine:end tracked-issues -->\n\n"
        "## My hand-written notes\n\nMust survive.\n"
    )
    cp_path.write_text(legacy)

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((project,)),
        now=datetime(2026, 5, 11, 8, 0),  # Mon → planning W19 (v0.8.7.3 anchor)
    )

    body = cp_path.read_text()
    assert "<!-- cp-engine:start current-sprint -->" in body
    assert "<!-- cp-engine:end current-sprint -->" in body
    assert "## Current sprint" in body
    assert "sprints/2026-W20/peb.md" in body
    # Hand-written content must survive the marker injection.
    assert "Must survive." in body
    assert "## My hand-written notes" in body


def test_sync_tenant_writes_sprint_files_for_active_projects(tmp_path: Path) -> None:
    """sync_tenant should call into the sprint-file orchestrator for every
    active project — engagement (Open/Deal, not is_internal) OR repo-source
    (status="Active"), dropping a `<code>.md` file under
    `<tenant_root>/sprints/<YYYY-W##>/`. The orchestrator (via
    _is_active_for_sprint) mirrors render.py's `is_active` rule.

    v0.8.2 regression: previously sync.py pre-filtered with
    `not is_internal and is_active_status(status)`, which stripped FPSF/
    Canonic repos (status="Active", is_internal=True) before the
    orchestrator could consider them — even after v0.8.1 tried to fix
    this at the orchestrator level. The fix now hands the full project
    list down and lets the orchestrator own the rule.
    """
    config = make_config(tmp_path)
    fake = FakeBackend(
        (
            # Active client engagement → sprint file written
            make_state(code="peb", name="Pebble Foods", status="Open"),
            # Holding engagement → no sprint file
            make_state(code="apx", name="Apex Holding", status="Holding"),
            # Internal engagement (rare, but is_internal=True wins) → no sprint file
            make_state(
                code="internal-1",
                name="Internal one",
                status="Open",
                is_internal=True,
            ),
            # FPSF internal tooling: source="repo", status="Active",
            # is_internal=True → SPRINT FILE WRITTEN per v0.8.2
            make_state(
                code="mc-2-tooling",
                name="MC-2 tooling",
                source="repo",
                company_kind="self-fpsf",
                status="Active",
                is_internal=True,
            ),
            # Canonic project: same shape, different company_kind → SPRINT FILE WRITTEN
            make_state(
                code="storyos",
                name="storyos",
                source="repo",
                company_kind="self-canonic",
                status="Active",
                is_internal=True,
            ),
            # Inactive repo → no sprint file
            make_state(
                code="lns",
                name="Lensman",
                source="repo",
                company_kind="self-fpsf",
                status="Inactive",
                is_internal=True,
            ),
        )
    )

    sync_tenant(
        config,
        backend_factory=lambda _: fake,
        now=datetime(2026, 5, 11, 8, 0),  # Mon → planning W19 (v0.8.7.3 anchor)
    )

    sprint_dir = tmp_path / "sprints" / "2026-W20"
    assert sprint_dir.is_dir()
    # Active engagements + repo-source projects with status=Active are included.
    assert (sprint_dir / "peb.md").exists()
    assert (sprint_dir / "mc-2-tooling.md").exists()
    assert (sprint_dir / "storyos.md").exists()
    # Holding, internal-engagement, and inactive-repo are excluded.
    assert not (sprint_dir / "apx.md").exists()
    assert not (sprint_dir / "internal-1.md").exists()
    assert not (sprint_dir / "lns.md").exists()


# ──────────────────────────────────────────────────────────────────────
#  Sprint-window anchor logic — _last_week_monday
# ──────────────────────────────────────────────────────────────────────


# Rule: anchor on the upcoming sprint-planning Monday — that's "next
# Monday, unless today IS Monday, in which case today." The 7-day
# allocation window is the week ending the day before that anchor.


def test_last_week_monday_on_monday_uses_today() -> None:
    """If today is Monday, anchor IS today — show the week just ended."""
    monday = datetime(2026, 5, 11, 9, 0)  # Mon May 11 (sprint planning day)
    # Anchor = today (May 11) → window starts 7 days before = May 4
    assert _last_week_monday(monday) == date(2026, 5, 4)


def test_last_week_monday_on_tuesday_anchors_on_next_monday() -> None:
    """Day after sprint planning: window flips forward to the new week."""
    tuesday = datetime(2026, 5, 12, 9, 0)
    # Next Monday is May 18 → window starts May 11
    assert _last_week_monday(tuesday) == date(2026, 5, 11)


def test_last_week_monday_on_saturday_anchors_on_upcoming_monday() -> None:
    """Weekend prep for Monday's planning: show the week the meeting will plan."""
    saturday = datetime(2026, 5, 9, 9, 0)
    # Next Monday is May 11 → window starts May 4
    assert _last_week_monday(saturday) == date(2026, 5, 4)


def test_last_week_monday_on_sunday_anchors_on_upcoming_monday() -> None:
    """Sunday before sprint planning: same window as Monday will see."""
    sunday = datetime(2026, 5, 10, 9, 0)
    assert _last_week_monday(sunday) == date(2026, 5, 4)


def test_last_week_monday_consistent_across_meeting_week() -> None:
    """Saturday → Sunday → Monday morning all show the same week
    (May 4 - May 10), so the picture during meeting prep matches the
    picture at the meeting itself."""
    sat = _last_week_monday(datetime(2026, 5, 9))
    sun = _last_week_monday(datetime(2026, 5, 10))
    mon = _last_week_monday(datetime(2026, 5, 11))
    assert sat == sun == mon == date(2026, 5, 4)


def test_last_week_monday_flips_after_meeting_day() -> None:
    """Monday → Tuesday: window must move forward by one week so the
    next sprint planning operates on fresh data."""
    mon = _last_week_monday(datetime(2026, 5, 11))
    tue = _last_week_monday(datetime(2026, 5, 12))
    # One week's difference
    assert (tue - mon).days == 7


def test_last_week_monday_friday_during_week() -> None:
    """Friday May 8: still in 'planning for next Monday' mode."""
    friday = datetime(2026, 5, 8, 9, 0)
    # Next Monday is May 11 → window starts May 4
    assert _last_week_monday(friday) == date(2026, 5, 4)


def test_last_week_monday_accepts_date_or_datetime() -> None:
    """Helper handles both date and datetime inputs."""
    as_datetime = _last_week_monday(datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc))
    as_date = _last_week_monday(date(2026, 5, 9))
    assert as_datetime == as_date == date(2026, 5, 4)


# ──────────────────────────────────────────────────────────────────────
#  Quick Resume engine-managed region (v0.11.0+, Lever 5)
#
# Project cp.md's `## Quick Resume` becomes an engine-managed region
# wrapped in `<!-- cp-engine:start quick-resume -->` markers. Sync
# wraps existing un-marked sections on the cutover sync; new
# scaffolds carry the markers from the template.
# ──────────────────────────────────────────────────────────────────────


def test_ensure_quick_resume_markers_wraps_existing_section() -> None:
    """Pre-cutover cp.md has `## Quick Resume` + body, no markers. The
    helper inserts markers before the heading and after the section's
    last line (just before the next `## ` heading)."""
    from cp_engine.sync import _ensure_quick_resume_markers

    body = (
        "# Some project\n\n"
        "<!-- cp-engine:end tracked-issues -->\n\n"
        "## Quick Resume\n\n"
        "**Last session:** 2026-05-25 — Drew\n"
        "**Current work:** Tony shipping playbooks to Rena.\n"
        "**Next up:** EHS pitch deck Tue-Thu.\n"
        "**Blockers:** None.\n\n"
        "## Current Work\n\n"
        "_<long-form>_\n"
    )
    wrapped = _ensure_quick_resume_markers(body)

    # Markers present, wrapping the Quick Resume section.
    assert "<!-- cp-engine:start quick-resume -->" in wrapped
    assert "<!-- cp-engine:end quick-resume -->" in wrapped
    # Content preserved verbatim — all four lines still in the body.
    assert "**Current work:** Tony shipping playbooks to Rena." in wrapped
    assert "**Next up:** EHS pitch deck Tue-Thu." in wrapped
    # End marker comes before the next `## ` heading (Current Work stays
    # outside the region).
    end_pos = wrapped.find("<!-- cp-engine:end quick-resume -->")
    cw_pos = wrapped.find("## Current Work")
    assert end_pos != -1 and cw_pos != -1
    assert end_pos < cw_pos


def test_ensure_quick_resume_markers_is_idempotent() -> None:
    """Running twice on an already-wrapped body returns it unchanged."""
    from cp_engine.sync import _ensure_quick_resume_markers

    body = (
        "<!-- cp-engine:start quick-resume -->\n"
        "## Quick Resume\n\n"
        "**Current work:** existing line.\n"
        "<!-- cp-engine:end quick-resume -->\n\n"
        "## Next Section\n"
    )
    once = _ensure_quick_resume_markers(body)
    twice = _ensure_quick_resume_markers(once)
    assert once == twice == body


def test_ensure_quick_resume_markers_handles_section_at_end_of_file() -> None:
    """Edge case: Quick Resume is the LAST section. End marker should
    extend to end-of-file (no following `## ` heading to bound against)."""
    from cp_engine.sync import _ensure_quick_resume_markers

    body = (
        "# Some project\n\n"
        "## Quick Resume\n\n"
        "**Last session:** _<date>_\n"
        "**Current work:** _<what's in flight right now>_\n"
        "**Next up:** _<next 1-3 concrete actions, dated where possible>_\n"
        "**Blockers:** _<or \"None\">_\n"
    )
    wrapped = _ensure_quick_resume_markers(body)

    assert "<!-- cp-engine:start quick-resume -->" in wrapped
    assert "<!-- cp-engine:end quick-resume -->" in wrapped
    # End marker is at the end of the file (after the last `**Blockers:**`
    # line, no following section).
    assert wrapped.rstrip().endswith("<!-- cp-engine:end quick-resume -->")


def test_ensure_quick_resume_markers_handles_missing_section() -> None:
    """Cp.md without a `## Quick Resume` section at all — should pass
    through unchanged. (Not all CPs have this section; defensive.)"""
    from cp_engine.sync import _ensure_quick_resume_markers

    body = "# Some project\n\n## Something else\n\nbody\n"
    assert _ensure_quick_resume_markers(body) == body


def test_new_project_scaffold_includes_quick_resume_markers(tmp_path: Path) -> None:
    """Fresh project scaffold (first sync after a project lands in MC-2)
    carries the `quick-resume` markers from the template. New projects
    don't need the migration wrap — markers are there from the start."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="new", name="Brand New", status="Open"),)
        ),
    )

    # Dir is the slugified code (the name no longer contributes a tail).
    cp_path = tmp_path / "1p" / "google" / "new" / "cp.md"
    body = cp_path.read_text()
    assert "<!-- cp-engine:start quick-resume -->" in body
    assert "<!-- cp-engine:end quick-resume -->" in body
    # The four scaffolded lines live inside the region.
    start_pos = body.find("<!-- cp-engine:start quick-resume -->")
    end_pos = body.find("<!-- cp-engine:end quick-resume -->")
    region_body = body[start_pos:end_pos]
    assert "**Last session:**" in region_body
    assert "**Current work:**" in region_body
    assert "**Next up:**" in region_body
    assert "**Blockers:**" in region_body


def test_sync_wraps_quick_resume_in_existing_project_cp(tmp_path: Path) -> None:
    """Integration: an unwrapped Quick Resume in a pre-existing project
    cp.md gets wrapped on next sync. Content unchanged; markers added."""
    from cp_engine.state import dir_slug

    config = make_config(tmp_path)
    project = make_state(code="peb", name="Pebble Foods", status="Open")
    slug = dir_slug(project.code, project.name)
    # Pre-create the project working dir + cp.md with the OLD shape
    # (no quick-resume markers).
    project_dir = tmp_path / "1p" / "google" / slug
    project_dir.mkdir(parents=True)
    cp_path = project_dir / "cp.md"
    legacy = (
        "# Pebble Foods — Project CP\n\n"
        "<!-- cp-engine:start project-facts -->\n"
        "## Facts\n"
        "<!-- cp-engine:end project-facts -->\n\n"
        "<!-- cp-engine:start tracked-issues -->\n"
        "## Tracked issues\n"
        "<!-- cp-engine:end tracked-issues -->\n\n"
        "## Quick Resume\n\n"
        "**Last session:** _<date>_\n"
        "**Current work:** Tony shipping playbooks.\n"
        "**Next up:** _<next 1-3 concrete actions>_\n"
        "**Blockers:** _<or \"None\">_\n\n"
        "## Current Work\n\n"
        "_<2-10 paragraphs>_\n"
    )
    cp_path.write_text(legacy)

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((project,)),
    )

    body = cp_path.read_text()
    assert "<!-- cp-engine:start quick-resume -->" in body
    assert "<!-- cp-engine:end quick-resume -->" in body
    # Hand-written `**Current work:**` content preserved verbatim across
    # the wrap — markers are inserted, content is untouched.
    assert "**Current work:** Tony shipping playbooks." in body


# ──────────────────────────────────────────────────────────────────────
#  Fix 5 (v0.15.2): _write_if_changed graceful fallback on splice failure
# ──────────────────────────────────────────────────────────────────────


def test_write_if_changed_falls_back_to_full_rewrite_on_duplicated_marker(
    tmp_path: Path, caplog
) -> None:
    """A duplicated start marker for a region in the existing file used to
    abort the entire sync (MarkerDuplicated propagated out of
    ``splice_managed_region``). After the fix, the splice failure logs a
    warning and falls back to a full rewrite of the new body."""
    import logging

    from cp_engine.sync import _write_if_changed

    target = tmp_path / "master-cp.md"
    # Existing file has TWO `cp-engine:start banner` markers — corrupt.
    target.write_text(
        "# Master\n"
        "<!-- cp-engine:start banner -->\n"
        "old banner one\n"
        "<!-- cp-engine:end banner -->\n"
        "<!-- cp-engine:start banner -->\n"
        "old banner two (duplicate!)\n"
        "<!-- cp-engine:end banner -->\n"
    )
    new_body = (
        "# Master\n"
        "<!-- cp-engine:start banner -->\n"
        "fresh banner\n"
        "<!-- cp-engine:end banner -->\n"
    )

    with caplog.at_level(logging.WARNING, logger="cp_engine.sync"):
        changed = _write_if_changed(
            target, new_body, splice_regions=("banner",)
        )

    assert changed is True
    # Full-rewrite fallback: the new body fully replaces the old (the
    # duplicated marker is gone).
    final = target.read_text()
    assert final == new_body
    assert "fresh banner" in final
    assert "old banner one" not in final
    assert "old banner two" not in final
    # A warning was emitted naming the file + the splice failure.
    assert any(
        "splice failed" in rec.message and "master-cp.md" in rec.message
        for rec in caplog.records
    )


def test_write_if_changed_no_op_on_duplicated_marker_when_content_matches(
    tmp_path: Path,
) -> None:
    """If the duplicated-marker file ALREADY equals the new body verbatim,
    the fallback returns False (no write). Keeps the no-op-resync property
    even in the corrupted-marker case."""
    from cp_engine.sync import _write_if_changed

    target = tmp_path / "master-cp.md"
    body = (
        "# Master\n"
        "<!-- cp-engine:start banner -->\n"
        "x\n"
        "<!-- cp-engine:end banner -->\n"
        "<!-- cp-engine:start banner -->\n"
        "y\n"
        "<!-- cp-engine:end banner -->\n"
    )
    target.write_text(body)

    changed = _write_if_changed(
        target, body, splice_regions=("banner",)
    )
    assert changed is False


# ──────────────────────────────────────────────────────────────────────
#  _collect_sprint_per_project_data — recent_commits aggregation
# ──────────────────────────────────────────────────────────────────────


def _init_git_repo(path: Path) -> None:
    """Create a fresh git repo at `path` with deterministic config."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", "--quiet"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True,
    )


def _git_commit(
    path: Path,
    *,
    subject: str,
    author: str = "Test Author",
    when: str = "2026-06-01T12:00:00",
) -> None:
    """Create an empty commit with controlled author + date."""
    env = {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": "author@example.com",
        "GIT_COMMITTER_DATE": when,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        # Keep PATH so git can find its helpers.
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(path.parent),
    }
    subprocess.run(
        ["git", "commit", "--allow-empty", "--quiet", "-m", subject],
        cwd=path, check=True, env=env,
    )


def _config_with_local_repos(
    tmp_path: Path, local_repos: dict[str, Path]
) -> TenantConfig:
    """Build a TenantConfig anchored at `tmp_path` with a local_repos map."""
    from types import MappingProxyType
    return TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref",
        ),
        projects=(),
        root=tmp_path,
        local_repos=MappingProxyType(dict(local_repos)),
    )


def test_collect_sprint_per_project_data_returns_commits_from_local_repo(
    tmp_path: Path,
) -> None:
    """Three real commits in a local clone surface as SprintCommits."""
    repo = tmp_path / "foo"
    _init_git_repo(repo)
    _git_commit(repo, subject="first", author="Alice", when="2026-06-01T09:00:00")
    _git_commit(repo, subject="second", author="Bob",   when="2026-06-01T10:00:00")
    _git_commit(repo, subject="third",  author="Carol", when="2026-06-01T11:00:00")

    config = _config_with_local_repos(tmp_path, {"foo": repo})
    projects = (make_state(code="foo", name="foo"),)

    result = _collect_sprint_per_project_data(
        config, projects, (), sprint_start=date(2026, 6, 1),
    )

    assert "foo" in result
    commits = result["foo"]["recent_commits"]
    assert len(commits) == 3
    # git log --pretty=format walks newest → oldest.
    subjects = [c.subject for c in commits]
    assert subjects == ["third", "second", "first"]
    authors = [c.author for c in commits]
    assert authors == ["Carol", "Bob", "Alice"]
    for c in commits:
        assert c.when_short == "2026-06-01"
        assert len(c.sha_short) >= 4  # %h gives a short SHA


def test_collect_sprint_per_project_data_skips_projects_without_local_repo(
    tmp_path: Path,
) -> None:
    """Projects without a local_repos entry are absent from the result."""
    config = _config_with_local_repos(tmp_path, {})  # empty
    projects = (
        make_state(code="foo", name="foo"),
        make_state(code="bar", name="bar"),
    )

    result = _collect_sprint_per_project_data(
        config, projects, (), sprint_start=date(2026, 6, 1),
    )
    assert result == {}


def test_collect_sprint_per_project_data_skips_commits_before_sprint_start(
    tmp_path: Path,
) -> None:
    """Commits dated before sprint_start are excluded from `recent_commits`."""
    repo = tmp_path / "foo"
    _init_git_repo(repo)
    # One way before, two in-window.
    _git_commit(repo, subject="old",      when="2026-05-10T09:00:00")
    _git_commit(repo, subject="in-1",     when="2026-06-01T12:00:00")
    _git_commit(repo, subject="in-2",     when="2026-06-02T12:00:00")

    config = _config_with_local_repos(tmp_path, {"foo": repo})
    projects = (make_state(code="foo", name="foo"),)

    result = _collect_sprint_per_project_data(
        config, projects, (), sprint_start=date(2026, 6, 1),
    )
    subjects = [c.subject for c in result["foo"]["recent_commits"]]
    assert subjects == ["in-2", "in-1"]
    assert "old" not in subjects


def test_collect_sprint_per_project_data_caps_at_20_commits(
    tmp_path: Path,
) -> None:
    """30 in-window commits → cap at 20 in the result."""
    repo = tmp_path / "foo"
    _init_git_repo(repo)
    for i in range(30):
        _git_commit(
            repo,
            subject=f"commit-{i:02d}",
            when=f"2026-06-01T{i % 24:02d}:00:00",
        )

    config = _config_with_local_repos(tmp_path, {"foo": repo})
    projects = (make_state(code="foo", name="foo"),)

    result = _collect_sprint_per_project_data(
        config, projects, (), sprint_start=date(2026, 6, 1),
    )
    assert len(result["foo"]["recent_commits"]) == 20


def test_collect_sprint_per_project_data_handles_missing_clone_gracefully(
    tmp_path: Path,
) -> None:
    """A nonexistent path in local_repos is skipped, not an exception."""
    bogus = tmp_path / "does-not-exist"
    config = _config_with_local_repos(tmp_path, {"bar": bogus})
    projects = (make_state(code="bar", name="bar"),)

    result = _collect_sprint_per_project_data(
        config, projects, (), sprint_start=date(2026, 6, 1),
    )
    assert "bar" not in result
    assert result == {}


def test_collect_sprint_per_project_data_handles_unicode_subject(
    tmp_path: Path,
) -> None:
    """Non-ASCII subjects round-trip cleanly through git log → SprintCommit."""
    repo = tmp_path / "foo"
    _init_git_repo(repo)
    _git_commit(
        repo,
        subject="ship 🚀 résumé update — café",
        when="2026-06-01T09:00:00",
    )

    config = _config_with_local_repos(tmp_path, {"foo": repo})
    projects = (make_state(code="foo", name="foo"),)

    result = _collect_sprint_per_project_data(
        config, projects, (), sprint_start=date(2026, 6, 1),
    )
    commits = result["foo"]["recent_commits"]
    assert len(commits) == 1
    assert "🚀" in commits[0].subject
    assert "résumé" in commits[0].subject
    assert "café" in commits[0].subject


# --- _read_mc_id (uuid stamp reader) ---------------------------------------

_CP_MD_TEMPLATE = (
    "---\n"
    "Project: Acme Thing\n"
    "Provenance: Version 0.35.1 | 2026-06-20\n"
    "Filename: cp.md\n"
    "{mc_id_line}"
    "Author: cp-engine (initial scaffold)\n"
    "---\n"
    "\n"
    "# Acme Thing — Project CP\n"
)


def test_read_mc_id_returns_stamp(tmp_path: Path) -> None:
    cp = tmp_path / "cp.md"
    cp.write_text(_CP_MD_TEMPLATE.format(mc_id_line="MC-id: abc-uuid\n"))
    assert _read_mc_id(cp) == "abc-uuid"


def test_read_mc_id_none_when_frontmatter_has_no_stamp(tmp_path: Path) -> None:
    cp = tmp_path / "cp.md"
    cp.write_text(_CP_MD_TEMPLATE.format(mc_id_line=""))
    assert _read_mc_id(cp) is None


def test_read_mc_id_none_when_file_missing(tmp_path: Path) -> None:
    assert _read_mc_id(tmp_path / "does-not-exist.md") is None


def test_read_mc_id_none_when_no_frontmatter_block(tmp_path: Path) -> None:
    cp = tmp_path / "cp.md"
    cp.write_text("# hello\n\nno frontmatter here.\n")
    assert _read_mc_id(cp) is None


def test_read_mc_id_roundtrips_rendered_template(tmp_path: Path) -> None:
    """Task1 (stamp) + Task2 (read) compose: a cp.md rendered with mc2_id set
    yields that uuid back through _read_mc_id."""
    from dataclasses import replace

    from cp_engine.render import render_project_cp

    config = make_config(tmp_path)
    state = replace(make_state(code="acme-1", name="Acme Thing"), mc2_id="render-uuid-123")
    cp = tmp_path / "cp.md"
    cp.write_text(render_project_cp(config, state))
    assert _read_mc_id(cp) == "render-uuid-123"


# --- _ensure_mc_id_stamp (self-healing backfill) ---------------------------

# A legacy cp.md: real frontmatter (NO MC-id) + engine regions + hand content.
_LEGACY_CP_MD = (
    "---\n"
    "Project: Acme Thing\n"
    "Provenance: Version 0.20.0 | 2026-01-01\n"
    "Filename: cp.md\n"
    "Author: cp-engine (initial scaffold)\n"
    "---\n"
    "\n"
    "# Acme Thing — Project CP\n"
    "\n"
    "<!-- cp-engine:start project-facts -->\n"
    "| Code | acme-1 |\n"
    "<!-- cp-engine:end project-facts -->\n"
    "\n"
    "## My notes\n"
    "\n"
    "Hand-written content that MUST survive byte-for-byte.\n"
    "Second line, with trailing spaces here:   \n"
)


def test_ensure_mc_id_stamp_stamps_legacy_cp(tmp_path: Path) -> None:
    """A legacy cp.md (no MC-id) gets the stamp after `Filename:`; the entire
    body (engine regions + hand notes) is preserved byte-for-byte."""
    cp = tmp_path / "cp.md"
    cp.write_text(_LEGACY_CP_MD)
    body_before = _LEGACY_CP_MD.split("\n---\n", 1)[1]

    assert _ensure_mc_id_stamp(cp, "U") is True

    text = cp.read_text()
    # Stamp placed immediately after the Filename line.
    assert "Filename: cp.md\nMC-id: U\n" in text
    assert _read_mc_id(cp) == "U"
    # Body after the closing `---` unchanged, byte-for-byte.
    assert text.split("\n---\n", 1)[1] == body_before


def test_ensure_mc_id_stamp_idempotent(tmp_path: Path) -> None:
    """Calling again when the stamp is already correct → no write, byte-identical."""
    cp = tmp_path / "cp.md"
    cp.write_text(_LEGACY_CP_MD)
    _ensure_mc_id_stamp(cp, "U")
    after_first = cp.read_text()

    assert _ensure_mc_id_stamp(cp, "U") is False
    assert cp.read_text() == after_first


def test_ensure_mc_id_stamp_replaces_wrong_value(tmp_path: Path) -> None:
    """An existing MC-id with the wrong value is replaced in place; only that
    one line changes, body untouched."""
    cp = tmp_path / "cp.md"
    stamped_old = _LEGACY_CP_MD.replace(
        "Filename: cp.md\n", "Filename: cp.md\nMC-id: OLD\n"
    )
    cp.write_text(stamped_old)
    body_before = stamped_old.split("\n---\n", 1)[1]

    assert _ensure_mc_id_stamp(cp, "NEW") is True

    text = cp.read_text()
    assert "MC-id: NEW\n" in text
    assert "MC-id: OLD" not in text
    assert text.split("\n---\n", 1)[1] == body_before
    # Exactly one line differs.
    diff = [
        (a, b)
        for a, b in zip(stamped_old.splitlines(), text.splitlines())
        if a != b
    ]
    assert diff == [("MC-id: OLD", "MC-id: NEW")]


def test_ensure_mc_id_stamp_no_filename_line(tmp_path: Path) -> None:
    """Frontmatter without a Filename line → MC-id still inserted (as the last
    frontmatter line before the closing `---`), body preserved."""
    no_filename = (
        "---\n"
        "Project: Acme Thing\n"
        "Author: cp-engine\n"
        "---\n"
        "\n"
        "# body\n"
        "## My notes\n"
        "keep me\n"
    )
    cp = tmp_path / "cp.md"
    cp.write_text(no_filename)
    body_before = no_filename.split("\n---\n", 1)[1]

    assert _ensure_mc_id_stamp(cp, "U") is True

    text = cp.read_text()
    assert _read_mc_id(cp) == "U"
    # Inserted as last frontmatter line (after Author, before closing ---).
    assert text.startswith(
        "---\nProject: Acme Thing\nAuthor: cp-engine\nMC-id: U\n---\n"
    )
    assert text.split("\n---\n", 1)[1] == body_before


def test_ensure_mc_id_stamp_no_frontmatter_returns_false(tmp_path: Path) -> None:
    """A file with no parseable frontmatter is left entirely untouched."""
    cp = tmp_path / "cp.md"
    cp.write_text("# hello\n\nno frontmatter.\n")
    before = cp.read_text()
    assert _ensure_mc_id_stamp(cp, "U") is False
    assert cp.read_text() == before


def test_sync_backfills_mc_id_into_existing_unstamped_cp(tmp_path: Path) -> None:
    """Self-healing path: an existing unstamped cp.md for a live project (mc2_id
    set) gets the stamp after a full sync, and hand content survives."""
    from dataclasses import replace

    config = make_config(tmp_path)
    code = "acme-legacy"
    # First sync scaffolds the dir, THEN we strip the stamp + add hand content
    # to simulate a pre-feature legacy cp.md on disk.
    state = replace(make_state(code=code, name=code), mc2_id="U-backfill")
    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))
    cp = tmp_path / "1p" / "google" / code / "cp.md"
    legacy = cp.read_text().replace("MC-id: U-backfill\n", "")
    legacy += "\n## My notes\n\nhand content survives\n"
    cp.write_text(legacy)
    assert _read_mc_id(cp) is None  # genuinely unstamped now

    # Re-sync → the backfill stamps it.
    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    assert _read_mc_id(cp) == "U-backfill"
    assert "## My notes\n\nhand content survives\n" in cp.read_text()


def test_sync_skips_mc_id_stamp_for_uuidless_repo(tmp_path: Path) -> None:
    """A project with mc2_id=None (e.g. a standalone repo) → no stamp attempt,
    no error; its cp.md carries no MC-id."""
    config = make_config(tmp_path)
    state = make_state(code="cp-engine", name="cp-engine")  # mc2_id defaults None
    assert state.mc2_id is None
    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))
    cp = tmp_path / "1p" / "google" / "cp-engine" / "cp.md"
    assert cp.exists()
    assert _read_mc_id(cp) is None


# --- _find_project_dir (uuid-first lookup) ---------------------------------


def _make_dir_with_cp(parent: Path, name: str, mc_id: str | None = None) -> Path:
    """Create parent/name/cp.md with a real frontmatter block, optional MC-id."""
    d = parent / name
    d.mkdir(parents=True)
    mc_id_line = f"MC-id: {mc_id}\n" if mc_id else ""
    (d / "cp.md").write_text(_CP_MD_TEMPLATE.format(mc_id_line=mc_id_line))
    return d


def test_find_project_dir_by_uuid_despite_name_mismatch(tmp_path: Path) -> None:
    """A dir whose name no longer matches the code is still found via its
    MC-id stamp — drift becomes a rename, not an orphan."""
    drifted = _make_dir_with_cp(tmp_path, "oldname", mc_id="U")
    assert _find_project_dir(tmp_path, code="new-code", mc2_id="U") == drifted


def test_find_project_dir_fallback_for_unstamped_dir(tmp_path: Path) -> None:
    """An unstamped dir is found by the legacy code match even when a uuid is
    passed (uuid pass misses, fallback hits)."""
    legacy = _make_dir_with_cp(tmp_path, "ggl-5168-activation", mc_id=None)
    assert (
        _find_project_dir(tmp_path, "ggl-5168-activation", mc2_id="U") == legacy
    )


def test_find_project_dir_none_uuid_is_legacy_behavior(tmp_path: Path) -> None:
    """mc2_id=None → only code match (bare + slugged), no uuid pass."""
    bare = tmp_path / "abc"
    bare.mkdir()
    slugged = tmp_path / "xyz-thing"
    slugged.mkdir()
    assert _find_project_dir(tmp_path, "abc") == bare
    assert _find_project_dir(tmp_path, "xyz") == slugged
    assert _find_project_dir(tmp_path, "missing") is None


def test_find_project_dir_uuid_beats_code_sibling(tmp_path: Path) -> None:
    """uuid-first: a stamped dir (under a different name) wins over a same-parent
    dir whose name matches the code but carries no stamp."""
    stamped = _make_dir_with_cp(tmp_path, "aaa", mc_id="U")
    (tmp_path / "new-code").mkdir()  # name matches code, but no stamp
    assert _find_project_dir(tmp_path, code="new-code", mc2_id="U") == stamped


# ──────────────────────────────────────────────────────────────────────
#  _deactivate_stale_cps — uuid-aware staleness (don't sweep drifted dirs)
# ──────────────────────────────────────────────────────────────────────


def _client_parent(tmp_path: Path) -> Path:
    """The per-account live parent dir for a Google client project
    (`1p/google/`). Must exist for the deactivation sweep to scan it."""
    parent = tmp_path / "1p" / "google"
    parent.mkdir(parents=True)
    return parent


def test_deactivate_keeps_drifted_dir_recognised_by_uuid(tmp_path: Path) -> None:
    """Core fix: a dir whose name matches no live code but whose cp.md
    is stamped with a live uuid is NOT swept to inactive/."""
    parent = _client_parent(tmp_path)
    drifted = _make_dir_with_cp(parent, "oldslug", mc_id="U")
    # Live set: new code + the same uuid U (oldslug matches no live code).
    live_dirs = {("1p/google", "ggl-new-code", "U")}

    moved = _deactivate_stale_cps(tmp_path, live_dirs)

    assert moved == []
    assert drifted.exists()
    assert not (parent / "inactive" / "oldslug").exists()


def test_deactivate_sweeps_genuinely_gone_uuid_and_name(tmp_path: Path) -> None:
    """A dir whose stamped uuid is NOT in the live set AND whose name
    matches no live code IS moved to inactive/."""
    parent = _client_parent(tmp_path)
    gone = _make_dir_with_cp(parent, "going-away", mc_id="DEAD-UUID")
    live_dirs = {("1p/google", "ggl-still-here", "LIVE-UUID")}

    moved = _deactivate_stale_cps(tmp_path, live_dirs)

    assert not gone.exists()
    target = parent / "inactive" / "going-away"
    assert target.exists()
    assert moved == [target]


def test_deactivate_sweeps_unstamped_stale_dir(tmp_path: Path) -> None:
    """Fallback unchanged: a dir with NO MC-id and a name matching no live
    code is still swept (legacy behaviour)."""
    parent = _client_parent(tmp_path)
    stale = _make_dir_with_cp(parent, "no-stamp-stale", mc_id=None)
    live_dirs = {("1p/google", "ggl-live", "LIVE-UUID")}

    moved = _deactivate_stale_cps(tmp_path, live_dirs)

    assert not stale.exists()
    assert (parent / "inactive" / "no-stamp-stale").exists()
    assert moved == [parent / "inactive" / "no-stamp-stale"]


def test_deactivate_keeps_unstamped_dir_matching_live_code(tmp_path: Path) -> None:
    """Fallback unchanged: a dir with NO MC-id but a name matching a live
    code (bare or slugged) is kept."""
    parent = _client_parent(tmp_path)
    bare = _make_dir_with_cp(parent, "ggl-5168", mc_id=None)
    slugged = _make_dir_with_cp(parent, "ggl-5177-activation", mc_id=None)
    live_dirs = {
        ("1p/google", "ggl-5168", "U1"),
        ("1p/google", "ggl-5177", "U2"),
    }

    moved = _deactivate_stale_cps(tmp_path, live_dirs)

    assert moved == []
    assert bare.exists()
    assert slugged.exists()


def test_full_sync_does_not_sweep_drifted_dir(tmp_path: Path) -> None:
    """End-to-end: a project whose code drifts (same mc2_id) is renamed in
    place, NOT swept to inactive/, even though its old-named dir matches no
    live code at the moment the deactivation sweep runs."""
    from dataclasses import replace

    config = make_config(tmp_path)
    old = replace(make_state(code="ggl-5177-old", name="ggl-5177-old"), mc2_id="U-x")
    sync_tenant(config, backend_factory=lambda _: FakeBackend((old,)))
    old_dir = tmp_path / "1p" / "google" / "ggl-5177-old"
    assert (old_dir / "cp.md").exists()

    # Re-sync: same uuid, drifted code.
    new = replace(make_state(code="ggl-5177-new", name="ggl-5177-new"), mc2_id="U-x")
    sync_tenant(config, backend_factory=lambda _: FakeBackend((new,)))

    # Dir was renamed, content preserved, and nothing landed in inactive/.
    assert not old_dir.exists()
    assert (tmp_path / "1p" / "google" / "ggl-5177-new" / "cp.md").exists()
    assert not (tmp_path / "1p" / "google" / "inactive" / "ggl-5177-old").exists()


# ──────────────────────────────────────────────────────────────────────
#  _rename_sprint_files (sprint files follow a dir drift rename)
# ──────────────────────────────────────────────────────────────────────


def test_rename_sprint_files_renames_all_weeks(tmp_path: Path) -> None:
    """The helper renames `sprints/*/<old>.md` → `<new>.md` across every
    week, preserving content, returns the new paths, leaves no old file,
    and never touches an unrelated sprint file."""
    for week, body in (("2026-W01", "w1 content"), ("2026-W02", "w2 content")):
        d = tmp_path / "sprints" / week
        d.mkdir(parents=True)
        (d / "old.md").write_text(body)
    # An unrelated sprint file that must stay put.
    (tmp_path / "sprints" / "2026-W01" / "other.md").write_text("untouched")

    new_paths = _rename_sprint_files(tmp_path, "old", "new")

    assert len(new_paths) == 2
    assert (tmp_path / "sprints" / "2026-W01" / "new.md").read_text() == "w1 content"
    assert (tmp_path / "sprints" / "2026-W02" / "new.md").read_text() == "w2 content"
    assert not (tmp_path / "sprints" / "2026-W01" / "old.md").exists()
    assert not (tmp_path / "sprints" / "2026-W02" / "old.md").exists()
    # Unrelated file untouched.
    assert (tmp_path / "sprints" / "2026-W01" / "other.md").read_text() == "untouched"


def test_rename_sprint_files_no_files_returns_empty(tmp_path: Path) -> None:
    """No matching sprint files (none scaffolded yet) → returns [], no error."""
    (tmp_path / "sprints").mkdir()
    assert _rename_sprint_files(tmp_path, "old", "new") == []


def _stamped_old_project_tree(
    tmp_path: Path, *, old_code: str, mc_id: str
) -> "TenantConfig":
    """First-sync a project under `old_code` (stamping MC-id=<mc_id> into its
    cp.md), then hand-add sprint files for it across two weeks. Returns the
    config to re-sync with a drifted code."""
    config = make_config(tmp_path)
    from dataclasses import replace

    state = replace(
        make_state(code=old_code, name=old_code),
        mc2_id=mc_id,
    )
    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))
    # Sanity: dir exists and cp.md carries the stamp.
    old_dir = tmp_path / "1p" / "google" / old_code
    assert old_dir.exists()
    assert _read_mc_id(old_dir / "cp.md") == mc_id
    # Hand-add sprint files for OLD weeks (well before the current sync week
    # so they aren't pulled in as the prior-sprint for carry-forward, which
    # would try to parse our minimal bodies as full sprint files). The first
    # sync also scaffolds the current-week file; these past-week ones are what
    # we assert content on.
    for week, body in (("2026-W10", "old sprint W10"), ("2026-W11", "old sprint W11")):
        d = tmp_path / "sprints" / week
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{old_code}.md").write_text(body)
    return config


def test_drift_rename_moves_sprint_files(tmp_path: Path) -> None:
    """When a code/slug drift renames the working dir, the project's sprint
    files are renamed to the new code too — old-named files vanish, new-named
    files carry the original content."""
    old_code = "ggl-5177-old-slug"
    new_code = "ggl-5177-new-slug"
    config = _stamped_old_project_tree(tmp_path, old_code=old_code, mc_id="U-drift")

    # Re-sync with the SAME mc2_id but a drifted code → uuid-anchored rename.
    from dataclasses import replace

    new_state = replace(make_state(code=new_code, name=new_code), mc2_id="U-drift")
    sync_tenant(config, backend_factory=lambda _: FakeBackend((new_state,)))

    # Dir moved.
    assert (tmp_path / "1p" / "google" / new_code).exists()
    assert not (tmp_path / "1p" / "google" / old_code).exists()

    # Sprint files moved with content preserved.
    for week, body in (("2026-W10", "old sprint W10"), ("2026-W11", "old sprint W11")):
        new_sf = tmp_path / "sprints" / week / f"{new_code}.md"
        old_sf = tmp_path / "sprints" / week / f"{old_code}.md"
        assert new_sf.read_text() == body
        assert not old_sf.exists()


def test_e2e_full_job_name_edit_uuid_anchored_drift(tmp_path: Path) -> None:
    """End-to-end proof of the whole UUID-anchored fix, simulating the real
    `full_job_name` edit gotcha:

    1. First sync lands a project at `ggl-9001-old-name/` with a stamped cp.md.
    2. Hand content (research.md) + a sprint file are added under the old name.
    3. The full_job_name is edited → second sync sees the SAME mc2_id but a new
       code/slug (`ggl-9001-new-name`).
    4. The dir + sprint file are renamed (found by uuid), hand content survives,
       NOTHING is swept to inactive/, and the stamp persists.
    """
    from dataclasses import replace

    old_code = "ggl-9001-old-name"
    new_code = "ggl-9001-new-name"
    config = make_config(tmp_path)

    # 1. Initial sync — project lands at its old-named working dir, stamped.
    old_state = replace(make_state(code=old_code, name=old_code), mc2_id="U-e2e")
    sync_tenant(config, backend_factory=lambda _: FakeBackend((old_state,)))
    old_dir = tmp_path / "1p" / "google" / old_code
    assert (old_dir / "cp.md").exists()
    assert _read_mc_id(old_dir / "cp.md") == "U-e2e"

    # 2. Hand content + a sprint file under the old name. Use a past week for the
    #    sprint file so it isn't pulled in as the prior-sprint carry-forward
    #    source (whose minimal body would fail to parse as a real sprint file).
    (old_dir / "research.md").write_text("important notes")
    sprint_week = "2026-W10"
    sprint_dir = tmp_path / "sprints" / sprint_week
    sprint_dir.mkdir(parents=True, exist_ok=True)
    (sprint_dir / f"{old_code}.md").write_text("old sprint body")

    # 3. The full_job_name edit: same uuid, new code/slug.
    new_state = replace(make_state(code=new_code, name=new_code), mc2_id="U-e2e")
    sync_tenant(config, backend_factory=lambda _: FakeBackend((new_state,)))

    new_dir = tmp_path / "1p" / "google" / new_code

    # 4a. Working dir renamed (found by uuid), old dir gone.
    assert (new_dir / "cp.md").exists()
    assert not old_dir.exists()
    # 4b. Old dir was NOT swept to inactive/ (uuid-aware staleness check).
    assert not (tmp_path / "1p" / "google" / "inactive" / old_code).exists()
    # 4c. Hand content rode along, content intact.
    assert (new_dir / "research.md").read_text() == "important notes"
    # 4d. Sprint file renamed to the new code, content intact, old one gone.
    assert (sprint_dir / f"{new_code}.md").read_text() == "old sprint body"
    assert not (sprint_dir / f"{old_code}.md").exists()
    # 4e. The stamp survives the rename.
    assert _read_mc_id(new_dir / "cp.md") == "U-e2e"


def test_drift_rename_with_no_sprint_files_is_fine(tmp_path: Path) -> None:
    """Drift rename when the project has no hand-added sprint files at the old
    name still renames the dir cleanly (helper returns [])."""
    config = make_config(tmp_path)
    from dataclasses import replace

    old_state = replace(
        make_state(code="ggl-5177-old", name="ggl-5177-old"), mc2_id="U-empty"
    )
    sync_tenant(config, backend_factory=lambda _: FakeBackend((old_state,)))
    old_dir = tmp_path / "1p" / "google" / "ggl-5177-old"
    assert old_dir.exists()
    # Remove any auto-scaffolded current-week sprint file so there are NO
    # old-named sprint files to move.
    for sf in (tmp_path / "sprints").glob("*/ggl-5177-old.md"):
        sf.unlink()

    new_state = replace(
        make_state(code="ggl-5177-new", name="ggl-5177-new"), mc2_id="U-empty"
    )
    sync_tenant(config, backend_factory=lambda _: FakeBackend((new_state,)))

    assert (tmp_path / "1p" / "google" / "ggl-5177-new").exists()
    assert not (tmp_path / "1p" / "google" / "ggl-5177-old").exists()
