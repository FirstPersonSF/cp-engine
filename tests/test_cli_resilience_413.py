"""Read-only per-project commands survive unrelated local state (#413).

2026-11-01 and 11-05, mid-wrap-up of cnc-5239:
1. With 1Password locked, `cxp commitments-sweep` ended in a 40-line
   `BackendUnavailable` traceback instead of one line naming the cause and the
   cp-hosted `commitments_sweep` that would have worked.
2. `commitments-sweep`, `spine-lint`, `seal-sweep` and `exec-lint` all stopped
   on `Local path for project 'storyos' does not exist` — a clone the
   workstream being wrapped up never touches.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cp_engine import LocalPathNotFound, load
from cp_engine.cli import main

CP_MD = """---
Project: x
---

## Exec Summary

**Status:** fine.
"""


def _tenant(root: Path, *, storyos_path: Path, op_block: bool = False) -> None:
    """A canonic tenant tracking a `storyos` repo whose clone is at `storyos_path`."""
    (root / ".cp-engine.toml").write_text(
        '[tenant]\nname = "canonic"\n\n[engine]\nversion = "~= 0.1"\n\n'
        '[sync]\nbackend = "mc-2"\ncron = "0 * * * *"\n\n'
        '[sync.mc_2]\nsupabase_project_ref = "abc123"\n\n'
        '[[projects]]\ncode = "storyos"\ngithub = "Canonic-OS/storyos"\n'
    )
    local = f'[repos]\nstoryos = "{storyos_path}"\n'
    if op_block:
        local += '\n[supabase]\nurl_ref = "op://v/i/url"\nservice_key_ref = "op://v/i/key"\n'
    (root / ".cp-engine.local.toml").write_text(local)
    program = root / "canonic" / "cnc-9004-storyos"
    site = program / "cnc-5239-canonic-site"
    site.mkdir(parents=True)
    (program / "cp.md").write_text(CP_MD)
    (program / "_repo-storyos.md").write_text("# storyos\n")
    (site / "cp.md").write_text(CP_MD)


@pytest.fixture
def no_supabase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"):
        monkeypatch.delenv(var, raising=False)


# ── 1 · a locked 1Password is one line, naming the hosted fallback ──────


def _locked_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake `op` that refuses the way a signed-out 1Password does."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "op"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '[ERROR] 2026/11/01 09:00:00 You are not currently signed in.' >&2\n"
        "exit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize(
    ("command", "verb"),
    [
        ("commitments-sweep", "commitments_sweep"),
        ("spine-lint", "spine_lint"),
        ("seal-sweep", "seal_sweep"),
    ],
)
def test_locked_1password_is_one_line_naming_the_hosted_verb(
    tmp_path, monkeypatch, no_supabase_env, command, verb
):
    clone = tmp_path / "storyos-clone"
    clone.mkdir()
    _tenant(tmp_path, storyos_path=clone, op_block=True)
    _locked_op(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, [command, "cnc-5239"])

    # No traceback: the run ends in a clean exit, not the raised error.
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    lines = [ln for ln in result.stderr.splitlines()
             if ln.startswith(f"cxp {command}:")]
    assert len(lines) == 1, result.stderr
    line = lines[0]
    assert "not currently signed in" in line  # op's own words, the cause
    assert "op signin" in line
    assert f"`{verb}` on the cp-hosted connector" in line
    assert "Traceback" not in result.output


def test_tenant_wide_sweep_is_one_line_too(tmp_path, monkeypatch, no_supabase_env):
    clone = tmp_path / "storyos-clone"
    clone.mkdir()
    _tenant(tmp_path, storyos_path=clone, op_block=True)
    _locked_op(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["commitments-sweep", "--all"])
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert "`commitments_sweep` on the cp-hosted connector" in result.stderr


def test_line_names_no_verb_for_an_unmapped_command():
    from cp_engine.cli import _backend_unavailable_line

    line = _backend_unavailable_line("feeds-sweep", RuntimeError("a\n  b"))
    assert line == "cxp feeds-sweep: can't reach Mission Control from this machine — a b"


# ── 2 · an unrelated missing clone warns once and carries on ────────────


def test_exec_lint_carries_on_past_an_unrelated_missing_clone(tmp_path, monkeypatch):
    _tenant(tmp_path, storyos_path=tmp_path / "gone" / "storyos")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["exec-lint", "cnc-5239"])

    assert result.exit_code == 0, result.output
    assert "cnc-5239 —" in result.stdout
    notes = [ln for ln in result.stderr.splitlines() if "storyos" in ln]
    assert len(notes) == 1, result.stderr
    assert "carrying on" in notes[0]


def test_exec_lint_still_fails_when_the_missing_clone_is_the_workstreams_own(
    tmp_path, monkeypatch
):
    """cnc-9004-storyos carries `_repo-storyos.md`: its clone is the one in play."""
    _tenant(tmp_path, storyos_path=tmp_path / "gone" / "storyos")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["exec-lint", "cnc-9004-storyos"])

    assert result.exit_code == 1
    assert "storyos" in result.stderr
    assert "not on this machine" in result.stderr


def test_exec_lint_still_fails_when_acting_on_the_repo_code_itself(tmp_path, monkeypatch):
    _tenant(tmp_path, storyos_path=tmp_path / "gone" / "storyos")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["exec-lint", "storyos"])
    assert result.exit_code == 1
    assert "not on this machine" in result.stderr


def test_strict_load_is_unchanged_for_writers(tmp_path):
    """Writers (sync, render, …) keep calling `load()` bare: still a hard fail."""
    _tenant(tmp_path, storyos_path=tmp_path / "gone" / "storyos")
    with pytest.raises(LocalPathNotFound):
        load(tmp_path)


def test_tolerant_load_reports_the_missing_path_and_skips_it(tmp_path):
    _tenant(tmp_path, storyos_path=tmp_path / "gone" / "storyos")
    (tmp_path / ".cp-engine.local.toml").write_text(
        (tmp_path / ".cp-engine.local.toml").read_text()
        + f'\n[local-repos]\nunf-forge = "{tmp_path / "gone" / "unf"}"\n'
    )
    missing: list = []
    cfg = load(tmp_path, missing_paths=missing)
    assert [name for name, _ in missing] == ["unf-forge", "storyos"]
    assert cfg.projects[0].local_path is None
    assert "unf-forge" not in cfg.local_repos


def test_missing_mc2_clone_is_named_as_missing_not_unconfigured(
    tmp_path, monkeypatch, no_supabase_env
):
    clone = tmp_path / "storyos-clone"
    clone.mkdir()
    _tenant(tmp_path, storyos_path=clone)
    gone = tmp_path / "gone" / "mc-2"
    (tmp_path / ".cp-engine.local.toml").write_text(
        (tmp_path / ".cp-engine.local.toml").read_text()
        + f'\n[local-repos]\n"mc-2" = "{gone}"\n'
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["commitments-sweep", "cnc-5239"])

    assert result.exit_code == 1
    assert "no MC-2 clone configured" not in result.stderr
    assert f"is not on this machine: {gone}" in result.stderr
