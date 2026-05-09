"""Tests for `cp_engine.project_context`."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cp_engine.project_context import (
    NoLocalCloneAvailable,
    NotAWorkingDir,
    project_context,
)


# ──────────────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────────────


def make_tenant_with_project(
    tmp_path: Path,
    *,
    repo_name: str,
    local_clone: Path | None,
    extra_users: dict[str, Path] | None = None,
) -> Path:
    """Build a cp tenant root with one working dir for `repo_name`. If
    `local_clone` is given, also writes `[local-repos.drew]` pointing at
    it; if `extra_users` is given, adds those users too.
    """
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()

    local_repos_block = ""
    users_blocks: list[str] = []
    if local_clone is not None:
        users_blocks.append(
            f'[local-repos.drew]\n"{repo_name}" = "{local_clone}"\n'
        )
    if extra_users:
        for name, path in extra_users.items():
            users_blocks.append(
                f'[local-repos.{name}]\n"{repo_name}" = "{path}"\n'
            )
    if users_blocks:
        local_repos_block = "\n".join(users_blocks)

    (tenant / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.5"\n'
        '[sync]\nbackend = "github-issues"\n\n'
        + local_repos_block,
        encoding="utf-8",
    )
    # _load_local() requires .cp-engine.local.toml when committed has projects
    # but here we have no projects → file is optional.

    wd = tenant / "firstpersonsf" / repo_name
    wd.mkdir(parents=True)
    (wd / "_repo.md").write_text(
        f"# Source repository\n\n"
        f"[FirstPersonSF/{repo_name}](https://github.com/FirstPersonSF/{repo_name})\n",
        encoding="utf-8",
    )
    (wd / "cp.md").write_text(f"# {repo_name}\n", encoding="utf-8")
    return wd


def make_clone(tmp_path: Path, name: str) -> Path:
    """Init a git repo at tmp_path/name with a single initial commit."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    (repo / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True
    )
    return repo


