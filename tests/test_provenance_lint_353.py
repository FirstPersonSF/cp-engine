# tests/test_provenance_lint_353.py — hand-kept Provenance stamps decay (#353 item 5)
"""A budget doc read `Provenance: Version 01` through six revisions (09-15).

Pinned here: a hand-kept stamp (`Version NN`, integer) older than the file's
last SUBSTANTIVE commit is reported by `cxp render --check`; machine commits,
whitespace, engine-region and stamp-only edits, the creating commit, archives
and engine-written (dotted-version) stamps are not. The CLI test is the
CONTROL: it fails on the pre-#353 render, which had no such pass.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from cp_engine.provenance_lint import (
    hand_stamp,
    stale_stamps,
    substantive_text,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

TODAY = date(2026, 10, 22)


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


def _doc(stamp: str, body: str, *, version: str = "01", region: str = "x") -> str:
    return (
        "---\n"
        "Project: sap-5198 2027 Ad Videos\n"
        f"Provenance: Version {version} | {stamp}\n"
        "Filename: budget.md\n"
        "Author: Drew\n"
        "---\n\n"
        "# Budget\n\n"
        f"{body}\n\n"
        "<!-- cp-engine:start strip -->\n"
        f"{region}\n"
        "<!-- cp-engine:end strip -->\n"
    )


def _write(repo: Path, rel: str, text: str, msg: str, when: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, when=when)


def _tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    return tmp_path


BUDGET = "1p/sap/sap-5198/budget.md"


def _decayed(tmp_path: Path) -> Path:
    """Created 09-01 at Version 01; revised by hand 09-10 without a bump."""
    root = _tenant(tmp_path)
    _write(root, BUDGET, _doc("2026-09-01", "Total: $400k"),
           "budget v01", "2026-09-03T10:00:00-07:00")  # creation, later commit
    _write(root, BUDGET, _doc("2026-09-01", "Total: $425k"),
           "budget: client added a cutdown", "2026-09-10T10:00:00-07:00")
    return root


# ── units ────────────────────────────────────────────────────────────────


def test_hand_stamp_reads_integer_versions_only():
    assert hand_stamp(_doc("2026-09-01", "b")) == ("01", date(2026, 9, 1))
    assert hand_stamp(_doc("2026-09-01", "b", version="0.131.11")) is None
    assert hand_stamp("# no anchor\n") is None


def test_substantive_text_ignores_regions_comments_stamp_and_whitespace():
    a = _doc("2026-09-01", "Total:  $400k", region="one")
    b = _doc("2026-09-30", "Total: $400k\n", region="two") + "<!-- cp:hash=ab -->\n"
    assert substantive_text(a) == substantive_text(b)
    assert substantive_text(a) != substantive_text(_doc("2026-09-01", "Total: $425k"))


# ── the finding ──────────────────────────────────────────────────────────


def test_hand_revision_after_the_stamp_is_flagged(tmp_path):
    root = _decayed(tmp_path)
    found = stale_stamps(root, today=TODAY)
    assert [(f.path, f.stamp, f.revised, f.revisions) for f in found] == [
        (BUDGET, date(2026, 9, 1), date(2026, 9, 10), 1)]
    assert "Bump the version and date" in found[0].describe()


def test_creating_commit_after_the_stamp_is_not_a_revision(tmp_path):
    root = _tenant(tmp_path)
    _write(root, BUDGET, _doc("2026-09-01", "Total: $400k"),
           "budget v01", "2026-09-03T10:00:00-07:00")
    assert stale_stamps(root, today=TODAY) == []


@pytest.mark.parametrize("msg,text", [
    ("[auto-ingest] sap-5198: meeting 4e1f7c61", _doc("2026-09-01", "Total: $999k")),
    ("[cp-sync] 2026-09-10T14:02Z — auto-sync", _doc("2026-09-01", "Total: $999k")),
    ("rewrap", _doc("2026-09-01", "Total:\n$400k")),
    ("region only", _doc("2026-09-01", "Total: $400k", region="changed")),
])
def test_machine_whitespace_and_region_edits_are_not_revisions(tmp_path, msg, text):
    root = _tenant(tmp_path)
    _write(root, BUDGET, _doc("2026-09-01", "Total: $400k"), "v01",
           "2026-09-01T10:00:00-07:00")
    _write(root, BUDGET, text, msg, "2026-09-10T10:00:00-07:00")
    assert stale_stamps(root, today=TODAY) == []


def test_a_bumped_stamp_clears_it(tmp_path):
    root = _decayed(tmp_path)
    _write(root, BUDGET, _doc("2026-09-12", "Total: $425k", version="02"),
           "budget v02", "2026-09-12T10:00:00-07:00")
    assert stale_stamps(root, today=TODAY) == []


def test_engine_stamps_and_archives_are_out_of_scope(tmp_path):
    root = _tenant(tmp_path)
    for rel, ver in (("1p/x/cp.md", "0.131.11"), ("1p/x/archive/old.md", "01")):
        _write(root, rel, _doc("2026-09-01", "a", version=ver), "v",
               "2026-09-01T10:00:00-07:00")
        _write(root, rel, _doc("2026-09-01", "b", version=ver), "edit",
               "2026-09-10T10:00:00-07:00")
    assert stale_stamps(root, today=TODAY) == []


def test_uncommitted_substantive_edit_counts_as_today(tmp_path):
    root = _tenant(tmp_path)
    _write(root, BUDGET, _doc("2026-09-01", "Total: $400k"), "v01",
           "2026-09-01T10:00:00-07:00")
    (root / BUDGET).write_text(_doc("2026-09-01", "Total: $450k"), encoding="utf-8")
    found = stale_stamps(root, today=TODAY)
    assert len(found) == 1 and found[0].revised == TODAY and found[0].commit is None
    assert "uncommitted edit" in found[0].describe()


def test_shallow_or_no_git_is_silent(tmp_path):
    (tmp_path / "doc.md").write_text(_doc("2026-09-01", "a"), encoding="utf-8")
    assert stale_stamps(tmp_path, today=TODAY) == []


# ── the surface (CONTROL) ────────────────────────────────────────────────


def test_render_check_reports_the_decayed_stamp(tmp_path, monkeypatch):
    """CONTROL: fails on pre-#353 code — `render --check` had no such pass."""
    from cp_engine.cli import main

    root = _decayed(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["render", "--check"])
    assert result.exit_code == 0, result.output
    assert (f"{BUDGET}: ⚠ Provenance says Version 01 | 2026-09-01, but the file "
            "has 1 substantive revision since — latest 2026-09-10") in result.output
