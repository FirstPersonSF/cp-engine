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
#  Archive sweep — projects that drop out of sync's view
# ──────────────────────────────────────────────────────────────────────


def test_archived_project_cp_moves_to_archived_dir(tmp_path: Path) -> None:
    """A project that disappears from sync output (archived in MC-2)
    has its CP moved to projects/archived/, not deleted."""
    config = make_config(tmp_path)

    # First sync: project exists
    fake1 = FakeBackend((make_state(code="going-away"),))
    sync_tenant(config, backend_factory=lambda _: fake1)
    assert (tmp_path / "projects" / "going-away.md").exists()

    # Second sync: project is gone (e.g. archived in MC-2)
    fake2 = FakeBackend(())
    result = sync_tenant(config, backend_factory=lambda _: fake2)

    assert not (tmp_path / "projects" / "going-away.md").exists()
    archived_path = tmp_path / "projects" / "archived" / "going-away.md"
    assert archived_path.exists()
    assert archived_path in result.files_archived
    assert not result.no_op


def test_archive_preserves_hand_edited_content(tmp_path: Path) -> None:
    """Hand-edited content survives the move because we rename, not regenerate."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="my-project"),)),
    )

    # User adds notes to the project CP
    project_path = tmp_path / "projects" / "my-project.md"
    edited = project_path.read_text() + "\n## My notes\n\nImportant stuff.\n"
    project_path.write_text(edited)

    # Project disappears from MC-2
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))

    archived = tmp_path / "projects" / "archived" / "my-project.md"
    assert archived.exists()
    assert "Important stuff." in archived.read_text()


def test_resync_after_archive_is_a_noop_for_that_project(tmp_path: Path) -> None:
    """Once archived, the project's CP stays in archived/ on subsequent syncs."""
    config = make_config(tmp_path)
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="dead-project"),)),
    )

    # Archive
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    archived = tmp_path / "projects" / "archived" / "dead-project.md"
    mtime_after_archive = archived.stat().st_mtime_ns

    # Second post-archive sync — should leave the archived file alone.
    result = sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    assert archived.exists()
    assert archived.stat().st_mtime_ns == mtime_after_archive
    assert result.files_archived == ()


def test_archive_collision_logs_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If projects/archived/<code>.md already exists (e.g. unarchive-then-
    re-archive cycle), the engine logs a warning and leaves both files in
    place rather than silently overwriting."""
    import logging

    config = make_config(tmp_path)
    # Pre-populate an existing archived CP
    archive_dir = tmp_path / "projects" / "archived"
    archive_dir.mkdir(parents=True)
    existing_archived = archive_dir / "ghost.md"
    existing_archived.write_text("# Old archive\n\nFrom an earlier life.\n")

    # And a current live CP for the same code
    (tmp_path / "projects").mkdir(exist_ok=True)
    (tmp_path / "projects" / "ghost.md").write_text("# Current\n\nIn flight.\n")

    # Sync with no projects — engine wants to archive ghost.md but the
    # collision blocks it.
    with caplog.at_level(logging.WARNING, logger="cp_engine.sync"):
        result = sync_tenant(config, backend_factory=lambda _: FakeBackend(()))

    # Both files survive
    assert (tmp_path / "projects" / "ghost.md").exists()
    assert existing_archived.read_text() == "# Old archive\n\nFrom an earlier life.\n"
    # Warning logged
    assert any("ghost.md" in m and "already exists" in m for m in caplog.messages)
    # No file actually moved
    assert result.files_archived == ()


def test_archive_dir_itself_not_archived(tmp_path: Path) -> None:
    """projects/archived/ is a directory, not a CP file. The sweep must not
    confuse it for a stale project."""
    config = make_config(tmp_path)

    # First sync creates projects/keep.md
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="keep"),)),
    )
    # Manually create projects/archived/ with an unrelated file
    (tmp_path / "projects" / "archived").mkdir(exist_ok=True)
    (tmp_path / "projects" / "archived" / "previous.md").write_text("# Old\n")

    # Re-sync with `keep` still alive — archived/ should not be touched
    result = sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="keep"),)),
    )
    assert (tmp_path / "projects" / "keep.md").exists()
    assert (tmp_path / "projects" / "archived" / "previous.md").exists()
    assert result.files_archived == ()


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
