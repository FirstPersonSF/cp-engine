# tests/test_unauthored_ingest_353.py — ingest the Exec Summary never learns (#353 item 2)
"""On 2026-09-01 a full client briefing landed in sap-5198's sprint file by
meeting auto-ingest while its cp.md Exec Summary was still the scaffold, and
prep reported "not yet authored" — which reads as "no state".

Pinned here, each as a CONTROL that fails on the pre-#353 code:

1. The weekly drafter (#251) takes an unauthored summary into scope whatever
   its stamp says; a scaffold stamped this week used to read as "current".
   The draft fills the state fields and leaves Objective's placeholder alone.
2. `cxp exec-lint` says "N meetings ingested since <date>" for a scaffold
   that ingest has fed (it used to say "within budget").
3. The planning bundle's unauthored sentinel carries the same count.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from cp_engine import exec_summary_draft as esd
from cp_engine.exec_summary_freshness import ingested_meetings, unauthored_ingest
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

CODE = "sap-5198-2027-ad-videos"
_PROJ = Path("1p") / "sap" / CODE
TODAY = date(2026, 9, 2)
WEEK = "2026-W36"

SCAFFOLD = (
    "# sap-5198 2027 Ad Videos\n\n"
    f"{EXEC_SUMMARY_START}\n"
    "## Exec Summary  ·  updated 2026-08-31\n\n"
    "**Last session:** _<date>_\n"
    "**Objective:** _<one line — what this project delivers>_\n"
    "**Status:** _<current state in a phrase>_\n\n"
    "**Where it stands:**\n- _<2-4 dense bullets of current reality>_\n\n"
    "**Next up:**\n- _<concrete near-term moves, dated where possible>_\n\n"
    "**Blockers:**\n- _<what's stuck / needed, with who — or \"None\">_\n\n"
    "**Updates:**\n- _<dated — first wrap up authors this>_\n"
    f"{EXEC_SUMMARY_END}\n"
)

SPRINT = """# sap-5198 — 2027 Ad Videos · Sprint W36

## Meeting notes & decisions

