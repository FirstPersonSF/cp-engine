"""Tests for `cp_engine.config`.

Each test builds a temporary tenant directory with the two config files,
then asserts on the merged TenantConfig (or the raised error).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cp_engine import (
    CommittedConfigInvalid,
    CommittedConfigMissing,
    EngineVersionMismatch,
    LocalConfigInvalid,
    LocalConfigMissing,
    LocalPathNotFound,
    ProjectsMissingFromLocal,
    __version__,
    load,
)


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def write_committed(
    tenant_root: Path,
    *,
    name: str = "1p",
    display: str | None = "First Person Jobs",
    engine_version: str | None = "~= 0.1",
    backend: str = "mc-2",
    supabase_ref: str | None = "abc123",
    projects: list[tuple[str, str]] | None = None,
    extra: str = "",
) -> None:
    """Write a `.cp-engine.toml` with the given parameters."""
    if projects is None:
        projects = [("ggl-5168", "FirstPersonSF/ggl-5168")]

    lines = [
        "[tenant]",
        f'name = "{name}"',
    ]
    if display is not None:
        lines.append(f'display = "{display}"')
    lines.append("")
    lines.append("[engine]")
    if engine_version is not None:
        lines.append(f'version = "{engine_version}"')
    lines.append("")
    lines.append("[sync]")
    lines.append(f'backend = "{backend}"')
    lines.append('cron = "0 * * * *"')
    if backend == "mc-2" and supabase_ref is not None:
        lines.append("")
        lines.append("[sync.mc_2]")
        lines.append(f'supabase_project_ref = "{supabase_ref}"')
    lines.append("")
    for code, github in projects:
        lines.append("[[projects]]")
        lines.append(f'code = "{code}"')
        lines.append(f'github = "{github}"')
        lines.append("")
    if extra:
        lines.append(extra)

    (tenant_root / ".cp-engine.toml").write_text("\n".join(lines) + "\n")


def write_local(tenant_root: Path, repos: dict[str, str]) -> None:
    lines = ["[repos]"]
    for code, path in repos.items():
        # Use double-quoted strings; paths may contain ~ which is fine.
        lines.append(f'"{code}" = "{path}"')
    (tenant_root / ".cp-engine.local.toml").write_text("\n".join(lines) + "\n")


# ──────────────────────────────────────────────────────────────────────
#  Happy path
# ──────────────────────────────────────────────────────────────────────


def test_load_happy_path(tmp_path: Path) -> None:
    project_dir = tmp_path / "ggl-5168"
    project_dir.mkdir()

    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])
    write_local(tmp_path, {"ggl-5168": str(project_dir)})

    cfg = load(tmp_path)

    assert cfg.name == "1p"
    assert cfg.display == "First Person Jobs"
    assert cfg.engine_version_constraint == "~= 0.1"
    assert cfg.sync.backend == "mc-2"
    assert cfg.sync.cron == "0 * * * *"
    assert cfg.sync.mc_2_supabase_project_ref == "abc123"
    assert len(cfg.projects) == 1
    p = cfg.projects[0]
    assert p.code == "ggl-5168"
    assert p.github == "FirstPersonSF/ggl-5168"
    assert p.local_path == project_dir.resolve()
    assert cfg.root == tmp_path.resolve()


# ──────────────────────────────────────────────────────────────────────
#  display field
# ──────────────────────────────────────────────────────────────────────


def test_display_optional_falls_back_to_titled_name(tmp_path: Path) -> None:
    project_dir = tmp_path / "ggl-5168"
    project_dir.mkdir()
    write_committed(tmp_path, name="cp-canonic", display=None)
    write_local(tmp_path, {"ggl-5168": str(project_dir)})

    cfg = load(tmp_path)

    assert cfg.display == "Cp Canonic"  # title-cased fallback


# ──────────────────────────────────────────────────────────────────────
#  Skipped projects
# ──────────────────────────────────────────────────────────────────────


def test_skipped_project_local_path_is_none(tmp_path: Path) -> None:
    """An empty-string local path means 'I don't have access — skip'."""
    write_committed(tmp_path, projects=[("mc-2", "FirstPersonSF/mc-2")])
    write_local(tmp_path, {"mc-2": ""})

    cfg = load(tmp_path)

    assert len(cfg.projects) == 1
    assert cfg.projects[0].local_path is None


# ──────────────────────────────────────────────────────────────────────
#  Symlinks
# ──────────────────────────────────────────────────────────────────────


def test_symlink_resolved(tmp_path: Path) -> None:
    """Dropbox-symlink case: the configured path is a symlink to elsewhere."""
    real = tmp_path / "real-storyos"
    real.mkdir()
    link = tmp_path / "linked-storyos"
    link.symlink_to(real)

    write_committed(tmp_path, projects=[("storyos", "CanonicOS/storyos")])
    write_local(tmp_path, {"storyos": str(link)})

    cfg = load(tmp_path)

    assert cfg.projects[0].local_path == real.resolve()


# ──────────────────────────────────────────────────────────────────────
#  Drift warning
# ──────────────────────────────────────────────────────────────────────


def test_drift_warning_for_unknown_local_project(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Local has paths committed config doesn't know about → warn, ignore."""
    p_dir = tmp_path / "ggl-5168"
    p_dir.mkdir()
    orphan_dir = tmp_path / "orphan"
    orphan_dir.mkdir()

    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])
    write_local(tmp_path, {"ggl-5168": str(p_dir), "old-project": str(orphan_dir)})

    with caplog.at_level(logging.WARNING, logger="cp_engine.config"):
        cfg = load(tmp_path)

    assert any("old-project" in m for m in caplog.messages)
    # Drift is a warning, not a failure: only ggl-5168 surfaces.
    assert {p.code for p in cfg.projects} == {"ggl-5168"}


