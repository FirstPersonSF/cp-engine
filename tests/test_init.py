"""Tests for `cp_engine.init`.

Inject a fake `prompt` callable instead of stubbing stdin so tests are
deterministic and don't pull in pytest-stdin or similar.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import tomllib

from cp_engine import (
    CommittedConfigMissing,
    InitAborted,
    load,
    run_init,
)


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def write_committed(
    tenant_root: Path,
    *,
    name: str = "1p",
    projects: list[tuple[str, str]] | None = None,
) -> None:
    """Write a minimal `.cp-engine.toml`."""
    if projects is None:
        projects = [("ggl-5168", "FirstPersonSF/ggl-5168")]

    lines = [
        "[tenant]",
        f'name = "{name}"',
        "",
        "[engine]",
        'version = "~= 0.1"',
        "",
        "[sync]",
        'backend = "mc-2"',
        'cron = "0 * * * *"',
        "",
        "[sync.mc_2]",
        'supabase_project_ref = "ref"',
        "",
    ]
    for code, github in projects:
        lines.append("[[projects]]")
        lines.append(f'code = "{code}"')
        lines.append(f'github = "{github}"')
        lines.append("")

    (tenant_root / ".cp-engine.toml").write_text("\n".join(lines))


class ScriptedPrompts:
    """Iterator-backed prompt fake — yields each scripted answer in turn."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        try:
            return next(self._answers)
        except StopIteration as exc:
            raise AssertionError(
                f"ScriptedPrompts ran out of answers; called with {prompt!r}"
            ) from exc


# ──────────────────────────────────────────────────────────────────────
#  Happy path
# ──────────────────────────────────────────────────────────────────────


def test_fresh_init_writes_local_with_supplied_paths(tmp_path: Path) -> None:
    project_dir = tmp_path / "ggl-5168"
    project_dir.mkdir()
    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])

    out = io.StringIO()
    run_init(
        tmp_path,
        prompt=ScriptedPrompts([str(project_dir)]),
        out=out,
    )

    local = (tmp_path / ".cp-engine.local.toml").read_text()
    parsed = tomllib.loads(local)
    assert parsed["repos"] == {"ggl-5168": str(project_dir)}

    # config.load() sees the merged config without errors
    cfg = load(tmp_path)
    assert len(cfg.projects) == 1
    assert cfg.projects[0].local_path == project_dir.resolve()


def test_init_skip_writes_empty_string(tmp_path: Path) -> None:
    write_committed(tmp_path, projects=[("mc-2", "FirstPersonSF/mc-2")])

    out = io.StringIO()
    run_init(tmp_path, prompt=ScriptedPrompts([""]), out=out)

    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"]["mc-2"] == ""

    cfg = load(tmp_path)
    assert cfg.projects[0].local_path is None  # skipped → None


