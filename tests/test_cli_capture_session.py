"""`cxp capture-session` CLI conventions (#325).

Two frictions, each a failed guess per use:

- every other per-project verb (`exec-lint`, `spine-lint`,
  `commitments-sweep`) takes `<code>` positionally; capture-session didn't;
- the `--user` default was the first token of `git config user.name`, which
  on Drew's machine is the GitHub handle `drewcanon` — so session files went
  out as `…-drewcanon.md` beside the tenant's established `…-drew.md`.

Git identity is supplied through a throwaway global config rather than a
monkeypatch, so these run the same `git config` the command really runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cp_engine.cli import main
from cp_engine.spine import find_spine_dir

SUMMARY = "## Session: 2026-09-29 10:00, Someone\n\n### What we did\nThings.\n"


def _tenant(tmp_path: Path, extra_toml: str = "") -> Path:
    root = tmp_path / "cp"
    root.mkdir()
    (root / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n' + extra_toml,
        encoding="utf-8",
    )
    wd = root / "1p" / "google" / "ggl-5168-activation"
    wd.mkdir(parents=True)
    (wd / "cp.md").write_text("# Activation\n\n**Last session:** _<date>_\n")
    # A git repo (capture lists the working dir's files through git), but
    # with NO local identity — the global config set by `git_identity` is
    # what every `git config` call resolves.
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture
def git_identity(tmp_path, monkeypatch):
    """Set the git identity every `git config` call in the test resolves."""
    def _set(name: str, email: str) -> None:
        cfg = tmp_path / "gitconfig"
        cfg.write_text(f"[user]\n\tname = {name}\n\temail = {email}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return _set


def _capture(root: Path, args: list[str], tmp_path: Path, monkeypatch):
    summary = tmp_path / "summary.md"
    summary.write_text(SUMMARY)
    monkeypatch.chdir(root)
    return CliRunner().invoke(
        main, ["capture-session", *args, "--summary-file", str(summary), "--no-commit"],
    )


def _wd_args(root: Path) -> list[str]:
    """The pre-#325 spelling, so the --user tests isolate the default rule
    from the new positional argument."""
    return ["--working-dir", str(find_spine_dir(root, "ggl-5168"))]


def _written(root: Path) -> list[str]:
    return sorted(p.name for p in root.rglob("sessions/*.md"))


# ── positional <code> ────────────────────────────────────────────────


def test_code_is_positional_like_the_other_per_project_verbs():
    """Derived from the live commands, not restated: capture-session's first
    parameter has the same shape as exec-lint's."""
    ref = main.get_command(None, "exec-lint").params[0]
    got = main.get_command(None, "capture-session").params[0]
    assert isinstance(ref, click.Argument) and isinstance(got, click.Argument)
    assert got.name == ref.name == "code"


def test_positional_code_writes_into_that_workstreams_sessions(
    tmp_path, monkeypatch, git_identity,
):
    git_identity("Test User", "test@example.com")
    root = _tenant(tmp_path)
    result = _capture(root, ["ggl-5168", "--user", "Drew"], tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    sessions = find_spine_dir(root, "ggl-5168") / "sessions"
    assert [p.name.endswith("-drew.md") for p in sessions.glob("*.md")] == [True]


def test_code_and_working_dir_together_is_refused(tmp_path, monkeypatch, git_identity):
    git_identity("Test User", "test@example.com")
    root = _tenant(tmp_path)
    wd = find_spine_dir(root, "ggl-5168")
    result = _capture(
        root, ["ggl-5168", "--working-dir", str(wd), "--user", "Drew"],
        tmp_path, monkeypatch,
    )
    assert result.exit_code == 2
    assert _written(root) == []


def test_unknown_code_fails_without_writing(tmp_path, monkeypatch, git_identity):
    git_identity("Test User", "test@example.com")
    root = _tenant(tmp_path)
    result = _capture(root, ["zzz-0000", "--user", "Drew"], tmp_path, monkeypatch)
    assert result.exit_code == 2
    assert _written(root) == []


def test_working_dir_flag_still_works(tmp_path, monkeypatch, git_identity):
    git_identity("Test User", "test@example.com")
    root = _tenant(tmp_path)
    wd = find_spine_dir(root, "ggl-5168")
    result = _capture(
        root, ["--working-dir", str(wd), "--user", "Drew"], tmp_path, monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert len(list((wd / "sessions").glob("*-drew.md"))) == 1


# ── --user default ───────────────────────────────────────────────────


def test_a_github_handle_in_user_name_resolves_through_the_roster(
    tmp_path, monkeypatch, git_identity,
):
    """The recorded case: user.name `drewcanon`, email `drew@…`, `drew` on
    the `[team]` roster → `…-drew.md`, not `…-drewcanon.md`."""
    git_identity("drewcanon", "drew@canonic-os.com")
    root = _tenant(tmp_path, '[team]\nmembers = ["drew", "tony"]\n')
    result = _capture(root, _wd_args(root), tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    [name] = _written(root)
    assert name.endswith("-drew.md")


def test_this_machines_local_repos_section_names_the_author(
    tmp_path, monkeypatch, git_identity,
):
    """A `[local-repos.<user>]` whose clone paths exist here identifies the
    machine even when git's identity matches nobody on the roster."""
    git_identity("someone-else", "x@example.com")
    clone = tmp_path / "clone"
    clone.mkdir()
    root = _tenant(
        tmp_path,
        f'[local-repos.tony]\n"mc-2" = "{clone}"\n'
        f'[local-repos.drew]\n"mc-2" = "{tmp_path / "not-on-this-machine"}"\n',
    )
    result = _capture(root, _wd_args(root), tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    [name] = _written(root)
    assert name.endswith("-tony.md")


def test_no_roster_keeps_the_legacy_first_token(tmp_path, monkeypatch, git_identity):
    git_identity("Marcello Rossi", "m@example.com")
    root = _tenant(tmp_path)
    result = _capture(root, _wd_args(root), tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    [name] = _written(root)
    assert name.endswith("-marcello.md")


def test_explicit_user_still_wins(tmp_path, monkeypatch, git_identity):
    git_identity("drewcanon", "drew@canonic-os.com")
    root = _tenant(tmp_path, '[team]\nmembers = ["drew"]\n')
    result = _capture(root, [*_wd_args(root), "--user", "Tony"], tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    [name] = _written(root)
    assert name.endswith("-tony.md")
