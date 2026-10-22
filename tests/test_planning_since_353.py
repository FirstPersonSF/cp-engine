# tests/test_planning_since_353.py — the planning doc goes stale within a day (#353 item 3)
"""On 2026-09-01 three meetings changed six projects the day after
`_planning.md` was synthesized, and nothing said so.

Pinned here: `cxp prep-planning --since-synthesis` lists the workstreams whose
sprint file (hand-written text), Exec Summary or MC-2 commitments changed
after the planning doc's last commit — and NOT the ones only sync touched.
The CLI test is the CONTROL: pre-#353 there was no such flag.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cp_engine.planning_since import changes_since_synthesis, find_planning_doc
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

WEEK = "2026-W36"
SAP = "sap-5198-2027-ad-videos"
GGL = "ggl-5188-calendar-maintenance"
TEL = "tel-5113-2025-collateral"


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


def _write(root: Path, files: dict[str, str], msg: str, when: str) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg, when=when)


def _sprint(code: str, inbound: str, facts: str = "Stage: Won") -> str:
    return (
        "---\n"
        f"Project: {code}\n"
        "Provenance: Version 0.131.11 | 2026-09-01\n"
        "---\n\n"
        f"# {code} · Sprint W36\n\n"
        "<!-- cp-engine:start sprint-facts -->\n"
        f"{facts}\n"
        "<!-- cp-engine:end sprint-facts -->\n\n"
        "## Client communication\n\n"
        "### Inbound\n"
        f"{inbound}\n"
    )


def _cp(status: str, strip: str = "- none") -> str:
    return (
        "# cp\n\n"
        f"{EXEC_SUMMARY_START}\n"
        "## Exec Summary  ·  updated 2026-08-31\n\n"
        f"**Status:** {status}\n"
        f"{EXEC_SUMMARY_END}\n\n"
        "<!-- cp-engine:start stakeholders-strip -->\n"
        f"{strip}\n"
        "<!-- cp-engine:end stakeholders-strip -->\n"
    )


def _tenant(tmp_path: Path) -> Path:
    root = tmp_path
    (root / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _write(root, {
        f"sprints/{WEEK}/{SAP}.md": _sprint(SAP, "- [2026-08-31 · Lena] kickoff."),
        f"sprints/{WEEK}/{GGL}.md": _sprint(GGL, "- [2026-08-31 · Tony] pot."),
        f"1p/google/{GGL}/cp.md": _cp("Maintenance only."),
        f"1p/teleflex/{TEL}/cp.md": _cp("Collateral round two."),
    }, "state before planning", "2026-08-31T12:00:00-07:00")
    _write(root, {f"sprints/{WEEK}/_planning.md": "# W36 planning\n"},
           "W36 planning doc", "2026-09-01T09:00:00-07:00")
    # After synthesis:
    _write(root, {f"sprints/{WEEK}/{SAP}.md": _sprint(
        SAP, "- [2026-08-31 · Lena] kickoff.\n- [2026-09-02 · Lena] full brief: six videos.")},
        "[auto-ingest] sap-5198: meeting 4e1f7c61", "2026-09-02T09:00:00-07:00")
    _write(root, {f"sprints/{WEEK}/{GGL}.md": _sprint(
        GGL, "- [2026-08-31 · Tony] pot.", facts="Stage: Won · 2 asks")},
        "[cp-sync] 2026-09-02T14:02Z — auto-sync from MC-2", "2026-09-02T10:00:00-07:00")
    _write(root, {f"1p/google/{GGL}/cp.md": _cp("Pot burns down on Triptych asks.")},
           "[project-state] ggl-5188: Exec Summary", "2026-09-02T11:00:00-07:00")
    _write(root, {f"1p/teleflex/{TEL}/cp.md": _cp("Collateral round two.", "- Jo")},
           "[cp-sync] strips", "2026-09-02T12:00:00-07:00")
    # A new week scaffolded by sync: nothing written in it yet.
    _write(root, {f"sprints/2026-W37/{TEL}.md": _sprint(TEL, "")},
           "[cp-sync] W37 scaffold", "2026-09-02T13:00:00-07:00")
    return root


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def select(self, cols):
        assert "*" not in cols
        return self

    def or_(self, _f):
        return self

    def in_(self, _c, _v):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _Client:
    def table(self, name):
        if name == "commitments":
            return _Q([{"project_id": "p-tel", "created_at": "2026-09-02T00:00:00Z",
                        "updated_at": None}])
        return _Q([{"id": "p-tel", "number": 5113}])


def test_planning_doc_falls_back_to_the_newest_earlier_week(tmp_path):
    root = _tenant(tmp_path)
    assert find_planning_doc(root, "2026-W38") == root / "sprints" / WEEK / "_planning.md"
    assert find_planning_doc(root, "2026-W35") is None


def test_only_real_changes_are_listed(tmp_path):
    root = _tenant(tmp_path)
    report = changes_since_synthesis(root, WEEK, supabase_client=_Client(),
                                     projects=(SimpleNamespace(code=TEL),))
    got = {c.code: c for c in report.changes}
    assert set(got) == {SAP, GGL, TEL}
    assert got[SAP].sprint_files == [f"sprints/{WEEK}/{SAP}.md"]
    assert not got[SAP].exec_summary
    assert "[auto-ingest] sap-5198: meeting 4e1f7c61" in got[SAP].subjects
    # GGL: the Exec Summary moved; its sprint file only got a sync re-render.
    assert got[GGL].exec_summary and got[GGL].sprint_files == []
    # TEL: only MC-2 says anything — a strip and an empty scaffold do not.
    assert got[TEL].commitments == 1
    assert got[TEL].sprint_files == [] and not got[TEL].exec_summary
    assert report.basis.startswith("commit ")
    # Without a roster, an MC-2-only change still shows, by job number.
    bare = changes_since_synthesis(root, WEEK, supabase_client=_Client())
    assert "job 5113" in {c.code for c in bare.changes}


def test_projects_filter_scopes_the_report(tmp_path):
    root = _tenant(tmp_path)
    report = changes_since_synthesis(root, WEEK, codes=("sap-5198",))
    assert [c.code for c in report.changes] == [SAP]
    assert "MC-2 not reachable" in report.commitments_note


def test_cli_since_synthesis(tmp_path, monkeypatch):
    """CONTROL: pre-#353 `prep-planning` had no `--since-synthesis`."""
    from cp_engine import prep_planning, sync
    from cp_engine.cli import main

    root = _tenant(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(prep_planning, "_make_supabase_client", lambda _c: None)

    class _Backend:
        def read_projects(self, _config):
            return ()

    monkeypatch.setattr(sync, "_default_backend_factory", lambda _b: _Backend())

    result = CliRunner().invoke(main, ["prep-planning", "--since-synthesis"])
    assert result.exit_code == 0, result.output
    assert f"Planning doc: `sprints/{WEEK}/_planning.md`" in result.output
    assert f"- **{SAP}** — sprint file ({WEEK})" in result.output
    assert f"- **{GGL}** — Exec Summary" in result.output

    result = CliRunner().invoke(
        main, ["prep-planning", "--since-synthesis", "--summary"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {c["code"] for c in data["changed"]} == {SAP, GGL}
