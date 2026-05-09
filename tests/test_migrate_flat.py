"""Tests for cp_engine.migrate_flat (one-shot v0.7 layout migration)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cp_engine.migrate_flat import (
    MigrateFlatError,
    migrate_projects_flat,
)


# ──────────────────────────────────────────────────────────────────────
#  Test helpers
# ──────────────────────────────────────────────────────────────────────


def _git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_tenant(tmp_path: Path) -> Path:
    """Create a fresh git repo with a minimal .cp-engine.toml.

    Returns the tenant root.
    """
    root = tmp_path / "cp"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    (root / ".cp-engine.toml").write_text(
        '[tenant]\nname = "cp"\n\n[engine]\nversion = "~= 0.7"\n'
    )
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


def _scaffold_old_layout(tenant_root: Path, scope: str, projects: list[str]) -> None:
    """Create `<tenant>/<scope>/projects/<project>/cp.md` for each project."""
    for project in projects:
        d = tenant_root / scope / "projects" / project
        d.mkdir(parents=True)
        (d / "cp.md").write_text(f"# {project}\n")
    _git(["add", "-A"], cwd=tenant_root)
    _git(["commit", "-m", f"scaffold {scope}"], cwd=tenant_root)


# ──────────────────────────────────────────────────────────────────────
#  Pre-flight
# ──────────────────────────────────────────────────────────────────────


def test_dirty_tree_aborts(tmp_path: Path) -> None:
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["alpha"])
    (root / "1p" / "projects" / "alpha" / "cp.md").write_text("dirty change")

    with pytest.raises(MigrateFlatError, match="not clean"):
        migrate_projects_flat(root)


def test_idempotent_when_already_migrated(tmp_path: Path) -> None:
    root = _init_tenant(tmp_path)
    # Direct v0.7 layout — no projects/ segment.
    (root / "1p" / "alpha").mkdir(parents=True)
    (root / "1p" / "alpha" / "cp.md").write_text("# alpha")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "v0.7 layout"], cwd=root)

    result = migrate_projects_flat(root)

    assert result.no_op
    assert result.moved_dirs == ()


# ──────────────────────────────────────────────────────────────────────
#  Happy-path moves
# ──────────────────────────────────────────────────────────────────────


def test_moves_single_project(tmp_path: Path) -> None:
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["alpha"])

    result = migrate_projects_flat(root)

    assert not result.no_op
    assert (root / "1p" / "alpha" / "cp.md").exists()
    assert not (root / "1p" / "projects").exists()
    assert (root / "1p" / "projects") in result.removed_projects_dirs


def test_moves_across_multiple_scopes(tmp_path: Path) -> None:
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["client-1", "client-2"])
    _scaffold_old_layout(root, "firstpersonsf", ["internal-tool"])
    _scaffold_old_layout(root, "canonic", ["personal-thing"])

    result = migrate_projects_flat(root)

    assert (root / "1p" / "client-1" / "cp.md").exists()
    assert (root / "1p" / "client-2" / "cp.md").exists()
    assert (root / "firstpersonsf" / "internal-tool" / "cp.md").exists()
    assert (root / "canonic" / "personal-thing" / "cp.md").exists()
    for scope in ("1p", "firstpersonsf", "canonic"):
        assert not (root / scope / "projects").exists()
    # 4 project dir moves; no archives in this test.
    assert len(result.moved_dirs) == 4


def test_moves_legacy_archived_subdir_to_inactive(tmp_path: Path) -> None:
    """Pre-v0.7 layouts called the dropped-out-of-sync bin `archived/`. v0.7.1
    renamed it to `inactive/`. The migration reads the legacy `archived/`
    name as input and writes the new `inactive/` name as output."""
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["alive"])
    legacy_archived = root / "1p" / "projects" / "archived" / "dead-project"
    legacy_archived.mkdir(parents=True)
    (legacy_archived / "cp.md").write_text("# dead")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "add legacy archived"], cwd=root)

    result = migrate_projects_flat(root)

    assert (root / "1p" / "alive" / "cp.md").exists()
    # Old `archived/` becomes new `inactive/`.
    assert (root / "1p" / "inactive" / "dead-project" / "cp.md").exists()
    assert not (root / "1p" / "archived").exists()
    assert not (root / "1p" / "projects").exists()
    assert len(result.moved_dirs) == 2  # alive + inactive bin


def test_skips_scope_with_no_old_projects_dir(tmp_path: Path) -> None:
    """A scope already in v0.7 layout coexists with one in old layout."""
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["client-1"])
    # canonic already migrated to v0.7 shape
    (root / "canonic" / "already-migrated").mkdir(parents=True)
    (root / "canonic" / "already-migrated" / "cp.md").write_text("# x")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "mixed"], cwd=root)

    result = migrate_projects_flat(root)

    # 1p moved, canonic untouched.
    assert (root / "1p" / "client-1" / "cp.md").exists()
    assert (root / "canonic" / "already-migrated" / "cp.md").exists()
    assert len(result.moved_dirs) == 1
    assert result.moved_dirs[0][0].name == "client-1"


# ──────────────────────────────────────────────────────────────────────
#  Conflict detection
# ──────────────────────────────────────────────────────────────────────


def test_collision_with_existing_destination_aborts(tmp_path: Path) -> None:
    """If `<scope>/<dir>` already exists when we try to move to it, fail loud."""
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["alpha"])
    # Create a same-named dir directly under the scope (collision).
    (root / "1p" / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "1p" / "alpha" / "marker.txt").write_text("x")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "collision"], cwd=root)

    with pytest.raises(MigrateFlatError, match="destination already exists"):
        migrate_projects_flat(root)


def test_inactive_collision_aborts(tmp_path: Path) -> None:
    """If `<scope>/inactive/` already exists alongside `<scope>/projects/archived/`
    (the legacy name), the migration must fail loudly rather than silently merge."""
    root = _init_tenant(tmp_path)
    (root / "1p" / "projects" / "archived" / "dead").mkdir(parents=True)
    (root / "1p" / "projects" / "archived" / "dead" / "cp.md").write_text("# dead")
    (root / "1p" / "inactive" / "other").mkdir(parents=True)
    (root / "1p" / "inactive" / "other" / "cp.md").write_text("# other")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "two inactive bins"], cwd=root)

    with pytest.raises(MigrateFlatError, match="destination already exists"):
        migrate_projects_flat(root)


# ──────────────────────────────────────────────────────────────────────
#  Git history preservation
# ──────────────────────────────────────────────────────────────────────


def test_moves_use_git_mv_so_history_follows(tmp_path: Path) -> None:
    """After migration, `git log --follow` on the new path should see the
    original commit. This is the whole point of using `git mv` over `mv`."""
    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["alpha"])
    original_sha = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "1p/projects/alpha/cp.md"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert original_sha  # sanity

    migrate_projects_flat(root)
    _git(["commit", "-m", "migrate"], cwd=root)

    # `git log --follow` should walk through the rename and still see the
    # original commit.
    follow_log = subprocess.run(
        ["git", "log", "--follow", "--format=%H", "--", "1p/alpha/cp.md"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()

    assert original_sha in follow_log
