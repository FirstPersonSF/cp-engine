"""A clean working tree must not 500 the request (#237).

THE REPORT (2026-09-09): snooze reported `files_written` on a no-op write, then
died on "nothing to commit". `git commit` fails when a handler wrote nothing —
a snooze whose bullet an earlier delivery already flipped, a plan whose every
verb was a no-op — and `check=True` turns that benign case into a 500 AFTER the
caller has reported success.

WHY THIS TEST EXISTS RATHER THAN A SECOND SPOT FIX. The original report was
fixed in `_commit_clickup_close` — ONE path. Four other helpers kept the bug:

    _commit_clickup_close          GUARDED  (the 2026-09 fix)
    _commit_with_message_and_push  unguarded  <- the SHARED tail
    _commit_and_push               unguarded  (delegates to it)
    _commit_and_push_promote       unguarded  (delegates to it)
    _commit_meeting_artifacts      already returns None, try/except wrapped

Guarding the shared tail fixes three at once and every direct caller with them
(sessions, project-state, email). Same shape as decision #123's corollary: fix
the class, not the instance.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import git_ops


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A real clone with a bare origin, so commit AND push both run."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=origin)

    work = tmp_path / "cp"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "seed.md").write_text("seed\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)

    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    return work


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


class TestTheEmptyTreeGuard:
    def test_a_clean_tree_returns_none_instead_of_raising(self, repo: Path):
        """THE REPORTED FAILURE: this used to raise CalledProcessError -> 500."""
        assert git_ops._commit_with_message_and_push(repo, "[test] no-op") is None

    def test_a_clean_tree_creates_no_commit(self, repo: Path):
        before = _head(repo)
        git_ops._commit_with_message_and_push(repo, "[test] no-op")
        assert _head(repo) == before, "a no-op must not move HEAD"

    def test_a_real_change_still_commits_and_returns_a_sha(self, repo: Path):
        (repo / "new.md").write_text("content\n")
        sha = git_ops._commit_with_message_and_push(repo, "[test] real change")
        assert sha and len(sha) == 40
        assert sha == _head(repo)

    def test_the_no_op_does_not_poison_a_later_real_commit(self, repo: Path):
        """A skipped commit must leave the tree usable, not half-staged."""
        assert git_ops._commit_with_message_and_push(repo, "[test] no-op") is None
        (repo / "after.md").write_text("x\n")
        sha = git_ops._commit_with_message_and_push(repo, "[test] after")
        assert sha == _head(repo)


class TestTheDelegatingWrappers:
    """Both build a message and hand off to the shared tail, so both inherit
    the guard — that is the point of fixing it there."""

    def test_auto_ingest_wrapper_returns_none_on_a_clean_tree(self, repo: Path):
        assert git_ops._commit_and_push(
            tenant_root=repo, meeting_id="m1",
            ingested=[{"code": "ggl-5136", "files_written": []}],
        ) is None

    def test_spine_promote_wrapper_returns_none_on_a_clean_tree(self, repo: Path):
        assert git_ops._commit_and_push_promote(
            tenant_root=repo, project_code="ggl-5136",
            version_label="v2", rel_path="spine/x.md",
        ) is None

    def test_spine_promote_still_returns_a_sha_for_real_work(self, repo: Path):
        (repo / "spine.md").write_text("promoted\n")
        sha = git_ops._commit_and_push_promote(
            tenant_root=repo, project_code="ggl-5136",
            version_label="v2", rel_path="spine/x.md",
        )
        assert sha == _head(repo)