def add_commit(
    repo: Path, *, subject: str, when: datetime, author: str = "Drew"
) -> None:
    """Add a commit with a back-dated commit-date."""
    file = repo / f"file-{when.strftime('%Y%m%d-%H%M%S')}.txt"
    file.write_text(subject, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    env = {
        **os.environ,
        "GIT_COMMITTER_DATE": when.isoformat(),
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": f"{author.lower()}@t",
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": f"{author.lower()}@t",
    }
    subprocess.run(
        ["git", "commit", "-q", "-m", subject],
        cwd=repo,
        env=env,
        check=True,
    )


def make_session_file(
    wd: Path, *, when: datetime, user: str, what_we_did: str
) -> Path:
    sessions = wd / "sessions"
    sessions.mkdir(exist_ok=True)
    name = (
        f"{when.strftime('%Y-%m-%d')}-{when.strftime('%H%M')}-{user.lower()}.md"
    )
    path = sessions / name
    path.write_text(
        f"## Session: {when.isoformat()}, {user}\n\n"
        f"### What we did\n{what_we_did}\n",
        encoding="utf-8",
    )
    return path


# ──────────────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────────────


def test_returns_recent_commits_and_sessions(tmp_path: Path) -> None:
    """Mixed commits + sessions in the last 7 days, both surface in the
    timeline."""
    clone = make_clone(tmp_path, "mc-2")
    wd = make_tenant_with_project(tmp_path, repo_name="mc-2", local_clone=clone)

    now = datetime(2026, 5, 9, 12, 0)
    add_commit(clone, subject="fix billing dropdown", when=now - timedelta(days=1))
    add_commit(clone, subject="refactor auth", when=now - timedelta(days=3))
    make_session_file(
        wd, when=now - timedelta(hours=2), user="drew",
        what_we_did="Wrapped the dropdown fix.",
    )

    result = project_context(working_dir=wd, days=7, now=now)

    assert result.repo_name == "mc-2"
    assert result.local_clone == clone.resolve()
    # Two recent commits + initial commit (depending on initial's date)
    assert len(result.commits) >= 2
    assert any("fix billing dropdown" in c.subject for c in result.commits)
    assert len(result.sessions) == 1
    assert result.sessions[0].user == "Drew"
    assert "dropdown" in (result.sessions[0].one_liner or "")


def test_excludes_old_activity(tmp_path: Path) -> None:
    clone = make_clone(tmp_path, "mc-2")
    wd = make_tenant_with_project(tmp_path, repo_name="mc-2", local_clone=clone)

    now = datetime(2026, 5, 9, 12, 0)
    add_commit(clone, subject="ancient", when=now - timedelta(days=30))
    make_session_file(
        wd, when=now - timedelta(days=15), user="drew", what_we_did="old"
    )

    result = project_context(working_dir=wd, days=7, now=now)
    assert all("ancient" not in c.subject for c in result.commits)
    assert result.sessions == ()


def test_no_local_clone_yields_sessions_only_timeline(tmp_path: Path) -> None:
    """When the tenant config has no local-clone entry for this repo on
    this machine, project_context returns sessions only — not an error."""
    wd = make_tenant_with_project(tmp_path, repo_name="mc-2", local_clone=None)
    now = datetime(2026, 5, 9, 12, 0)
    make_session_file(
        wd, when=now - timedelta(hours=1), user="drew", what_we_did="local-only"
    )

    result = project_context(working_dir=wd, days=7, now=now)
    assert result.local_clone is None
    assert result.commits == ()
    assert len(result.sessions) == 1


def test_explicit_user_with_missing_entry_raises(tmp_path: Path) -> None:
    clone = make_clone(tmp_path, "mc-2")
    wd = make_tenant_with_project(tmp_path, repo_name="mc-2", local_clone=clone)

    with pytest.raises(NoLocalCloneAvailable, match="tony"):
        project_context(working_dir=wd, user="tony", days=7)


def test_picks_first_existing_clone_when_user_unspecified(tmp_path: Path) -> None:
    """drew's path exists; tony's path doesn't. Default behavior: pick
    drew's clone."""
    clone = make_clone(tmp_path, "mc-2")
    nonexistent = tmp_path / "tony-elsewhere"
    wd = make_tenant_with_project(
        tmp_path,
        repo_name="mc-2",
        local_clone=clone,
        extra_users={"tony": nonexistent},
    )

    result = project_context(working_dir=wd, days=7)
    assert result.local_clone == clone.resolve()


def test_not_a_working_dir_raises(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-cp-dir"
    bare.mkdir()
    with pytest.raises(NotAWorkingDir):
        project_context(working_dir=bare)


def test_session_file_with_no_what_we_did_falls_back(tmp_path: Path) -> None:
    """A session file missing the `### What we did` header falls back to
    the first body line for the one-liner."""
    wd = make_tenant_with_project(tmp_path, repo_name="mc-2", local_clone=None)
    now = datetime(2026, 5, 9, 12, 0)
    sessions = wd / "sessions"
    sessions.mkdir()
    (sessions / "2026-05-09-1100-drew.md").write_text(
        "## Session: 2026-05-09 11:00, Drew\n\n"
        "Just some prose, no template.\n",
        encoding="utf-8",
    )

    result = project_context(working_dir=wd, days=7, now=now)
    assert len(result.sessions) == 1
    assert "Just some prose" in (result.sessions[0].one_liner or "")


def test_timeline_ordering_newest_first(tmp_path: Path) -> None:
    clone = make_clone(tmp_path, "mc-2")
    wd = make_tenant_with_project(tmp_path, repo_name="mc-2", local_clone=clone)

    now = datetime(2026, 5, 9, 12, 0)
    add_commit(clone, subject="oldest", when=now - timedelta(days=2))
    add_commit(clone, subject="middle", when=now - timedelta(days=1))
    add_commit(clone, subject="newest", when=now - timedelta(hours=1))

    result = project_context(working_dir=wd, days=7, now=now)
    subjects = [c.subject for c in result.commits]
    # Filter out the initial seed commit and check order
    relevant = [s for s in subjects if s in ("oldest", "middle", "newest")]
    assert relevant == ["newest", "middle", "oldest"]
