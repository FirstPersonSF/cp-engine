"""Tests for `cp_engine.sync` orchestration.

Tests inject a fake Backend instead of hitting MC-2 — these tests are
about the orchestration layer (read → render → splice → write), not the
backend.
"""

from __future__ import annotations

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
from cp_engine.sync import Backend, _last_week_monday


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
    name: str | None = None,
    status: str = "Open",
    is_internal: bool = False,
    summary: str | None = None,
    source: str = "engagement",
    company_kind: str = "client",
) -> ProjectState:
    """Default name == code so the working-dir slug equals the bare code,
    keeping path assertions in older tests simple. Tests that need to
    exercise the name-slug path pass `name="Some Real Name"` explicitly."""
    return ProjectState(
        code=code,
        name=name if name is not None else code,
        source=source,  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code="GGL",
        company_name="Google",
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
    # v0.3 layout: project working dir at <scope>/<dir_slug>/cp.md.
    # Default make_state has company_kind="client" → scope "1p". With name
    # set, the slug is `mc-2-mission-control-v2`.
    assert written_names == {"master-cp.md", "CLAUDE.md", ".gitignore", "cp.md"}

    # Files actually exist + reference the project
    master = (tmp_path / "master-cp.md").read_text()
    assert "mc-2" in master
    assert "Mission Control v2" in master

    cp_path = tmp_path / "1p" / "mc-2-mission-control-v2" / "cp.md"
    project_cp = cp_path.read_text()
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
    files = ["master-cp.md", "CLAUDE.md", "1p/mc-2/cp.md"]
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

    project_cp_before = (tmp_path / "1p" / "mc-2" / "cp.md").read_text()
    project_cp_mtime_before = (tmp_path / "1p" / "mc-2" / "cp.md").stat().st_mtime_ns

    fake2 = FakeBackend((make_state(status="Holding"),))
    result = sync_tenant(config, backend_factory=lambda _: fake2)

    # master-cp.md changed; the project CP did NOT (still scaffolded, untouched)
    assert (tmp_path / "master-cp.md") in result.files_written
    assert (tmp_path / "1p" / "mc-2" / "cp.md") not in result.files_written

    project_cp_after = (tmp_path / "1p" / "mc-2" / "cp.md").read_text()
    assert project_cp_before == project_cp_after
    assert (tmp_path / "1p" / "mc-2" / "cp.md").stat().st_mtime_ns == project_cp_mtime_before


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
    project_path = tmp_path / "1p" / "mc-2" / "cp.md"
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
    # own (cp-firstpersonsf) tenant. v0.3: each project is a working dir
    # under <scope>/<code>-<slugified-name>/.
    scope_dir = tmp_path / "1p"
    dirs = sorted(p.name for p in scope_dir.iterdir() if p.is_dir())
    assert dirs == ["closed-1-closed-one", "hold-1-held-one", "open-1-open-one"]


# ──────────────────────────────────────────────────────────────────────
#  Archive sweep — projects that drop out of sync's view
# ──────────────────────────────────────────────────────────────────────


def test_archived_project_cp_moves_to_inactive_dir(tmp_path: Path) -> None:
    """A project that disappears from sync output (archived in MC-2)
    has its working dir moved to <scope>/inactive/<code>/, not deleted."""
    config = make_config(tmp_path)

    # First sync: project exists
    fake1 = FakeBackend((make_state(code="going-away"),))
    sync_tenant(config, backend_factory=lambda _: fake1)
    live_dir = tmp_path / "1p" / "going-away"
    assert (live_dir / "cp.md").exists()

    # Second sync: project is gone (e.g. archived in MC-2)
    fake2 = FakeBackend(())
    result = sync_tenant(config, backend_factory=lambda _: fake2)

    assert not live_dir.exists()
    inactive_dir = tmp_path / "1p" / "inactive" / "going-away"
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

    # User adds notes to the project CP and drops a transcript file alongside
    work_dir = tmp_path / "1p" / "my-project"
    cp_path = work_dir / "cp.md"
    edited = cp_path.read_text() + "\n## My notes\n\nImportant stuff.\n"
    cp_path.write_text(edited)
    (work_dir / "transcript.md").write_text("# 2026-05-08 call\n\n…\n")

    # Project disappears from MC-2
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))

    archived = tmp_path / "1p" / "inactive" / "my-project"
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

    # Archive
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    archived = tmp_path / "1p" / "inactive" / "dead-project" / "cp.md"
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
    # Pre-populate an existing archived working dir
    archived_old = tmp_path / "1p" / "inactive" / "ghost"
    archived_old.mkdir(parents=True)
    (archived_old / "cp.md").write_text("# Old archive\n\nFrom an earlier life.\n")

    # And a current live working dir for the same code
    live_dir = tmp_path / "1p" / "ghost"
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

    # First sync creates 1p/keep/
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="keep"),)),
    )
    # Manually create inactive/ with an unrelated entry
    inactive_root = tmp_path / "1p" / "inactive"
    inactive_root.mkdir(parents=True, exist_ok=True)
    (inactive_root / "previous").mkdir(exist_ok=True)
    (inactive_root / "previous" / "cp.md").write_text("# Old\n")

    # Re-sync with `keep` still alive — inactive/ should not be touched
    result = sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend((make_state(code="keep"),)),
    )
    assert (tmp_path / "1p" / "keep" / "cp.md").exists()
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
            make_state(code="ggl-5168", name="Playbooks", company_kind="client"),
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

    # Working dirs use slugged names (code + slugified project name).
    assert (tmp_path / "1p" / "ggl-5168-playbooks" / "cp.md").exists()
    assert (tmp_path / "firstpersonsf" / "mc-2" / "cp.md").exists()
    assert (tmp_path / "canonic" / "storyos" / "cp.md").exists()

    # Master CP links use the slugged paths
    master = (tmp_path / "master-cp.md").read_text()
    assert "1p/ggl-5168-playbooks/cp.md" in master
    assert "firstpersonsf/mc-2/cp.md" in master
    assert "canonic/storyos/cp.md" in master


