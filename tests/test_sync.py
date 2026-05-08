"""Tests for `cp_engine.sync` orchestration.

Tests inject a fake Backend instead of hitting MC-2 — these tests are
about the orchestration layer (read → render → splice → write), not the
backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from cp_engine.sync import Backend


class FakeBackend(Backend):
    """In-memory backend that returns whatever ProjectStates we hand it."""

    def __init__(self, states: tuple[ProjectState, ...]) -> None:
        self._states = states
        self.read_calls = 0

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        self.read_calls += 1
        return self._states


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
    name: str = "Mission Control v2",
    status: str = "Open",
    is_internal: bool = False,
    summary: str | None = None,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name,
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
    fake = FakeBackend((make_state(),))

    result = sync_tenant(config, backend_factory=lambda _: fake)

    assert result.projects_seen == 1
    assert not result.no_op
    written_names = {p.name for p in result.files_written}
    assert written_names == {"master-cp.md", "CLAUDE.md", "mc-2.md"}

    # Files actually exist + reference the project
    master = (tmp_path / "master-cp.md").read_text()
    assert "mc-2" in master
    assert "Mission Control v2" in master

    project_cp = (tmp_path / "projects" / "mc-2.md").read_text()
    assert "Mission Control v2" in project_cp
    assert "<!-- cp-engine:start tracked-issues -->" in project_cp


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
    files = ["master-cp.md", "CLAUDE.md", "projects/mc-2.md"]
    mtimes_after_first = {f: (tmp_path / f).stat().st_mtime_ns for f in files}

    second = sync_tenant(config, backend_factory=lambda _: fake, now=fixed_now)
    mtimes_after_second = {f: (tmp_path / f).stat().st_mtime_ns for f in files}

    assert second.no_op
    assert second.files_written == ()
    assert mtimes_after_first == mtimes_after_second


def test_resync_with_changed_status_updates_master_only(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fake1 = FakeBackend((make_state(status="Open"),))
    sync_tenant(config, backend_factory=lambda _: fake1)

    project_cp_before = (tmp_path / "projects" / "mc-2.md").read_text()
    project_cp_mtime_before = (tmp_path / "projects" / "mc-2.md").stat().st_mtime_ns

    fake2 = FakeBackend((make_state(status="Holding"),))
    result = sync_tenant(config, backend_factory=lambda _: fake2)

    # master-cp.md changed; the project CP did NOT (still scaffolded, untouched)
    assert (tmp_path / "master-cp.md") in result.files_written
    assert (tmp_path / "projects" / "mc-2.md") not in result.files_written

    project_cp_after = (tmp_path / "projects" / "mc-2.md").read_text()
    assert project_cp_before == project_cp_after
    assert (tmp_path / "projects" / "mc-2.md").stat().st_mtime_ns == project_cp_mtime_before


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


def test_resync_does_not_overwrite_existing_project_cp(tmp_path: Path) -> None:
    """v0.1 decision A: the mc-2 backend never touches existing project CPs."""
    config = make_config(tmp_path)
    fake = FakeBackend((make_state(),))
    sync_tenant(config, backend_factory=lambda _: fake)

    # User edits the project CP heavily — adds notes, removes default sections
    project_path = tmp_path / "projects" / "mc-2.md"
    custom_body = "# Hand-rewritten\n\nNothing else.\n"
    project_path.write_text(custom_body)

    # Re-sync; project CP must remain exactly as the user wrote it
    sync_tenant(config, backend_factory=lambda _: fake)

    assert project_path.read_text() == custom_body


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

    sync_tenant(config, backend_factory=lambda _: fake)

    master = (tmp_path / "master-cp.md").read_text()
    assert "open-1" in master  # active
    assert "hold-1" in master  # holding subtable
    assert "closed-1" in master  # closed-recent subtable
    assert "internal-1" not in master  # is_internal filtered

    # Project CP scaffolding matches master-CP visibility: internal projects
    # are NOT scaffolded into client/public tenants. They belong in their
    # own (cp-firstpersonsf) tenant.
    projects_dir = tmp_path / "projects"
    files = sorted(p.name for p in projects_dir.iterdir())
    assert files == ["closed-1.md", "hold-1.md", "open-1.md"]


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
