"""Tests for cp_engine.migrate_accounts (Phase 2 of the account
restructure — one-shot flat-1p/ to 1p/<account>/ migration).

Mirrors test_migrate_flat.py's shape: each test sets up a fixture
tenant in a real git repo, runs the migration with a FakeBackend
standing in for MC-2, and asserts on disk state + result fields.
"""

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
from cp_engine.migrate_accounts import (
    MigrateAccountsError,
    migrate_accounts,
)
from cp_engine.sync import Backend


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

    Migration loads config + queries the backend, so the toml needs to
    parse as a valid TenantConfig — give it a stub [sync] section.
    """
    root = tmp_path / "cp"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    (root / ".cp-engine.toml").write_text(
        "[tenant]\n"
        'name = "cp"\n'
        'display = "Test Tenant"\n'
        "\n[engine]\n"
        'version = "~= 0.8"\n'
        "\n[sync]\n"
        'backend = "mc-2"\n'
        'cron = "0 * * * *"\n'
        "\n[sync.mc_2]\n"
        'supabase_project_ref = "stub"\n'
    )
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


def _scaffold_flat_project(tenant_root: Path, dir_name: str, body: str = "") -> Path:
    """Create `<tenant>/1p/<dir>/cp.md` (the pre-migration flat layout)."""
    d = tenant_root / "1p" / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cp.md").write_text(body or f"# {dir_name}\n")
    return d


def _commit_all(tenant_root: Path, msg: str) -> None:
    _git(["add", "-A"], cwd=tenant_root)
    _git(["commit", "-m", msg], cwd=tenant_root)


class _FakeBackend(Backend):
    """Returns whatever ProjectStates we hand it."""

    def __init__(self, states: tuple[ProjectState, ...]) -> None:
        self._states = states

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        return self._states


def _state(
    code: str,
    company_code: str,
    company_name: str,
    *,
    name: str | None = None,
    status: str = "Open",
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name if name is not None else code,
        source="engagement",  # type: ignore[arg-type]
        company_kind="client",  # type: ignore[arg-type]
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 5, 20, tzinfo=timezone.utc),
        deadline=None,
    )


# ──────────────────────────────────────────────────────────────────────
#  Happy path
# ──────────────────────────────────────────────────────────────────────


def test_migration_moves_flat_dirs_to_account_subdirs(tmp_path: Path) -> None:
    """Three flat client dirs land under their account's nested dir."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    _scaffold_flat_project(root, "ibx-5153-ai-campaign")
    _scaffold_flat_project(root, "tel-2001-bold-2-0")
    _commit_all(root, "scaffold flat projects")

    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
        _state("ibx-5153", "IBX", "Infoblox", name="AI Campaign"),
        _state("tel-2001", "TEL", "Teleflex", name="Bold 2.0"),
    ))

    result = migrate_accounts(root, backend_factory=lambda _: fake)

    assert (root / "1p" / "google" / "ggl-5168-playbooks" / "cp.md").exists()
    assert (root / "1p" / "infoblox" / "ibx-5153-ai-campaign" / "cp.md").exists()
    assert (root / "1p" / "teleflex" / "tel-2001-bold-2-0" / "cp.md").exists()
    # Old flat paths gone
    assert not (root / "1p" / "ggl-5168-playbooks").exists()
    assert not (root / "1p" / "ibx-5153-ai-campaign").exists()
    assert not (root / "1p" / "tel-2001-bold-2-0").exists()
    # Three moves planned, three executed
    assert len(result.planned_moves) == 3
    assert len(result.moved_dirs) == 3
    assert not result.no_op
    assert result.unresolved_dirs == ()


def test_migration_preserves_git_history(tmp_path: Path) -> None:
    """`git log --follow` on a moved file shows the old path. Using
    `git mv` (not raw filesystem rename) is what makes this work.

    The fixture cp.md has enough body that git's rename-detection
    heuristic recognizes the move as a rename (not a delete + add).
    Without sufficient content, --follow returns only the post-move
    commit; with it, the scaffold commit is reachable too."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(
        root, "ggl-5168-playbooks",
        body="# Playbooks Project CP\n\n" + ("Initial scaffold content.\n" * 20),
    )
    _commit_all(root, "scaffold")

    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))
    migrate_accounts(root, backend_factory=lambda _: fake)
    # Migration stages but doesn't commit; real workflow commits next.
    # Commit here so `git log --follow` can walk the history of the
    # moved file.
    _commit_all(root, "migrate")

    new_path = root / "1p" / "google" / "ggl-5168-playbooks" / "cp.md"
    # Use a low rename-detection threshold (-M10 = 10% similarity). The
    # post-move `cp sync` step modifies the cp.md (adds current-sprint
    # markers etc.), so the default 50% threshold doesn't always trip;
    # lowering it lets `--follow` reliably walk through the rename even
    # when the file's content changes alongside the move.
    log = subprocess.run(
        ["git", "log", "-M10", "--follow", "--format=%s",
         str(new_path.relative_to(root))],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    assert "scaffold" in log


def test_migration_runs_sync_at_end(tmp_path: Path) -> None:
    """After migration, account `cp.md` exists — proves `cp sync` ran
    so Phase 1's account scaffolding kicked in."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    _commit_all(root, "scaffold")

    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))
    migrate_accounts(root, backend_factory=lambda _: fake)

    assert (root / "1p" / "google" / "cp.md").exists()


