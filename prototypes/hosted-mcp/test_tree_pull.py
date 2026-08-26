"""The tenant-tree mirror must converge on the remote, not freeze.

Regression test for the 2026-08-26 silent stall: the hosted clone had been
pinned to an 08-17 commit for nine days while the DB verbs returned current
rows. Cause — `git pull --ff-only` on a `--depth 1` clone. Git holds exactly
one commit, so it cannot prove the fetched tip descends from local HEAD, and
every pull dies with "Not possible to fast-forward". A pull failure is
non-fatal by design (a momentarily unreachable remote should serve a slightly
stale tree), so the freeze was invisible.

These tests drive real git against real repos on disk. No mocks: the bug was
in git's behavior, and a mocked subprocess would have reproduced neither the
failure nor the fix.

    python -m pytest prototypes/hosted-mcp/test_tree_pull.py -v
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )


def head(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A real repo with history, served over file:// so --depth is honored."""
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "-q", "-b", "main", cwd=up)
    git("config", "user.email", "t@example.com", cwd=up)
    git("config", "user.name", "t", cwd=up)
    for i in range(5):
        (up / "f.md").write_text(f"commit {i}\n")
        git("add", "-A", cwd=up)
        git("commit", "-qm", f"c{i}", cwd=up)
    return up


@pytest.fixture
def stale_clone(tmp_path: Path, upstream: Path) -> Path:
    """A depth-1 clone pinned to an OLD commit, then upstream moves on.

    This is the production shape: a long-lived mirror whose HEAD has fallen
    far enough behind that ancestry is no longer provable from one commit.
    """
    old = git("rev-parse", "HEAD~3", cwd=upstream).stdout.strip()
    git("checkout", "-q", "-B", "serving", old, cwd=upstream)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--depth", "1", "-b", "serving",
         f"file://{upstream}", str(clone)],
        check=True, capture_output=True,
    )
    assert (clone / ".git" / "shallow").exists(), "clone must be shallow"

    git("checkout", "-q", "-B", "serving", "main", cwd=upstream)
    return clone


def test_ff_only_pull_freezes_a_shallow_clone(stale_clone: Path, upstream: Path):
    """The bug, pinned. If this ever passes, git changed and the fix can go."""
    before = head(stale_clone)
    pull = git("pull", "--ff-only", "--depth", "1", cwd=stale_clone)

    assert pull.returncode != 0
    assert "fast-forward" in (pull.stderr or "").lower()
    assert head(stale_clone) == before, "clone silently stayed on the old commit"
    assert head(stale_clone) != head(upstream)


def test_fetch_reset_converges(stale_clone: Path, upstream: Path):
    """The fix: no ancestry needed, so the mirror matches the remote."""
    assert git("fetch", "--depth", "1", "origin", "HEAD", cwd=stale_clone).returncode == 0
    assert git("reset", "--hard", "FETCH_HEAD", cwd=stale_clone).returncode == 0

    assert head(stale_clone) == head(upstream)
    assert (stale_clone / ".git" / "shallow").exists(), "must stay shallow"
    assert (stale_clone / "f.md").read_text() == (upstream / "f.md").read_text()


def test_fetch_reset_is_idempotent(stale_clone: Path, upstream: Path):
    """A read-heavy surface re-runs this constantly; it must be a no-op."""
    for _ in range(3):
        git("fetch", "--depth", "1", "origin", "HEAD", cwd=stale_clone)
        git("reset", "--hard", "FETCH_HEAD", cwd=stale_clone)
    assert head(stale_clone) == head(upstream)


def test_fetch_reset_follows_a_rewound_remote(stale_clone: Path, upstream: Path):
    """`--ff-only` refuses a rewind; a mirror should follow it.

    Covers force-push and branch-reset, where "match the remote exactly" is
    the contract and there is nothing local worth preserving.
    """
    git("fetch", "--depth", "1", "origin", "HEAD", cwd=stale_clone)
    git("reset", "--hard", "FETCH_HEAD", cwd=stale_clone)
    assert head(stale_clone) == head(upstream)

    git("checkout", "-q", "-B", "main", "HEAD~2", cwd=upstream)
    git("fetch", "--depth", "1", "origin", "HEAD", cwd=stale_clone)
    git("reset", "--hard", "FETCH_HEAD", cwd=stale_clone)

    assert head(stale_clone) == head(upstream), "mirror must follow a rewind"


def test_server_uses_fetch_reset_not_ff_only():
    """Guard the call site itself — the bug was one command, not one behavior."""
    src = (Path(__file__).parent / "server.py").read_text()
    block = src[src.index("def tree_root()"):]
    block = block[:block.index("\ndef ")]

    assert '"pull", "--ff-only"' not in block, "the freezing command is back"
    assert '"fetch", "--depth", "1", "origin", "HEAD"' in block
    assert '"reset", "--hard", "FETCH_HEAD"' in block


def test_reads_report_the_commit_they_came_from():
    """A stall must be visible: the tree can lag while the DB verbs cannot."""
    src = (Path(__file__).parent / "server.py").read_text()
    assert '"tree_head": _TREE_STATE.get("head")' in src
