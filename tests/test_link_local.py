"""Tests for `cp_engine.link_local`.

Each test builds a temporary cp tenant directory with one or more
working dirs (each containing a `_repo.md`) and a temporary "source
repo" (a directory with `.git/` to mimic a clone), then runs
`link_local()` and asserts on the result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cp_engine.config import TenantConfig, SyncConfig
from cp_engine.link_local import (
    GitRemoteMismatch,
    NoMatchingCpWorkingDir,
    NotAGitRepo,
    discover_cp_working_dirs,
    find_cp_working_dir_for_remote,
    link_local,
)


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def make_tenant(
    tmp_path: Path,
    *,
    working_dirs: list[tuple[str, str, str]],  # (scope, dir_slug, repo_name)
    org: str = "FirstPersonSF",
) -> Path:
    """Build a fake cp tenant tree. Each entry creates
    `<scope>/projects/<dir_slug>/_repo.md` whose body links to
    `<org>/<repo_name>` on GitHub.
    """
    for scope, dir_slug, repo_name in working_dirs:
        wd = tmp_path / scope / "projects" / dir_slug
        wd.mkdir(parents=True)
        (wd / "_repo.md").write_text(
            f"# Source repository\n\n"
            f"[{org}/{repo_name}](https://github.com/{org}/{repo_name})\n",
            encoding="utf-8",
        )
    return tmp_path


def make_source_repo(
    tmp_path: Path,
    name: str,
    *,
    org: str = "FirstPersonSF",
    set_remote: bool = True,
) -> Path:
    """Build a fake source-repo clone at tmp_path/<name>/ with .git initialized
    and `origin` pointed at github.com/<org>/<name>.
    """
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    if set_remote:
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                f"https://github.com/{org}/{name}.git",
            ],
            cwd=repo,
            check=True,
        )
    return repo


def make_config(tenant_root: Path, local_repos: dict[str, Path]) -> TenantConfig:
    return TenantConfig(
        name="cp",
        display="CP",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="x"),
        projects=(),
        root=tenant_root.resolve(),
        local_repos={k: v.resolve() for k, v in local_repos.items()},
    )


# ──────────────────────────────────────────────────────────────────────
#  Discovery
# ──────────────────────────────────────────────────────────────────────


def test_discover_finds_repo_md_files(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(
        tenant,
        working_dirs=[
            ("firstpersonsf", "mc-2", "mc-2"),
            ("firstpersonsf", "cp-engine", "cp-engine"),
            ("canonic", "storyos", "storyos"),
        ],
    )
    found = discover_cp_working_dirs(tenant)
    assert [(d.repo_name, d.github_org) for d in found] == [
        ("cp-engine", "FirstPersonSF"),
        ("mc-2", "FirstPersonSF"),
        ("storyos", "FirstPersonSF"),
    ]


def test_discover_skips_archived(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    # Live working dir
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    # Archived (should be skipped)
    archived = tenant / "firstpersonsf" / "projects" / "archived" / "old-repo"
    archived.mkdir(parents=True)
    (archived / "_repo.md").write_text(
        "[FPSF/old-repo](https://github.com/FPSF/old-repo)\n", encoding="utf-8"
    )
    found = discover_cp_working_dirs(tenant)
    assert {d.repo_name for d in found} == {"mc-2"}


def test_find_for_remote_matches_https_url(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    wd = find_cp_working_dir_for_remote(
        tenant, "https://github.com/FirstPersonSF/mc-2.git"
    )
    assert wd is not None
    assert wd.repo_name == "mc-2"


def test_find_for_remote_matches_ssh_url(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    wd = find_cp_working_dir_for_remote(tenant, "git@github.com:FirstPersonSF/mc-2.git")
    assert wd is not None
    assert wd.repo_name == "mc-2"


def test_find_for_remote_no_match(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    assert (
        find_cp_working_dir_for_remote(tenant, "https://github.com/foo/bar") is None
    )


# ──────────────────────────────────────────────────────────────────────
#  Linking
# ──────────────────────────────────────────────────────────────────────


def test_link_local_writes_cp_link(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    repo = make_source_repo(tmp_path, "mc-2")

    config = make_config(tenant, {"mc-2": repo})
    [result] = link_local(config)

    link_file = repo / ".cp-link"
    assert link_file.exists()
    expected_target = str((tenant / "firstpersonsf" / "projects" / "mc-2").resolve())
    assert link_file.read_text(encoding="utf-8").strip() == expected_target
    assert result.wrote_link is True
    assert result.excluded is True


def test_link_local_appends_to_git_info_exclude(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    repo = make_source_repo(tmp_path, "mc-2")

    # Pre-populate exclude with another entry to confirm we append, not replace.
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# pre-existing\n.idea/\n", encoding="utf-8")

    link_local(make_config(tenant, {"mc-2": repo}))

    contents = exclude.read_text(encoding="utf-8")
    assert ".idea/" in contents
    assert ".cp-link" in contents


def test_link_local_idempotent(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    repo = make_source_repo(tmp_path, "mc-2")

    config = make_config(tenant, {"mc-2": repo})
    link_local(config)
    [second] = link_local(config)

    assert second.wrote_link is False
    assert second.excluded is False
    # And exclude has exactly one .cp-link entry, not two.
    contents = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert contents.count(".cp-link") == 1


def test_link_local_rejects_non_git_path(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])

    # A directory that exists but isn't a git repo
    fake_repo = tmp_path / "mc-2"
    fake_repo.mkdir()

    config = make_config(tenant, {"mc-2": fake_repo})
    with pytest.raises(NotAGitRepo):
        link_local(config)


def test_link_local_rejects_remote_mismatch(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])

    # User pointed [local-repos]."mc-2" at a clone of *some other* repo.
    wrong_repo = tmp_path / "wrong-clone-dir"
    wrong_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wrong_repo, check=True)
    subprocess.run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/FirstPersonSF/something-else.git",
        ],
        cwd=wrong_repo,
        check=True,
    )

    config = make_config(tenant, {"mc-2": wrong_repo})
    with pytest.raises(GitRemoteMismatch):
        link_local(config)


def test_link_local_rejects_unknown_repo_name(tmp_path: Path) -> None:
    """[local-repos] names a repo with no matching cp working dir."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])

    # mc-2 has a working dir; unknown-repo doesn't.
    repo = make_source_repo(tmp_path, "unknown-repo")

    config = make_config(tenant, {"unknown-repo": repo})
    with pytest.raises(NoMatchingCpWorkingDir, match="unknown-repo"):
        link_local(config)


def test_link_local_no_op_when_local_repos_empty(tmp_path: Path) -> None:
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    config = make_config(tenant, {})
    assert link_local(config) == ()


def test_link_local_tolerates_missing_remote(tmp_path: Path) -> None:
    """If a source repo has no `origin` remote yet, we still write the link.
    The remote check is a guardrail against pointing at the *wrong* repo,
    not a hard requirement that origin be configured.
    """
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    make_tenant(tenant, working_dirs=[("firstpersonsf", "mc-2", "mc-2")])
    repo = make_source_repo(tmp_path, "mc-2", set_remote=False)

    config = make_config(tenant, {"mc-2": repo})
    [result] = link_local(config)
    assert result.wrote_link is True