# ──────────────────────────────────────────────────────────────────────
#  Idempotency
# ──────────────────────────────────────────────────────────────────────


def test_re_running_migration_is_noop(tmp_path: Path) -> None:
    """Running twice in a row: second run does nothing."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    _commit_all(root, "scaffold")
    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))

    first = migrate_accounts(root, backend_factory=lambda _: fake)
    assert not first.no_op
    # Commit the first run's changes so the second run sees a clean tree.
    _commit_all(root, "migrated")

    second = migrate_accounts(root, backend_factory=lambda _: fake)
    assert second.no_op
    assert second.planned_moves == ()
    assert second.moved_dirs == ()


def test_migration_skips_already_nested_account_dirs(tmp_path: Path) -> None:
    """When some dirs are flat and others already nested, only the flat
    ones move. (Mixed-state run, e.g. a partially-recovered migration.)

    The pre-nested account dir uses a real scaffolded account cp.md
    (with engine markers) so the post-move sync can re-splice cleanly —
    this is what a real partially-recovered tree looks like in practice."""
    from cp_engine.render import render_account_cp
    root = _init_tenant(tmp_path)
    google_dir = root / "1p" / "google"
    google_dir.mkdir(parents=True)
    (google_dir / "cp.md").write_text(render_account_cp("google", "Google", ()))
    nested_project = google_dir / "ggl-5168-playbooks"
    nested_project.mkdir()
    (nested_project / "cp.md").write_text("# nested marker line\n")
    # Flat sibling that still needs migrating.
    _scaffold_flat_project(root, "ibx-5153-ai-campaign")
    _commit_all(root, "mixed state")

    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
        _state("ibx-5153", "IBX", "Infoblox", name="AI Campaign"),
    ))
    result = migrate_accounts(root, backend_factory=lambda _: fake)

    # Only the flat ibx dir moved.
    assert len(result.moved_dirs) == 1
    old, new = result.moved_dirs[0]
    assert old.name == "ibx-5153-ai-campaign"
    assert new == root / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    # The already-nested google project survives the migration — its
    # hand-written marker line is preserved. (sync may splice in a
    # current-sprint block; we only assert the marker line survives.)
    nested_body = (root / "1p" / "google" / "ggl-5168-playbooks" / "cp.md").read_text()
    assert "# nested marker line" in nested_body


# ──────────────────────────────────────────────────────────────────────
#  Pre-flight failures
# ──────────────────────────────────────────────────────────────────────


def test_migration_refuses_dirty_tree(tmp_path: Path) -> None:
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    # Don't commit — leaves the tree dirty.
    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))
    with pytest.raises(MigrateAccountsError, match="not clean"):
        migrate_accounts(root, backend_factory=lambda _: fake)


def test_migration_fails_on_unresolved_dir(tmp_path: Path) -> None:
    """A flat dir whose code doesn't appear in MC-2 → hard-fail with
    that dir in `unresolved_dirs` and no disk writes."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "xxx-9999-unknown")
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    _commit_all(root, "scaffold")

    # MC-2 only knows about ggl-5168, not xxx-9999.
    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))
    result = migrate_accounts(root, backend_factory=lambda _: fake)

    assert len(result.unresolved_dirs) == 1
    assert result.unresolved_dirs[0].name == "xxx-9999-unknown"
    # Hard-fail means no disk writes happened.
    assert result.moved_dirs == ()
    assert (root / "1p" / "xxx-9999-unknown" / "cp.md").exists()
    assert (root / "1p" / "ggl-5168-playbooks" / "cp.md").exists()
    assert not (root / "1p" / "google").exists()


