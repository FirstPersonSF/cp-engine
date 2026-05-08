"""Tests for `cp_engine.migrate` — the v0.2 → v0.3 layout migration."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cp_engine import (
    ProjectConfig,
    ProjectState,
    SyncConfig,
    TenantConfig,
)
from cp_engine.migrate import MigrationError, migrate_to_v03
from cp_engine.sync import Backend


class FakeBackend(Backend):
    def __init__(self, states: tuple[ProjectState, ...]) -> None:
        self._states = states

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        return self._states


def _make_config(root: Path) -> TenantConfig:
    return TenantConfig(
        name="cp",
        display="Drew + Tony",
        engine_version_constraint="~= 0.3",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=root,
    )


def _make_state(code: str, company_kind: str = "client") -> ProjectState:
    return ProjectState(
        code=code,
        name=f"Project {code}",
        source="engagement",  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code=None,
        company_name=None,
        status="Open",
        is_internal=False,
        owner=None,
        last_touched=datetime(2026, 5, 7, tzinfo=timezone.utc),
        deadline=None,
    )


def _git_init_with_files(root: Path, files: dict[str, str]) -> None:
    """Init a git repo at `root` with `files` (path → content) committed."""
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial v0.2 layout"],
        cwd=root,
        check=True,
        capture_output=True,
    )


# ──────────────────────────────────────────────────────────────────────
#  Happy path
# ──────────────────────────────────────────────────────────────────────


def test_migrate_moves_live_projects_to_scoped_dirs(tmp_path: Path) -> None:
    """Each projects/<code>.md moves to <scope>/projects/<code>/cp.md based on
    the project's company_kind in MC-2."""
    _git_init_with_files(
        tmp_path,
        {
            "projects/ggl-5168.md": "# 5168\n",
            "projects/mc-2.md": "# mc-2\n",
            "projects/storyos.md": "# storyos\n",
        },
    )

    backend = FakeBackend(
        (
            _make_state("ggl-5168", company_kind="client"),
            _make_state("mc-2", company_kind="self-fpsf"),
            _make_state("storyos", company_kind="self-canonic"),
        )
    )

    result = migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    assert (tmp_path / "1p" / "projects" / "ggl-5168" / "cp.md").exists()
    assert (tmp_path / "firstpersonsf" / "projects" / "mc-2" / "cp.md").exists()
    assert (tmp_path / "canonic" / "projects" / "storyos" / "cp.md").exists()
    assert not (tmp_path / "projects" / "ggl-5168.md").exists()
    assert len(result.moved) == 3
    assert result.skipped == ()


def test_migrate_preserves_hand_edited_content(tmp_path: Path) -> None:
    """File content survives the move — git mv is a rename, not a regenerate."""
    body = "# 5168\n\n## Hand-written notes\n\nImportant stuff.\n"
    _git_init_with_files(tmp_path, {"projects/ggl-5168.md": body})

    backend = FakeBackend((_make_state("ggl-5168"),))
    migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    moved = tmp_path / "1p" / "projects" / "ggl-5168" / "cp.md"
    assert moved.read_text() == body


def test_migrate_moves_archived_projects(tmp_path: Path) -> None:
    """projects/archived/<code>.md moves to <scope>/projects/archived/<code>/cp.md."""
    _git_init_with_files(
        tmp_path,
        {
            "projects/active.md": "# active\n",
            "projects/archived/old.md": "# old\n",
        },
    )

    backend = FakeBackend(
        (
            _make_state("active"),
            _make_state("old"),  # MC-2 still knows about it; just archived
        )
    )

    result = migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    assert (tmp_path / "1p" / "projects" / "active" / "cp.md").exists()
    assert (tmp_path / "1p" / "projects" / "archived" / "old" / "cp.md").exists()
    assert len(result.moved) == 2


def test_migrate_uses_git_mv_for_rename_detection(tmp_path: Path) -> None:
    """After migration, `git log --follow` should reach across the rename."""
    _git_init_with_files(tmp_path, {"projects/ggl-5168.md": "# 5168\n"})

    backend = FakeBackend((_make_state("ggl-5168"),))
    migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    # The file should be staged as a rename, not as add+delete.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # Format: "R  old -> new" for a rename.
    assert "R  projects/ggl-5168.md -> 1p/projects/ggl-5168/cp.md" in status


# ──────────────────────────────────────────────────────────────────────
#  Safety properties
# ──────────────────────────────────────────────────────────────────────


