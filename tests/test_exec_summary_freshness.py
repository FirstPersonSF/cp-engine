# tests/test_exec_summary_freshness.py — stamp vs. state (#251)
"""A one-field refresh advances the `· updated` stamp for the whole summary.

The fixture is the live 09-15 case in miniature: a summary fully authored on
2026-07-14, then a Status-only write on 2026-09-15 that moved the stamp. Its
`Next up` still names a July deadline and every freshness reader called it
current. These tests build that history in a real git repo, because the only
honest record of when a field last changed is the file's own history.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START
from cp_engine.clock import tenant_today

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

_PROJ = Path("1p") / "google" / "ggl-5188-calendar-maintenance"


def _region(stamp: str, status: str, where: str, next_up: str,
            blockers: str) -> str:
    return (
        "# ggl-5188 Calendar maintenance\n\n"
        f"{EXEC_SUMMARY_START}\n"
        f"## Exec Summary  ·  updated {stamp}\n\n"
        "**Last session:** _<date>_\n"
        "**Objective:** Keep the Go Safety calendar widgets current.\n"
        f"**Status:** {status}\n\n"
        f"**Where it stands:**\n- {where}\n\n"
        f"**Next up:**\n- {next_up}\n\n"
        f"**Blockers:**\n- {blockers}\n\n"
        "**Updates:**\n- 2026-07-14 — first authored.\n"
        f"{EXEC_SUMMARY_END}\n"
    )


_JULY = dict(
    status="Widgets built; waiting on Tony's spec.",
    where="Calendar/cards/tables widgets built and working.",
    next_up="Drew: widget updates per Tony's spec by Fri 7/17.",
    blockers="Tony's spec (due Tue) gates the build.",
)


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                   capture_output=True)


def _tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    (tmp_path / _PROJ).mkdir(parents=True)
    _git(tmp_path, "init", "-q")
    return tmp_path / _PROJ / "cp.md"


def _commit(repo: Path, cp_md: Path, text: str, when: str) -> None:
    cp_md.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "write", when=when)


def _status_only_history(tmp_path: Path) -> Path:
    """July authoring, then a September write that touched Status + stamp."""
    cp_md = _tenant(tmp_path)
    _commit(tmp_path, cp_md, _region("2026-07-14", **_JULY),
            "2026-07-14T12:00:00-07:00")
    september = dict(_JULY, status="Now folded into ggl-5197's $130K scope.")
    _commit(tmp_path, cp_md, _region("2026-09-15", **september),
            "2026-09-15T12:00:00-07:00")
    return cp_md


def _full_refresh_history(tmp_path: Path) -> Path:
    cp_md = _tenant(tmp_path)
    _commit(tmp_path, cp_md, _region("2026-07-14", **_JULY),
            "2026-07-14T12:00:00-07:00")
    _commit(tmp_path, cp_md, _region(
        "2026-09-15",
        status="Now folded into ggl-5197's $130K scope.",
        where="Calendar work moved to ggl-5197.",
        next_up="Close this job out once 5197's SOW signs.",
        blockers="None.",
    ), "2026-09-15T12:00:00-07:00")
    return cp_md


# ── the detector ─────────────────────────────────────────────────────────


def test_status_only_refresh_is_detected(tmp_path):
    from cp_engine.exec_summary_freshness import partial_refresh

    found = partial_refresh(_status_only_history(tmp_path))
    assert found is not None
    assert found.stamp == date(2026, 9, 15)
    assert found.body_changed == date(2026, 7, 14)
    assert found.lag_days == 63
    assert found.fields == ("Where it stands", "Next up", "Blockers")


def test_field_dates_come_from_blame(tmp_path):
    from cp_engine.exec_summary_freshness import blame_field_dates

    dates = blame_field_dates(_status_only_history(tmp_path))
    assert dates["Status"] == date(2026, 9, 15)
    assert dates["Next up"] == date(2026, 7, 14)
    assert dates["Objective"] == date(2026, 7, 14)


def test_full_refresh_is_clean(tmp_path):
    from cp_engine.exec_summary_freshness import partial_refresh

    assert partial_refresh(_full_refresh_history(tmp_path)) is None


def test_one_refreshed_state_field_is_enough(tmp_path):
    """`Blockers: None` may rightly survive many refreshes; one moved state
    field means the author looked at the state."""
    from cp_engine.exec_summary_freshness import partial_refresh

    cp_md = _tenant(tmp_path)
    _commit(tmp_path, cp_md, _region("2026-07-14", **_JULY),
            "2026-07-14T12:00:00-07:00")
    _commit(tmp_path, cp_md, _region(
        "2026-09-15", **dict(_JULY, next_up="Close out once 5197 signs.")),
        "2026-09-15T12:00:00-07:00")
    assert partial_refresh(cp_md) is None


def test_uncommitted_edit_counts_as_today(tmp_path):
    """A wrap-up runs the lint BEFORE it commits; the edit in the working
    tree is the refresh."""
    from cp_engine.exec_summary_freshness import partial_refresh

    cp_md = _tenant(tmp_path)
    _commit(tmp_path, cp_md, _region("2026-07-14", **_JULY),
            "2026-07-14T12:00:00-07:00")
    today = tenant_today().isoformat()
    cp_md.write_text(_region(
        today, status="Refreshed.", where="Fresh state.",
        next_up="Fresh move.", blockers="None."), encoding="utf-8")
    assert partial_refresh(cp_md) is None


def test_scaffold_state_fields_are_not_this_finding(tmp_path):
    """All-scaffold state fields are #190's PARTIAL, reported elsewhere."""
    from cp_engine.exec_summary_freshness import partial_refresh

    cp_md = _tenant(tmp_path)
    scaffold = dict(where="_<2-4 dense bullets>_", next_up="_<moves>_",
                    blockers="_<stuck>_")
    _commit(tmp_path, cp_md, _region("2026-07-01", status="_<phrase>_",
                                     **scaffold), "2026-07-01T12:00:00-07:00")
    _commit(tmp_path, cp_md, _region("2026-09-15", status="Real status.",
                                     **scaffold), "2026-09-15T12:00:00-07:00")
    assert partial_refresh(cp_md) is None


