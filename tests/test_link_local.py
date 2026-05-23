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


def make_multirepo_engagement(
    tmp_path: Path,
    *,
    scope: str,
    dir_slug: str,
    repo_names: list[str],
    org: str = "FirstPersonSF",
) -> Path:
    """Build a multi-repo engagement working dir.

    Mirrors what sync writes for an engagement with multiple linked
    repos in MC-2: each repo gets a `_repo-<name>.md` file (sync uses
    `f"_repo-{linked.repo_name}.md"`). All files live in the same
    engagement working dir; there's no singular `_repo.md`.
    """
    wd = tmp_path / scope / "projects" / dir_slug
    wd.mkdir(parents=True)
    for repo_name in repo_names:
        (wd / f"_repo-{repo_name}.md").write_text(
            f"# Linked source repository\n\n"
            f"[{org}/{repo_name}](https://github.com/{org}/{repo_name})\n",
            encoding="utf-8",
        )
    return wd


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
#  Multi-repo engagement working dirs (_repo-<name>.md form)
#
# An engagement with N linked repos has N `_repo-<name>.md` files in
# its working dir (no singular `_repo.md`). Discovery + remote-matching
# + linking all need to surface each linked repo as its own entry so
# `cp link-local` and `cp capture-session` can route correctly.
# ──────────────────────────────────────────────────────────────────────


def test_discover_finds_repo_name_md_files_in_multirepo_engagement(
    tmp_path: Path,
) -> None:
    """Each `_repo-<name>.md` yields its own CpWorkingDir entry, all
    pointing at the shared engagement working dir."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    engagement_dir = make_multirepo_engagement(
        tenant,
        scope="1p",
        dir_slug="ggl-5136-go-safety-website",
        repo_names=["ggl-5136-ai-pipeline", "ggl-5136-events-calendar"],
    )

    found = discover_cp_working_dirs(tenant)
    repo_names = {d.repo_name for d in found}
    assert repo_names == {"ggl-5136-ai-pipeline", "ggl-5136-events-calendar"}
    # Both entries point at the same engagement dir (the file's parent).
    for d in found:
        assert d.path == engagement_dir.resolve()


def test_discover_handles_mixed_singular_and_per_repo_forms(tmp_path: Path) -> None:
    """A tenant with both a repo-source project (singular `_repo.md`)
    and a multi-repo engagement (`_repo-<name>.md` files) finds both."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    # Singular: a standalone repo-source project.
    make_tenant(tenant, working_dirs=[("firstpersonsf", "cp-engine", "cp-engine")])
    # Multi-repo: an engagement with two linked repos.
    make_multirepo_engagement(
        tenant,
        scope="1p",
        dir_slug="ggl-5136-go-safety-website",
        repo_names=["ggl-5136-ai-pipeline", "ggl-5136-events-calendar"],
    )

    found = discover_cp_working_dirs(tenant)
    assert {d.repo_name for d in found} == {
        "cp-engine",
        "ggl-5136-ai-pipeline",
        "ggl-5136-events-calendar",
    }


def test_discover_skips_per_repo_files_in_inactive_dirs(tmp_path: Path) -> None:
    """`_repo-<name>.md` files under an `inactive/` subtree are not
    surfaced as link targets — same rule as singular `_repo.md`."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    # Live multi-repo engagement
    make_multirepo_engagement(
        tenant,
        scope="1p",
        dir_slug="ggl-active",
        repo_names=["live-repo"],
    )
    # Inactive multi-repo engagement (per-account inactive bin shape)
    inactive_wd = tenant / "1p" / "google" / "inactive" / "ggl-old"
    inactive_wd.mkdir(parents=True)
    (inactive_wd / "_repo-old-repo.md").write_text(
        "[FPSF/old-repo](https://github.com/FPSF/old-repo)\n", encoding="utf-8",
    )

    found = discover_cp_working_dirs(tenant)
    assert {d.repo_name for d in found} == {"live-repo"}


def test_find_for_remote_matches_a_per_repo_md_file(tmp_path: Path) -> None:
    """`find_cp_working_dir_for_remote` resolves a source repo whose
    cp working dir is a multi-repo engagement (only `_repo-<name>.md`
    files, no singular `_repo.md`)."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    engagement_dir = make_multirepo_engagement(
        tenant,
        scope="1p",
        dir_slug="ggl-5136-go-safety-website",
        repo_names=["ggl-5136-events-calendar"],
    )

    wd = find_cp_working_dir_for_remote(
        tenant,
        "https://github.com/FirstPersonSF/ggl-5136-events-calendar.git",
    )
    assert wd is not None
    assert wd.repo_name == "ggl-5136-events-calendar"
    assert wd.path == engagement_dir.resolve()


