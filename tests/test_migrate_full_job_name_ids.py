"""Tests for scripts/migrate_full_job_name_ids.py — the one-time tree
migration that renames sprint files (and any genuinely-differing working
dirs) from the legacy `<company>-<number>` id to the full_job_name slug.

The script's move-planning, execution, and link-reporting are factored
into pure/IO-only seams (`plan_moves`, `apply_moves`,
`find_handwritten_refs`) so they unit-test WITHOUT touching MC-2: tests
pass the old->new map in directly.

Each test sets up a fixture tenant in a real git repo so `git mv` (and
its idempotent re-run) exercise the real code path.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Load the script module by path (it lives under scripts/, not the package).
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_full_job_name_ids.py"
_spec = importlib.util.spec_from_file_location("migrate_full_job_name_ids", _SCRIPT)
mig = importlib.util.module_from_spec(_spec)
sys.modules["migrate_full_job_name_ids"] = mig
_spec.loader.exec_module(mig)


# ──────────────────────────────────────────────────────────────────────
#  Fixture
# ──────────────────────────────────────────────────────────────────────


def _git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def tenant(tmp_path: Path) -> Path:
    """A git tenant tree with two sprint weeks of ibx-5192, a working dir
    that ALREADY matches the new slug, and a handwritten link to a sprint
    file. Mirrors the real tenant's actual state."""
    root = tmp_path / "cp"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "t@example.com"], cwd=root)
    _git(["config", "user.name", "T"], cwd=root)

    # Sprint files for the project under its OLD id, across two weeks.
    for week in ("2026-W25", "2026-W26"):
        d = root / "sprints" / week
        d.mkdir(parents=True)
        (d / "ibx-5192.md").write_text(f"# ibx-5192 sprint {week}\n")
    # A closed project NOT in the map — must stay untouched.
    (root / "sprints" / "2026-W26" / "ggl-4000.md").write_text("# closed\n")

    # Working dir already at the NEW slug (the common real case).
    wd = root / "1p" / "infoblox" / "ibx-5192-platform-sales-readiness-summit"
    wd.mkdir(parents=True)
    (wd / "cp.md").write_text("# cp\n")

    # A handwritten file linking the old sprint path + old dir name.
    (root / "weekly-cp.md").write_text(
        "## Quick Resume\n"
        "- See [last week](sprints/2026-W26/ibx-5192.md) for details.\n"
        "- Working dir is 1p/infoblox/ibx-5192/ historically.\n"
    )

    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


_MAP = {"ibx-5192": "ibx-5192-platform-sales-readiness-summit"}


# ──────────────────────────────────────────────────────────────────────
#  plan_moves
# ──────────────────────────────────────────────────────────────────────


def test_plan_moves_lists_all_sprint_weeks(tenant: Path):
    moves = mig.plan_moves(tenant, _MAP)
    srcs = {src.relative_to(tenant).as_posix() for src, _ in moves}
    assert "sprints/2026-W25/ibx-5192.md" in srcs
    assert "sprints/2026-W26/ibx-5192.md" in srcs
    # Destinations carry the new id.
    dsts = {dst.relative_to(tenant).as_posix() for _, dst in moves}
    assert "sprints/2026-W25/ibx-5192-platform-sales-readiness-summit.md" in dsts
    assert "sprints/2026-W26/ibx-5192-platform-sales-readiness-summit.md" in dsts


def test_plan_moves_is_read_only(tenant: Path):
    before = sorted(p.as_posix() for p in tenant.rglob("*") if p.is_file())
    mig.plan_moves(tenant, _MAP)
    after = sorted(p.as_posix() for p in tenant.rglob("*") if p.is_file())
    assert before == after


def test_plan_moves_skips_already_matching_dir(tenant: Path):
    """The working dir already equals the new slug → no dir move planned."""
    moves = mig.plan_moves(tenant, _MAP)
    for src, _dst in moves:
        assert "1p/" not in src.relative_to(tenant).as_posix(), (
            f"working dir should not move, got {src}"
        )


def test_plan_moves_moves_genuinely_differing_dir(tmp_path: Path):
    """When a dir is named by the OLD id and differs from the new slug,
    it IS planned for a move."""
    root = tmp_path / "cp"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "t@example.com"], cwd=root)
    _git(["config", "user.name", "T"], cwd=root)
    old_dir = root / "1p" / "infoblox" / "ibx-5192"  # bare old id
    old_dir.mkdir(parents=True)
    (old_dir / "cp.md").write_text("# cp\n")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)

    moves = mig.plan_moves(root, _MAP)
    dir_moves = [
        (s, d) for s, d in moves if s.relative_to(root).as_posix().startswith("1p/")
    ]
    assert len(dir_moves) == 1
    src, dst = dir_moves[0]
    assert src.name == "ibx-5192"
    assert dst.name == "ibx-5192-platform-sales-readiness-summit"


def test_plan_moves_ignores_unmapped_project(tenant: Path):
    moves = mig.plan_moves(tenant, _MAP)
    for src, _dst in moves:
        assert "ggl-4000" not in src.name


# ──────────────────────────────────────────────────────────────────────
#  apply_moves + idempotency
# ──────────────────────────────────────────────────────────────────────


def test_apply_moves_renames_via_git(tenant: Path):
    moves = mig.plan_moves(tenant, _MAP)
    mig.apply_moves(tenant, moves, git=True)

    for week in ("2026-W25", "2026-W26"):
        old = tenant / "sprints" / week / "ibx-5192.md"
        new = tenant / "sprints" / week / "ibx-5192-platform-sales-readiness-summit.md"
        assert not old.exists()
        assert new.exists()

    # Moves are staged in git (tracked rename, not an untracked copy).
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tenant, capture_output=True, text=True
    ).stdout
    assert "ibx-5192-platform-sales-readiness-summit.md" in status


def test_apply_moves_is_idempotent(tenant: Path):
    mig.apply_moves(tenant, mig.plan_moves(tenant, _MAP), git=True)
    # Re-plan after apply → nothing left to move.
    assert mig.plan_moves(tenant, _MAP) == []


# ──────────────────────────────────────────────────────────────────────
#  find_handwritten_refs
# ──────────────────────────────────────────────────────────────────────


def test_find_handwritten_refs_reports_link(tenant: Path):
    refs = mig.find_handwritten_refs(tenant, _MAP)
    blob = "\n".join(str(r) for r in refs)
    assert "weekly-cp.md" in blob
    assert "sprints/2026-W26/ibx-5192.md" in blob


def test_find_handwritten_refs_does_not_edit(tenant: Path):
    before = (tenant / "weekly-cp.md").read_text()
    mig.find_handwritten_refs(tenant, _MAP)
    after = (tenant / "weekly-cp.md").read_text()
    assert before == after