def test_shallow_clone_cannot_tell(tmp_path):
    """A --depth 1 clone blames every line to the boundary commit, which
    would read as a full refresh. It must say "cannot tell" instead."""
    from cp_engine.exec_summary_freshness import (
        blame_field_dates,
        partial_refresh,
    )

    src = tmp_path / "src"
    src.mkdir()
    _status_only_history(src)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1",
                    f"file://{src}", str(clone)], check=True,
                   capture_output=True)
    shallow_cp = clone / _PROJ / "cp.md"
    assert shallow_cp.is_file()
    assert blame_field_dates(shallow_cp) is None
    assert partial_refresh(shallow_cp) is None


def test_outside_git_cannot_tell(tmp_path):
    from cp_engine.exec_summary_freshness import blame_field_dates

    cp_md = tmp_path / "cp.md"
    cp_md.write_text(_region("2026-09-15", **_JULY), encoding="utf-8")
    assert blame_field_dates(cp_md) is None


def test_decorated_labels_are_read(tmp_path):
    """Field identity comes from exec-lint's one reader (#319)."""
    from cp_engine.exec_summary_freshness import partial_refresh

    cp_md = _tenant(tmp_path)
    july = _region("2026-07-14", **_JULY).replace(
        "**Next up:**", "**Next up (W29):**")
    _commit(tmp_path, cp_md, july, "2026-07-14T12:00:00-07:00")
    september = july.replace("2026-07-14\n", "2026-09-15\n", 1).replace(
        _JULY["status"], "Folded into ggl-5197.")
    _commit(tmp_path, cp_md, september, "2026-09-15T12:00:00-07:00")
    found = partial_refresh(cp_md)
    assert found is not None and "Next up" in found.fields


# ── the surfaces (these also run as the control against unfixed code) ────


def test_exec_lint_reports_the_overstated_stamp(tmp_path, monkeypatch):
    from cp_engine.cli import main

    _status_only_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["exec-lint", "ggl-5188"])
    assert result.exit_code == 0, result.output
    assert "stamp overstates it" in result.output
    assert "last changed 2026-07-14" in result.output
    assert "63d before the 2026-09-15 stamp" in result.output


def test_exec_lint_full_refresh_stays_clean(tmp_path, monkeypatch):
    from cp_engine.cli import main

    _full_refresh_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["exec-lint", "ggl-5188"])
    assert result.exit_code == 0, result.output
    assert "within budget" in result.output


def _planning_inputs(tmp_path: Path):
    from cp_engine.config import SyncConfig, TenantConfig
    from cp_engine.state import ProjectState

    config = TenantConfig(
        name="t", display="T", engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *",
                        mc_2_supabase_project_ref="ref"),
        projects=(), root=tmp_path,
    )
    state = ProjectState(
        code="ggl-5188-calendar-maintenance", name="Calendar maintenance",
        has_agreement=True, company_kind="client", company_code="GGL",
        company_name="Google", status="Open",
        owner="drew", last_touched=None, deadline=None,
    )
    return config, state


def test_planning_bundle_flags_partial_refresh(tmp_path):
    """Sprint planning reads Next up. The bundle said this summary was 15
    days old; its state was 78."""
    from cp_engine import prep_planning

    _status_only_history(tmp_path)
    config, state = _planning_inputs(tmp_path)
    block = prep_planning.build_project_block(
        state, config=config, supabase_client=None,
        today=date(2026, 9, 30), week_iso="2026-W40",
    )
    rendered = "\n".join(prep_planning._render_bundle_project_block(block))
    assert "PARTIAL REFRESH" in rendered
    assert "last changed 2026-07-14" in rendered
    assert "78d old, not 15d" in rendered


def test_planning_bundle_full_refresh_has_no_flag(tmp_path):
    from cp_engine import prep_planning

    _full_refresh_history(tmp_path)
    config, state = _planning_inputs(tmp_path)
    block = prep_planning.build_project_block(
        state, config=config, supabase_client=None,
        today=date(2026, 9, 30), week_iso="2026-W40",
    )
    rendered = "\n".join(prep_planning._render_bundle_project_block(block))
    assert "Exec Summary" in rendered and "PARTIAL REFRESH" not in rendered