# ──────────────────────────────────────────────────────────────────────
#  Hard failures — committed file
# ──────────────────────────────────────────────────────────────────────


def test_committed_missing(tmp_path: Path) -> None:
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigMissing):
        load(tmp_path)


def test_committed_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine.toml").write_text("this is not = valid TOML [[[\n")
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigInvalid):
        load(tmp_path)


def test_committed_missing_tenant_section(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[engine]\nversion = "~= 0.1"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "x"\n'
    )
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigInvalid, match="missing \\[tenant\\]"):
        load(tmp_path)


def test_committed_missing_engine_version(tmp_path: Path) -> None:
    write_committed(tmp_path, engine_version=None, projects=[])
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigInvalid, match="version"):
        load(tmp_path)


def test_committed_invalid_backend(tmp_path: Path) -> None:
    write_committed(tmp_path, backend="invalid", supabase_ref=None, projects=[])
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigInvalid, match="backend"):
        load(tmp_path)


def test_committed_mc2_without_supabase_ref(tmp_path: Path) -> None:
    write_committed(tmp_path, backend="mc-2", supabase_ref=None, projects=[])
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigInvalid, match="supabase_project_ref"):
        load(tmp_path)


def test_committed_github_issues_does_not_require_supabase(tmp_path: Path) -> None:
    """github-issues backend doesn't need the mc-2-specific block."""
    write_committed(tmp_path, backend="github-issues", supabase_ref=None, projects=[])
    write_local(tmp_path, {})
    cfg = load(tmp_path)
    assert cfg.sync.backend == "github-issues"
    assert cfg.sync.mc_2_supabase_project_ref is None


def test_committed_invalid_project_github_format(tmp_path: Path) -> None:
    write_committed(tmp_path, projects=[("ggl-5168", "no-slash-here")])
    write_local(tmp_path, {})
    with pytest.raises(CommittedConfigInvalid, match="owner/repo"):
        load(tmp_path)


def test_committed_duplicate_project_codes(tmp_path: Path) -> None:
    write_committed(
        tmp_path,
        projects=[
            ("ggl-5168", "FirstPersonSF/a"),
            ("ggl-5168", "FirstPersonSF/b"),
        ],
    )
    write_local(tmp_path, {"ggl-5168": ""})
    with pytest.raises(CommittedConfigInvalid, match="duplicate"):
        load(tmp_path)


# ──────────────────────────────────────────────────────────────────────
#  Hard failures — local file
# ──────────────────────────────────────────────────────────────────────


def test_local_missing(tmp_path: Path) -> None:
    write_committed(tmp_path)
    with pytest.raises(LocalConfigMissing, match="cp init"):
        load(tmp_path)


def test_local_invalid_toml(tmp_path: Path) -> None:
    write_committed(tmp_path)
    (tmp_path / ".cp-engine.local.toml").write_text("[[broken\n")
    with pytest.raises(LocalConfigInvalid):
        load(tmp_path)


def test_projects_missing_from_local(tmp_path: Path) -> None:
    write_committed(
        tmp_path,
        projects=[
            ("ggl-5168", "FirstPersonSF/ggl-5168"),
            ("ibx-5153", "FirstPersonSF/ibx-5153"),
        ],
    )
    p_dir = tmp_path / "ggl-5168"
    p_dir.mkdir()
    write_local(tmp_path, {"ggl-5168": str(p_dir)})  # missing ibx-5153

    with pytest.raises(ProjectsMissingFromLocal) as info:
        load(tmp_path)
    assert info.value.missing == ("ibx-5153",)


def test_local_path_not_found(tmp_path: Path) -> None:
    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])
    write_local(tmp_path, {"ggl-5168": str(tmp_path / "does-not-exist")})

    with pytest.raises(LocalPathNotFound) as info:
        load(tmp_path)
    assert info.value.code == "ggl-5168"


def test_local_path_with_tilde_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`~` in local paths expands to $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = tmp_path / "ggl-5168"
    project_dir.mkdir()

    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])
    write_local(tmp_path, {"ggl-5168": "~/ggl-5168"})

    cfg = load(tmp_path)
    assert cfg.projects[0].local_path == project_dir.resolve()


# ──────────────────────────────────────────────────────────────────────
#  Engine version enforcement
# ──────────────────────────────────────────────────────────────────────


def test_engine_version_constraint_satisfied(tmp_path: Path) -> None:
    """The installed engine matches the tenant's pin → load succeeds."""
    p_dir = tmp_path / "ggl-5168"
    p_dir.mkdir()
    write_committed(tmp_path, engine_version=f"== {__version__}")
    write_local(tmp_path, {"ggl-5168": str(p_dir)})

    cfg = load(tmp_path)
    assert cfg.engine_version_constraint == f"== {__version__}"


def test_engine_version_constraint_violated(tmp_path: Path) -> None:
    """A pin the installed engine can't satisfy → fail loudly."""
    write_committed(tmp_path, engine_version=">= 99.0", projects=[])
    write_local(tmp_path, {})

    with pytest.raises(EngineVersionMismatch) as info:
        load(tmp_path)
    assert info.value.installed == __version__
    assert info.value.required == ">= 99.0"


def test_engine_version_invalid_specifier(tmp_path: Path) -> None:
    write_committed(tmp_path, engine_version="not a real spec", projects=[])
    write_local(tmp_path, {})

    with pytest.raises(CommittedConfigInvalid, match="constraint"):
        load(tmp_path)
