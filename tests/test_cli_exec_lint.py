# tests/test_cli_exec_lint.py — `cp exec-lint` + the render-path budget pass
from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main
from cp_engine.cli_cmds.core import _exec_summary_warnings
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START


def _cp_md(status: str) -> str:
    return (
        "# Test project\n\n"
        f"{EXEC_SUMMARY_START}\n"
        "## Exec Summary  ·  updated 2026-07-25\n\n"
        "**Objective:** Ship the thing.\n"
        f"**Status:** {status}\n"
        f"{EXEC_SUMMARY_END}\n"
    )


def _tenant(tmp_path: Path, status: str) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text(_cp_md(status), encoding="utf-8")
    return proj


_FAT_STATUS = " ".join(f"word{i}" for i in range(700))


def test_exec_lint_warns_on_fat_status_but_exits_zero(tmp_path, monkeypatch):
    _tenant(tmp_path, _FAT_STATUS)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["exec-lint", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "1 exec-summary budget warning(s)" in result.output
    assert "700 words" in result.output and "budget 100" in result.output


def test_exec_lint_compliant_is_clean(tmp_path, monkeypatch):
    _tenant(tmp_path, "Deck built; awaiting the pillar ruling.")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["exec-lint", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "within budget" in result.output


def test_exec_lint_unknown_project_exits_nonzero(tmp_path, monkeypatch):
    _tenant(tmp_path, "fine")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["exec-lint", "nope-9999"])
    assert result.exit_code == 1


def test_render_pass_scans_live_projects_and_skips_inactive(tmp_path):
    proj = _tenant(tmp_path, _FAT_STATUS)
    # A parked project with the same fat summary must NOT surface.
    parked = tmp_path / "1p" / "inactive" / "old-1111" / "cp.md"
    parked.parent.mkdir(parents=True)
    parked.write_text(_cp_md(_FAT_STATUS), encoding="utf-8")

    out = _exec_summary_warnings(tmp_path)
    assert len(out) == 1
    assert str(proj.relative_to(tmp_path)) in out[0]
    assert "budget 100" in out[0]


def test_render_pass_silent_on_compliant_tenant(tmp_path):
    _tenant(tmp_path, "Deck built; awaiting the pillar ruling.")
    assert _exec_summary_warnings(tmp_path) == []