def test_discover_prefers_singular_repo_md_over_linked_form(tmp_path: Path) -> None:
    """When the same repo appears as BOTH a singular `_repo.md`
    (canonical standalone-repo working dir) AND a `_repo-<name>.md`
    (initiative-linked reference), discovery returns ONE entry pointing
    at the singular form's dir.

    The standalone dir is the repo's home — it carries the project's
    `cp.md`, `sessions/` history, etc. The initiative-linked file is
    a pointer; routing there would put `cp capture-session` writes
    into the initiative's dir instead of the repo's own. Per CLAUDE.md
    design: 'Initiative-linked repos surface as `_repo-<name>.md`
    files under their initiative's working dir, not as separate
    top-level dirs.' The standalone dir is the working dir; the
    linked reference is decoration."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    # Canonical standalone repo working dir.
    make_tenant(tenant, working_dirs=[("firstpersonsf", "cp-engine", "cp-engine")])
    # An initiative dir that references the same repo via _repo-<name>.md.
    mission_control = tenant / "firstpersonsf" / "mission-control"
    mission_control.mkdir(parents=True)
    (mission_control / "_repo-cp-engine.md").write_text(
        "[FirstPersonSF/cp-engine](https://github.com/FirstPersonSF/cp-engine)\n",
        encoding="utf-8",
    )

    found = discover_cp_working_dirs(tenant)
    cp_engine_entries = [d for d in found if d.repo_name == "cp-engine"]
    assert len(cp_engine_entries) == 1, (
        f"Expected exactly one cp-engine entry, got {len(cp_engine_entries)}: "
        f"{[str(d.path) for d in cp_engine_entries]}"
    )
    # Points at the standalone repo dir, not the initiative dir.
    assert cp_engine_entries[0].path == (
        tenant / "firstpersonsf" / "projects" / "cp-engine"
    ).resolve()


def test_discover_collapses_duplicate_entries_in_same_dir(tmp_path: Path) -> None:
    """A working dir with both `_repo.md` and `_repo-<name>.md` for
    the same repo (e.g. a standalone repo project that's ALSO linked
    to an initiative pointing at the same repo, where both files
    happen to live in the same dir) collapses to one entry. Same path
    twice is just deduplication."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    wd = tenant / "canonic" / "storyos"
    wd.mkdir(parents=True)
    (wd / "_repo.md").write_text(
        "[Canonic-OS/storyos](https://github.com/Canonic-OS/storyos)\n",
        encoding="utf-8",
    )
    (wd / "_repo-storyos.md").write_text(
        "[Canonic-OS/storyos](https://github.com/Canonic-OS/storyos)\n",
        encoding="utf-8",
    )

    found = discover_cp_working_dirs(tenant)
    storyos_entries = [d for d in found if d.repo_name == "storyos"]
    assert len(storyos_entries) == 1
    assert storyos_entries[0].path == wd.resolve()


def test_discover_returns_initiative_linked_when_no_standalone(tmp_path: Path) -> None:
    """When a repo has ONLY an initiative-linked `_repo-<name>.md`
    file (no standalone working dir), discovery still surfaces it.
    This is the realistic case where a repo's standalone dir got
    deactivated and now only the initiative reference remains."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    mission_control = tenant / "firstpersonsf" / "mission-control"
    mission_control.mkdir(parents=True)
    (mission_control / "_repo-mc-2.md").write_text(
        "[FirstPersonSF/mc-2](https://github.com/FirstPersonSF/mc-2)\n",
        encoding="utf-8",
    )

    found = discover_cp_working_dirs(tenant)
    mc2_entries = [d for d in found if d.repo_name == "mc-2"]
    assert len(mc2_entries) == 1
    assert mc2_entries[0].path == mission_control.resolve()


def test_link_local_wires_multirepo_engagement(tmp_path: Path) -> None:
    """End-to-end: two source repos sharing one multi-repo engagement
    dir each get a `.cp-link` pointing at the shared engagement dir."""
    tenant = tmp_path / "cp-tenant"
    tenant.mkdir()
    engagement_dir = make_multirepo_engagement(
        tenant,
        scope="1p",
        dir_slug="ggl-5136-go-safety-website",
        repo_names=["ggl-5136-ai-pipeline", "ggl-5136-events-calendar"],
    )
    pipeline_repo = make_source_repo(tmp_path, "ggl-5136-ai-pipeline")
    calendar_repo = make_source_repo(tmp_path, "ggl-5136-events-calendar")

    config = make_config(tenant, {
        "ggl-5136-ai-pipeline": pipeline_repo,
        "ggl-5136-events-calendar": calendar_repo,
    })
    results = link_local(config)

    assert len(results) == 2
    expected_target = str(engagement_dir.resolve())
    for r in results:
        assert (r.source_repo_path / ".cp-link").read_text(encoding="utf-8").strip() == expected_target
        assert r.cp_working_dir == engagement_dir.resolve()
        assert r.wrote_link is True


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