### Inbound
- [2026-09-01 · Lena] Lena briefed six ad videos for the 2027 launch; scripts due 2026-09-15.
"""


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


def _commit(repo: Path, msg: str, when: str) -> None:
    (repo / "log.txt").write_text(msg + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, when=when)


def _tenant(tmp_path: Path, cp_text: str = SCAFFOLD) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    (tmp_path / _PROJ).mkdir(parents=True)
    cp_md = tmp_path / _PROJ / "cp.md"
    cp_md.write_text(cp_text, encoding="utf-8")
    sprint = tmp_path / "sprints" / WEEK / f"{CODE}.md"
    sprint.parent.mkdir(parents=True)
    sprint.write_text(SPRINT, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, f"scaffold {CODE}", "2026-08-31T10:00:00-07:00")
    # Both spellings MC-2 has carried, one meeting twice (two sprint
    # files), an account summary and a neighbouring code that must not count.
    _commit(tmp_path, "[auto-ingest] sap-5198: meeting 4e1f7c61", "2026-09-01T09:00:00-07:00")
    _commit(tmp_path, f"[auto-ingest] {CODE}: meeting 4e1f7c61", "2026-09-01T09:01:00-07:00")
    _commit(tmp_path, f"[auto-ingest] {CODE}: meeting 33efcaa2", "2026-09-02T09:00:00-07:00")
    _commit(tmp_path, "[auto-ingest] account:sap-5216-sap: meeting 99999999",
            "2026-09-02T09:05:00-07:00")
    _commit(tmp_path, "[auto-ingest] sap-51980-other: meeting aaaaaaaa",
            "2026-09-02T09:06:00-07:00")
    return cp_md


# ── the count ────────────────────────────────────────────────────────────


def test_ingest_count_reads_both_spellings_and_dedupes_meetings(tmp_path):
    cp_md = _tenant(tmp_path)
    found = ingested_meetings(cp_md.parent, CODE)
    assert found is not None
    assert (found.meetings, found.first, found.last) == (
        2, date(2026, 9, 1), date(2026, 9, 2))
    assert found.describe() == (
        "2 meetings ingested since 2026-09-01 (latest 2026-09-02)")


def test_authored_summary_is_not_flagged(tmp_path):
    authored = SCAFFOLD.replace(
        "**Status:** _<current state in a phrase>_",
        "**Status:** Scripts in progress for the six 2027 launch videos.")
    cp_md = _tenant(tmp_path, authored)
    assert unauthored_ingest(cp_md, CODE) is None


def test_no_ingest_is_silent(tmp_path):
    cp_md = _tenant(tmp_path)
    assert ingested_meetings(cp_md.parent, "ggl-5136-go-safety-website") is None


# ── 1. the drafter seeds it ──────────────────────────────────────────────


def test_drafter_scopes_a_fresh_scaffold_and_names_the_ingest(tmp_path):
    """CONTROL: pre-#353 `scope_reason` read the scaffold's 2-day-old
    creation stamp as "current" and returned None."""
    cp_md = _tenant(tmp_path)
    reason = esd.scope_reason(cp_md, today=TODAY)
    assert reason == (
        "unauthored, 2 meetings ingested since 2026-09-01 (latest 2026-09-02)")


def test_draft_of_a_scaffold_fills_state_and_never_objective(tmp_path):
    from types import SimpleNamespace

    cp_md = _tenant(tmp_path)
    draft = {
        "status": "Lena briefed six ad videos for the 2027 launch.",
        "where_it_stands": ["Lena briefed six ad videos for the 2027 launch."],
        "next_up": ["Scripts due 2026-09-15."],
        "blockers": [],
    }
    res = esd.draft_one(
        SimpleNamespace(code=CODE, name="2027 Ad Videos"),
        tenant_root=tmp_path, work_dir=cp_md.parent,
        reason=esd.scope_reason(cp_md, today=TODAY), today=TODAY,
        current_week=WEEK, llm=lambda _s, _p: json.dumps(draft), apply=True,
    )
    assert res.outcome == "written", (res.outcome, res.detail)
    after = cp_md.read_text(encoding="utf-8")
    assert "**Status:** Lena briefed six ad videos" in after
    assert "**Objective:** _<one line — what this project delivers>_" in after
    assert esd.is_drafted(after)
    # Drafted, it is authored now — and out of the unauthored scope.
    assert unauthored_ingest(cp_md, CODE) is None


# ── 2. exec-lint says so ─────────────────────────────────────────────────


def test_exec_lint_names_the_ingest_on_a_scaffold(tmp_path, monkeypatch):
    """CONTROL: pre-#353 exec-lint printed "exec-summary within budget"."""
    from cp_engine.cli import main

    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["exec-lint", "sap-5198"])
    assert result.exit_code == 0, result.output
    assert "never authored, but 2 meetings ingested since 2026-09-01" in result.output


# ── 3. the planning bundle says so ───────────────────────────────────────


def test_planning_bundle_unauthored_sentinel_carries_the_count(tmp_path):
    """CONTROL: pre-#353 the bundle said only "not yet authored"."""
    from cp_engine import prep_planning
    from cp_engine.config import SyncConfig, TenantConfig
    from cp_engine.state import ProjectState

    _tenant(tmp_path)
    config = TenantConfig(
        name="t", display="T", engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *",
                        mc_2_supabase_project_ref="ref"),
        projects=(), root=tmp_path,
    )
    state = ProjectState(
        code=CODE, name="2027 Ad Videos", has_agreement=True,
        company_kind="client", company_code="SAP", company_name="SAP",
        status="Open", owner="drew", last_touched=None, deadline=None,
    )
    block = prep_planning.build_project_block(
        state, config=config, supabase_client=None, today=TODAY, week_iso=WEEK,
    )
    assert block.exec_summary is None
    for render in (prep_planning._render_bundle_project_block,
                   prep_planning._render_project_block):
        rendered = "\n".join(render(block))
        assert "not yet authored — but 2 meetings ingested since 2026-09-01" in rendered
