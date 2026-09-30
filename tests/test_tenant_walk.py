"""Tenant-wide walks skip worktrees of the tenant (#325).

An agent session's worktree under `.claude/worktrees/` is a full second copy
of the tree. `cxp render` walked into one on 2026-09-25 and printed every
warning twice; sync's Last-session refresh would have edited the worktree's
cp.md files as if they were the tenant's. Each walker is exercised against a
tenant that carries BOTH shapes of stray checkout — a dot-dir worktree and a
real `git worktree add` at a plain-named subdirectory — and must report
exactly what the same tenant reports without them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cp_engine.cli_cmds.core import _exec_summary_warnings
from cp_engine.link_local import discover_cp_working_dirs
from cp_engine.tenant_walk import linked_worktrees, walk_tenant
from cp_engine.word_count_lint import AUDIT_THRESHOLD_WORDS, word_count_warnings


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _tenant(tmp_path: Path) -> Path:
    """A committed tenant with one over-budget project cp.md, a sessions/
    dir and a `_repo.md` — one hit for every walker under test."""
    root = tmp_path / "cp"
    wd = root / "1p" / "ggl-5168-activation"
    (wd / "sessions").mkdir(parents=True)
    (wd / "sessions" / "2026-09-25-1000-drew.md").write_text("x\n")
    (wd / "cp.md").write_text(
        "**Last session:** _<date>_\n\n" + "word " * (AUDIT_THRESHOLD_WORDS + 50)
    )
    (wd / "_repo.md").write_text(
        "# Source repository\n\n"
        "[FirstPersonSF/mc-2](https://github.com/FirstPersonSF/mc-2)\n"
    )
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    return root


def _add_stray_worktrees(root: Path) -> None:
    (root / ".claude" / "worktrees").mkdir(parents=True)
    _git("worktree", "add", "-q", ".claude/worktrees/agent-a", cwd=root)
    _git("worktree", "add", "-q", "wt-plain", cwd=root)


def test_linked_worktrees_names_only_nested_checkouts(tmp_path):
    root = _tenant(tmp_path)
    assert linked_worktrees(root) == ()
    _add_stray_worktrees(root)
    assert set(linked_worktrees(root)) == {
        (root / ".claude" / "worktrees" / "agent-a").resolve(),
        (root / "wt-plain").resolve(),
    }


def test_every_walker_reports_the_same_with_or_without_worktrees(tmp_path):
    root = _tenant(tmp_path)
    before = {
        "files": list(walk_tenant(root, "cp.md")),
        "sessions": list(walk_tenant(root, "sessions", dirs=True)),
        "word_count": word_count_warnings(root),
        "exec": _exec_summary_warnings(root),
        "repos": [d.path for d in discover_cp_working_dirs(root)],
    }
    # Guard the guard: the fixture must actually produce something for each
    # walker, or "unchanged" would be trivially true.
    assert before["files"] and before["sessions"] and before["word_count"]
    assert before["repos"]

    _add_stray_worktrees(root)
    # The worktrees really do hold the duplicates a bare rglob would find.
    assert len(sorted(root.rglob("cp.md"))) == 3

    after = {
        "files": list(walk_tenant(root, "cp.md")),
        "sessions": list(walk_tenant(root, "sessions", dirs=True)),
        "word_count": word_count_warnings(root),
        "exec": _exec_summary_warnings(root),
        "repos": [d.path for d in discover_cp_working_dirs(root)],
    }
    assert after == before


def test_last_session_refresh_never_edits_a_worktrees_cp_md(tmp_path):
    """Sync's convergence pass writes cp.md files — the one walker here whose
    trip into a worktree would CHANGE another checkout, not just misreport."""
    from cp_engine.sync import _refresh_all_last_session_lines

    root = _tenant(tmp_path)
    _add_stray_worktrees(root)
    changed = _refresh_all_last_session_lines(root)
    # The tenant's own line converges — proves the pass ran at all.
    assert (root / "1p" / "ggl-5168-activation" / "cp.md") in changed
    strays = {p.resolve() for p in linked_worktrees(root)}
    assert not [c for c in changed if strays & set(c.resolve().parents)]


def test_a_non_git_root_still_skips_dot_dirs(tmp_path):
    """No git (webhook sparse clone, tests): the dot-dir rule still holds."""
    root = tmp_path / "t"
    (root / "1p" / "a").mkdir(parents=True)
    (root / "1p" / "a" / "cp.md").write_text("x")
    (root / ".claude" / "worktrees" / "w" / "1p" / "a").mkdir(parents=True)
    (root / ".claude" / "worktrees" / "w" / "1p" / "a" / "cp.md").write_text("x")
    assert list(walk_tenant(root, "cp.md")) == [root / "1p" / "a" / "cp.md"]