def test_un_archive_restores_working_dir_with_hand_content(tmp_path: Path) -> None:
    """A project that's archived, then re-enters the live set, comes back
    with all its hand-written content (not a fresh scaffold)."""
    config = make_config(tmp_path)
    state = make_state(code="resurrected", name="Resurrected")

    # First sync: live
    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))
    work_dir = tmp_path / "1p" / "resurrected"

    # Hand-add a transcript and edit cp.md
    (work_dir / "transcript-2026-05-08.md").write_text("# Call notes\n\nSecret sauce.\n")
    cp_path = work_dir / "cp.md"
    cp_path.write_text(cp_path.read_text() + "\n## Hand notes\n\nKeep me.\n")

    # Project drops out — gets archived
    sync_tenant(config, backend_factory=lambda _: FakeBackend(()))
    assert not work_dir.exists()
    inactive_dir = tmp_path / "1p" / "inactive" / "resurrected"
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
        code="ggl-5168",
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

    dropbox_path = tmp_path / "1p" / "ggl-5168-playbooks" / "_dropbox.md"
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

    dropbox_path = tmp_path / "1p" / "no-dropbox" / "_dropbox.md"
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
    dropbox_path = tmp_path / "1p" / "mover" / "_dropbox.md"
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
#  Working-dir slugs (v0.3.2+)
# ──────────────────────────────────────────────────────────────────────


def test_working_dir_uses_slugged_name(tmp_path: Path) -> None:
    """Engagement with a multi-word name lands at <code>-<slugified-name>/."""
    config = make_config(tmp_path)
    state = make_state(code="ggl-5177", name="GGL 5177 Event Safety Playbook")

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    expected_dir = tmp_path / "1p" / "ggl-5177-event-safety-playbook"
    assert (expected_dir / "cp.md").exists()


def test_working_dir_falls_back_to_bare_code(tmp_path: Path) -> None:
    """When name == code (typical for repos), the slug is just the code."""
    config = make_config(tmp_path)
    state = make_state(code="mc-2", name="mc-2", source="repo", status="Active",
                        company_kind="self-fpsf")

    sync_tenant(config, backend_factory=lambda _: FakeBackend((state,)))

    assert (tmp_path / "firstpersonsf" / "mc-2" / "cp.md").exists()


def test_name_drift_renames_existing_dir(tmp_path: Path) -> None:
    """When MC-2's name changes, next sync renames the working dir to the
    new slug. Hand-written content survives the move."""
    config = make_config(tmp_path)

    # First sync: scaffold at ggl-5177-event-safety-playbook/
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5177", name="GGL 5177 Event Safety Playbook"),)
        ),
    )
    old_dir = tmp_path / "1p" / "ggl-5177-event-safety-playbook"
    assert old_dir.exists()

    # Hand-add a transcript so we can verify content survives the rename
    (old_dir / "transcript.md").write_text("# call notes\n")

    # Sync again with a renamed project
    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5177", name="GGL 5177 Activation Playbook"),)
        ),
    )

    new_dir = tmp_path / "1p" / "ggl-5177-activation-playbook"
    assert new_dir.exists()
    assert (new_dir / "transcript.md").read_text() == "# call notes\n"
    assert not old_dir.exists()


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

    repo_path = tmp_path / "1p" / "ggl-5168" / "_repo.md"
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


def test_legacy_bare_code_dir_renamed_to_slug(tmp_path: Path) -> None:
    """A working dir created at the bare code (legacy v0.3.0/v0.3.1 format)
    gets renamed to the slugged form on the next sync."""
    config = make_config(tmp_path)

    # Pre-create a legacy bare-code dir with hand content
    legacy_dir = tmp_path / "1p" / "ggl-5177"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "cp.md").write_text("# legacy content\n")
    (legacy_dir / "notes.md").write_text("# preserved\n")

    sync_tenant(
        config,
        backend_factory=lambda _: FakeBackend(
            (make_state(code="ggl-5177", name="GGL 5177 Event Safety Playbook"),)
        ),
    )

    new_dir = tmp_path / "1p" / "ggl-5177-event-safety-playbook"
    assert (new_dir / "cp.md").read_text() == "# legacy content\n"
    assert (new_dir / "notes.md").read_text() == "# preserved\n"
    assert not legacy_dir.exists()


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