def test_migrate_refuses_dirty_working_tree(tmp_path: Path) -> None:
    _git_init_with_files(tmp_path, {"projects/ggl-5168.md": "# 5168\n"})
    # Add an uncommitted change
    (tmp_path / "projects" / "ggl-5168.md").write_text("# 5168 dirty\n")

    backend = FakeBackend((_make_state("ggl-5168"),))
    with pytest.raises(MigrationError, match="uncommitted changes"):
        migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)


def test_migrate_refuses_non_git_root(tmp_path: Path) -> None:
    # No git init
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "ggl-5168.md").write_text("# 5168\n")

    backend = FakeBackend((_make_state("ggl-5168"),))
    with pytest.raises(MigrationError, match="not a git working tree"):
        migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)


def test_migrate_skips_files_with_no_mc2_entry(tmp_path: Path) -> None:
    """A file like projects/1pi-admin.md (1PI internal that the design doc
    says we don't migrate) is left in place with a clear skip reason."""
    _git_init_with_files(
        tmp_path,
        {
            "projects/ggl-5168.md": "# 5168\n",
            "projects/1pi-admin.md": "# 1pi admin\n",
        },
    )

    # Backend only knows about ggl-5168 — 1pi-admin has no entry
    backend = FakeBackend((_make_state("ggl-5168"),))
    result = migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    assert (tmp_path / "1p" / "projects" / "ggl-5168" / "cp.md").exists()
    assert (tmp_path / "projects" / "1pi-admin.md").exists()  # still in place
    assert len(result.moved) == 1
    assert len(result.skipped) == 1
    skipped_path, reason = result.skipped[0]
    assert skipped_path.name == "1pi-admin.md"
    assert "no MC-2 entry" in reason


def test_migrate_defaults_archived_unknowns_to_1p(tmp_path: Path) -> None:
    """Archived files predate MC-2's companies table — many have no entry.
    The migration defaults them to the 1p scope (safe historical bucket)
    rather than skipping, so they make it into the new layout."""
    _git_init_with_files(
        tmp_path,
        {
            "projects/archived/5000.md": "# legacy 5000\n",
            "projects/archived/5001.md": "# legacy 5001\n",
        },
    )

    backend = FakeBackend(())  # MC-2 has no record of these archived files
    result = migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    assert (tmp_path / "1p" / "projects" / "archived" / "5000" / "cp.md").exists()
    assert (tmp_path / "1p" / "projects" / "archived" / "5001" / "cp.md").exists()
    assert len(result.moved) == 2
    assert result.skipped == ()


def test_migrate_skips_when_target_exists(tmp_path: Path) -> None:
    """Defensive: if a target path is already populated (someone partially
    migrated), don't overwrite. Skip and surface."""
    _git_init_with_files(
        tmp_path,
        {
            "projects/ggl-5168.md": "# old\n",
            "1p/projects/ggl-5168/cp.md": "# already there\n",
        },
    )

    backend = FakeBackend((_make_state("ggl-5168"),))
    result = migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    # Target unchanged
    assert (tmp_path / "1p" / "projects" / "ggl-5168" / "cp.md").read_text() == "# already there\n"
    # Source still there (couldn't move)
    assert (tmp_path / "projects" / "ggl-5168.md").exists()
    assert len(result.skipped) == 1


def test_migrate_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    _git_init_with_files(tmp_path, {"projects/ggl-5168.md": "# 5168\n"})

    backend = FakeBackend((_make_state("ggl-5168"),))
    result = migrate_to_v03(
        _make_config(tmp_path), backend_factory=lambda _: backend, dry_run=True
    )

    # Nothing actually moved
    assert (tmp_path / "projects" / "ggl-5168.md").exists()
    assert not (tmp_path / "1p").exists()
    # But the result reports what would have moved
    assert len(result.moved) == 1


def test_migrate_removes_empty_old_dirs(tmp_path: Path) -> None:
    _git_init_with_files(
        tmp_path,
        {
            "projects/ggl-5168.md": "# 5168\n",
            "projects/archived/old.md": "# old\n",
        },
    )

    backend = FakeBackend((_make_state("ggl-5168"), _make_state("old")))
    migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    # Old projects/ root and projects/archived/ subdir are gone (empty)
    assert not (tmp_path / "projects").exists()


def test_migrate_idempotent_on_already_migrated_tenant(tmp_path: Path) -> None:
    """Running migration on a tenant that already has v0.3 layout (no v0.2
    files left) is a clean no-op."""
    _git_init_with_files(
        tmp_path, {"1p/projects/ggl-5168/cp.md": "# 5168\n"}
    )

    backend = FakeBackend((_make_state("ggl-5168"),))
    result = migrate_to_v03(_make_config(tmp_path), backend_factory=lambda _: backend)

    assert result.moved == ()
    assert result.skipped == ()