def test_init_with_tilde_path_stored_unresolved(tmp_path: Path, monkeypatch) -> None:
    """Init stores the user's typed path (~/...), not the resolved one,
    so future filesystem moves don't strand the config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = tmp_path / "storyos"
    project_dir.mkdir()

    write_committed(tmp_path, projects=[("storyos", "CanonicOS/storyos")])

    out = io.StringIO()
    run_init(tmp_path, prompt=ScriptedPrompts(["~/storyos"]), out=out)

    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"]["storyos"] == "~/storyos"  # stored as typed


# ──────────────────────────────────────────────────────────────────────
#  Re-init: only prompt for new projects
# ──────────────────────────────────────────────────────────────────────


def test_reinit_skips_already_configured_projects(tmp_path: Path) -> None:
    p1 = tmp_path / "ggl-5168"
    p1.mkdir()
    p2 = tmp_path / "ibx-5153"
    p2.mkdir()

    # First init: one project committed and configured
    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])
    run_init(tmp_path, prompt=ScriptedPrompts([str(p1)]), out=io.StringIO())

    # Add a second project to committed config; re-init
    write_committed(
        tmp_path,
        projects=[
            ("ggl-5168", "FirstPersonSF/ggl-5168"),
            ("ibx-5153", "FirstPersonSF/ibx-5153"),
        ],
    )
    prompts = ScriptedPrompts([str(p2)])  # only one answer — proves only one prompt
    run_init(tmp_path, prompt=prompts, out=io.StringIO())

    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"] == {
        "ggl-5168": str(p1),  # preserved from first init
        "ibx-5153": str(p2),
    }


def test_reinit_when_nothing_to_do_is_a_noop(tmp_path: Path) -> None:
    project_dir = tmp_path / "ggl-5168"
    project_dir.mkdir()
    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])

    # First init
    run_init(tmp_path, prompt=ScriptedPrompts([str(project_dir)]), out=io.StringIO())
    first_contents = (tmp_path / ".cp-engine.local.toml").read_text()

    # Re-init with no new projects: should not call prompt at all
    out = io.StringIO()
    prompts = ScriptedPrompts([])  # zero answers — proves zero prompts
    run_init(tmp_path, prompt=prompts, out=out)

    assert "Nothing to do" in out.getvalue()
    assert (tmp_path / ".cp-engine.local.toml").read_text() == first_contents


# ──────────────────────────────────────────────────────────────────────
#  Strict path validation
# ──────────────────────────────────────────────────────────────────────


def test_strict_reprompts_on_missing_path_then_succeeds(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])

    out = io.StringIO()
    run_init(
        tmp_path,
        prompt=ScriptedPrompts([str(tmp_path / "does-not-exist"), str(real)]),
        out=out,
    )

    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"]["ggl-5168"] == str(real)
    assert "Path does not exist" in out.getvalue()


def test_strict_reprompts_on_file_not_directory_then_succeeds(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    a_file = tmp_path / "actually-a-file"
    a_file.write_text("not a dir")

    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])

    out = io.StringIO()
    run_init(tmp_path, prompt=ScriptedPrompts([str(a_file), str(real)]), out=out)

    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"]["ggl-5168"] == str(real)
    assert "Not a directory" in out.getvalue()


def test_too_many_bad_paths_aborts_and_saves_partial(tmp_path: Path) -> None:
    """3 bad attempts → InitAborted. Partial progress (a previous project's
    successful path) is saved before the abort."""
    p1 = tmp_path / "ggl-5168"
    p1.mkdir()
    write_committed(
        tmp_path,
        projects=[
            ("ggl-5168", "FirstPersonSF/ggl-5168"),
            ("ibx-5153", "FirstPersonSF/ibx-5153"),
        ],
    )

    bad = str(tmp_path / "no-such-dir")
    out = io.StringIO()
    with pytest.raises(InitAborted, match="Too many invalid paths for ibx-5153"):
        run_init(
            tmp_path,
            prompt=ScriptedPrompts([str(p1), bad, bad, bad]),
            out=out,
        )

    # ggl-5168 should be saved before the abort
    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"]["ggl-5168"] == str(p1)
    assert "ibx-5153" not in parsed["repos"]


# ──────────────────────────────────────────────────────────────────────
#  Failure when not in a tenant repo
# ──────────────────────────────────────────────────────────────────────


def test_init_fails_when_no_committed_config(tmp_path: Path) -> None:
    with pytest.raises(CommittedConfigMissing):
        run_init(tmp_path, prompt=ScriptedPrompts([]), out=io.StringIO())


# ──────────────────────────────────────────────────────────────────────
#  Round-trip safety
# ──────────────────────────────────────────────────────────────────────


def test_reinit_preserves_user_comments_and_formatting(tmp_path: Path) -> None:
    """User edits the local file by hand (adds comments, reorders entries).
    Re-init for a new project must not destroy that work."""
    p1 = tmp_path / "ggl-5168"
    p1.mkdir()
    p2 = tmp_path / "ibx-5153"
    p2.mkdir()

    write_committed(tmp_path, projects=[("ggl-5168", "FirstPersonSF/ggl-5168")])
    run_init(tmp_path, prompt=ScriptedPrompts([str(p1)]), out=io.StringIO())

    # User hand-edits: adds a comment and a blank line.
    local_path = tmp_path / ".cp-engine.local.toml"
    edited = (
        local_path.read_text()
        + "\n# My personal note: the ggl-5168 path lives on Dropbox.\n"
    )
    local_path.write_text(edited)

    # Re-init with a new project added.
    write_committed(
        tmp_path,
        projects=[
            ("ggl-5168", "FirstPersonSF/ggl-5168"),
            ("ibx-5153", "FirstPersonSF/ibx-5153"),
        ],
    )
    run_init(tmp_path, prompt=ScriptedPrompts([str(p2)]), out=io.StringIO())

    final = local_path.read_text()
    assert "My personal note" in final
    parsed = tomllib.loads(final)
    assert parsed["repos"]["ggl-5168"] == str(p1)
    assert parsed["repos"]["ibx-5153"] == str(p2)


# ──────────────────────────────────────────────────────────────────────
#  Non-interactive mode (CI bootstrap)
# ──────────────────────────────────────────────────────────────────────


def test_non_interactive_marks_everything_skipped(tmp_path: Path) -> None:
    write_committed(
        tmp_path,
        projects=[
            ("ggl-5168", "FirstPersonSF/ggl-5168"),
            ("ibx-5153", "FirstPersonSF/ibx-5153"),
        ],
    )

    # No prompts called at all in non-interactive mode.
    prompts = ScriptedPrompts([])
    out = io.StringIO()
    run_init(tmp_path, prompt=prompts, out=out, interactive=False)

    parsed = tomllib.loads((tmp_path / ".cp-engine.local.toml").read_text())
    assert parsed["repos"] == {"ggl-5168": "", "ibx-5153": ""}
    assert prompts.calls == []