def test_dry_run_with_unresolved_dirs_still_reports_them(tmp_path: Path) -> None:
    """Dry-run is also gated by the pre-flight resolution check."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "xxx-9999-unknown")
    _commit_all(root, "scaffold")

    fake = _FakeBackend(())
    result = migrate_accounts(root, dry_run=True, backend_factory=lambda _: fake)
    assert len(result.unresolved_dirs) == 1
    assert result.moved_dirs == ()


# ──────────────────────────────────────────────────────────────────────
#  Inactive bin routing
# ──────────────────────────────────────────────────────────────────────


def test_inactive_dirs_route_to_per_account_inactive(tmp_path: Path) -> None:
    """`1p/inactive/<dir>/` → `1p/<company>/inactive/<dir>/`.

    Realistic case: a project that flipped to `is_internal=true` —
    MC-2 still returns it (with the company info we need for the
    lookup), but sync's main project loop skips internal projects so
    the dir sits in the flat inactive bin until the operator migrates."""
    root = _init_tenant(tmp_path)
    inactive_old = root / "1p" / "inactive" / "hex-5184-internal-thing"
    inactive_old.mkdir(parents=True)
    (inactive_old / "cp.md").write_text("# internal\n")
    _commit_all(root, "scaffold flat inactive")

    internal_state = ProjectState(
        code="hex-5184", name="Internal Thing", source="engagement",  # type: ignore[arg-type]
        company_kind="client",  # type: ignore[arg-type]
        company_code="HEX", company_name="Hexagon",
        status="Open", is_internal=True, owner="drew",
        last_touched=datetime(2026, 5, 20, tzinfo=timezone.utc),
        deadline=None,
    )
    fake = _FakeBackend((internal_state,))
    result = migrate_accounts(root, backend_factory=lambda _: fake)

    assert (
        root / "1p" / "hexagon" / "inactive" / "hex-5184-internal-thing" / "cp.md"
    ).exists()
    assert not inactive_old.exists()
    assert len(result.moved_dirs) == 1


# ──────────────────────────────────────────────────────────────────────
#  _teleflex.md absorb
# ──────────────────────────────────────────────────────────────────────


def test_teleflex_md_absorbed_under_legacy_notes_heading(tmp_path: Path) -> None:
    """`_teleflex.md` content appears in `1p/teleflex/cp.md` under a
    dated heading; old file is git-removed."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "tel-2001-bold-2-0")
    (root / "1p" / "_teleflex.md").write_text(
        "# Teleflex (account)\n\nProject history:\n- tel-2001 Bold 2.0 (active)\n"
    )
    _commit_all(root, "scaffold with _teleflex")

    fake = _FakeBackend((
        _state("tel-2001", "TEL", "Teleflex", name="Bold 2.0"),
    ))
    result = migrate_accounts(
        root, backend_factory=lambda _: fake,
        now=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )

    assert result.absorbed_teleflex
    teleflex_cp = (root / "1p" / "teleflex" / "cp.md").read_text()
    assert "## Legacy notes (from _teleflex.md, migrated 2026-05-22)" in teleflex_cp
    assert "tel-2001 Bold 2.0 (active)" in teleflex_cp
    # Old file is gone.
    assert not (root / "1p" / "_teleflex.md").exists()


def test_teleflex_md_absorption_skipped_when_no_file(tmp_path: Path) -> None:
    """Tenants without `_teleflex.md` exit cleanly."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    _commit_all(root, "scaffold")

    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))
    result = migrate_accounts(root, backend_factory=lambda _: fake)
    assert not result.absorbed_teleflex


# ──────────────────────────────────────────────────────────────────────
#  Dry-run
# ──────────────────────────────────────────────────────────────────────


def test_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    """`--dry-run` returns the plan but doesn't execute moves."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ggl-5168-playbooks")
    _commit_all(root, "scaffold")

    fake = _FakeBackend((
        _state("ggl-5168", "GGL", "Google", name="Playbooks"),
    ))
    result = migrate_accounts(root, dry_run=True, backend_factory=lambda _: fake)

    assert len(result.planned_moves) == 1
    assert result.moved_dirs == ()
    # Disk is unchanged.
    assert (root / "1p" / "ggl-5168-playbooks" / "cp.md").exists()
    assert not (root / "1p" / "google").exists()


# ──────────────────────────────────────────────────────────────────────
#  Residue
# ──────────────────────────────────────────────────────────────────────


def test_residue_lists_weekly_cp_references(tmp_path: Path) -> None:
    """Stale `1p/<code>` references in weekly-cp.md surface as residue
    hints for the operator to hand-fix."""
    root = _init_tenant(tmp_path)
    _scaffold_flat_project(root, "ibx-5153-ai-campaign")
    (root / "weekly-cp.md").write_text(
        "# Weekly CP\n\n"
        "- See `cp/1p/ibx-5153-ai-campaign/notes.md` for context.\n"
        "- Teleflex history: `cp/1p/_teleflex.md`.\n"
    )
    _commit_all(root, "scaffold with weekly-cp")

    fake = _FakeBackend((
        _state("ibx-5153", "IBX", "Infoblox", name="AI Campaign"),
    ))
    result = migrate_accounts(root, backend_factory=lambda _: fake)

    # Both references surface.
    joined = "\n".join(result.residue)
    assert "ibx-5153-ai-campaign" in joined
    assert "now under 1p/infoblox/" in joined
    assert "_teleflex.md" in joined
