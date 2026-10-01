"""Architecture plan step 3 — fail loudly (group B: tree / render / planning).

Each test pins one swallowed failure that now reaches whoever gets the result:
a field on the returned object, a line in the rendered doc, a WARNING record
(which `cxp sync`'s `_WarningCounter` counts — `info`/`debug` it does not), or
a raise where the swallow used to report success. Every test here FAILS
against the pre-step-3 code; see the module's control notes in the PR.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import cp_engine
from cp_engine import ProjectState, SyncConfig, TenantConfig


# ──────────────────────────────────────────────────────────────────────
#  fixtures
# ──────────────────────────────────────────────────────────────────────


def _config(root: Path) -> TenantConfig:
    return TenantConfig(
        name="t", display="T", engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *",
                        mc_2_supabase_project_ref="ref"),
        projects=(), root=root,
    )


def _state(code: str = "ggl-5168-activation") -> ProjectState:
    return ProjectState(
        code=code, name=code, has_agreement=True, company_kind="client",
        company_code="GGL", company_name="Google", status="Open",
        is_internal=False, owner="drew",
        last_touched=datetime(2026, 6, 1, tzinfo=timezone.utc),
        deadline=None, one_line_summary=None,
    )


class _Boom:
    """A Supabase-shaped client whose every read raises."""

    def table(self, *_a, **_k):
        raise RuntimeError("db down")

    def rpc(self, *_a, **_k):
        raise RuntimeError("db down")


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ──────────────────────────────────────────────────────────────────────
#  prep_planning — per-project read failures reach result.errors + the doc
# ──────────────────────────────────────────────────────────────────────


def test_planning_failed_mc2_reads_land_in_result_errors(tmp_path):
    from cp_engine.prep_planning import build_planning_result

    result = build_planning_result(
        _config(tmp_path), (_state(),), today=date(2026, 9, 30),
        supabase_client=_Boom(),
    )
    joined = "\n".join(result.errors)
    assert "ggl-5168-activation" in joined
    assert "commitments fetch failed" in joined
    assert result.milestone_counts["errored"] == 1
    assert result.milestone_counts["fetched"] == 0


def test_planning_without_mc2_client_says_so(tmp_path):
    from cp_engine.prep_planning import build_planning_result

    result = build_planning_result(
        _config(tmp_path), (_state(),), today=date(2026, 9, 30),
        supabase_client=None,
    )
    assert any("MC-2 client unavailable" in e for e in result.errors)


def test_planning_bundle_renders_read_failures(tmp_path, capsys):
    from cp_engine.prep_planning import render_planning_bundle_doc

    out = render_planning_bundle_doc(
        _config(tmp_path), (_state(),), today=date(2026, 9, 30),
        supabase_client=_Boom(),
    )
    assert "Read failures" in out
    assert "commitments fetch failed" in out
    # ...and on stderr, which is what reaches the person running the CLI.
    assert "prep-planning read failures" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────────────
#  exec_summary_draft — never draft from a failed commitments read
# ──────────────────────────────────────────────────────────────────────


def test_draft_summaries_refuses_to_draft_when_commitments_read_fails(
    tmp_path, monkeypatch
):
    from cp_engine import commitments, exec_summary_draft as esd, state

    work = tmp_path / "ggl" / "ggl-5168-activation"
    work.mkdir(parents=True)
    (work / "cp.md").write_text("# cp\n", encoding="utf-8")
    monkeypatch.setattr(state, "resolve_project_dir", lambda *_a, **_k: work)
    monkeypatch.setattr(esd, "scope_reason", lambda *_a, **_k: "stale 20d")

    def _raise(*_a, **_k):
        raise RuntimeError("commitments store down")

    monkeypatch.setattr(commitments, "resolve_commitment_owner", _raise)
    calls: list = []

    def llm(system, prompt):
        calls.append(prompt)
        return "{}"

    results = esd.draft_summaries(
        _config(tmp_path), (_state(),), today=date(2026, 9, 30),
        current_week="2026-W40", llm=llm, supabase_client=object(), apply=True,
    )
    assert len(results) == 1
    assert results[0].outcome == "error"
    assert "commitments read failed" in results[0].detail
    assert calls == []  # no model call on partial sources


# ──────────────────────────────────────────────────────────────────────
#  sync — swallowed reads now log at WARNING (counted by _WarningCounter)
# ──────────────────────────────────────────────────────────────────────


def test_sync_deliverable_card_failure_is_a_counted_warning(
    tmp_path, monkeypatch, caplog
):
    from cp_engine import mc2_db, prep_planning, sync

    monkeypatch.setattr(mc2_db, "get_client", lambda *_a, **_k: object())

    def _raise(*_a, **_k):
        raise RuntimeError("cards down")

    monkeypatch.setattr(prep_planning, "_fetch_deliverable_lines", _raise)
    cfg = _config(tmp_path)
    caplog.set_level(logging.DEBUG, logger="cp_engine")
    sync._collect_sprint_per_project_data(
        cfg, (_state(),), None, sprint_start=date(2026, 9, 28)
    )
    assert any("deliverable-cards fetch failed" in m for m in _warnings(caplog))


def test_sync_git_log_failure_on_existing_clone_is_a_warning(
    tmp_path, monkeypatch, caplog
):
    from cp_engine import sync

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    def _fail(*a, **k):
        raise subprocess.CalledProcessError(128, a[0], stderr="corrupt")

    monkeypatch.setattr(sync.subprocess, "run", _fail)
    caplog.set_level(logging.DEBUG, logger="cp_engine")
    assert sync._recent_commits_for_repo(repo, sprint_start=date(2026, 9, 28)) == ()
    assert any("git log failed" in m for m in _warnings(caplog))


def test_sync_envelope_flag_read_failure_is_a_warning(monkeypatch, caplog):
    from cp_engine import mc2_db, sync

    class _Backend:
        def spine_client(self):
            return object()

    def _raise(_client):
        raise RuntimeError("flags table gone")

    monkeypatch.setattr(mc2_db, "fetch_open_workstream_flags", _raise)
    caplog.set_level(logging.DEBUG, logger="cp_engine")
    assert sync._read_envelope_flags(_Backend()) is None
    assert any("envelope flags unavailable" in m for m in _warnings(caplog))


# ──────────────────────────────────────────────────────────────────────
#  sprints — MC-2 resolution failure names its cause
# ──────────────────────────────────────────────────────────────────────


def test_sprint_stem_mc2_failure_is_a_warning(monkeypatch, caplog):
    from cp_engine import mc2_db, sprints

    def _raise(*_a, **_k):
        raise RuntimeError("identity read failed")

    monkeypatch.setattr(mc2_db, "project_sprint_identity", _raise)
    caplog.set_level(logging.DEBUG, logger="cp_engine")
    assert sprints._stem_from_mc2(object(), "slt-5196") is None
    assert sprints._project_state_from_mc2(object(), "slt-5196") is None
    msgs = _warnings(caplog)
    assert any("sprint-stem resolution failed" in m for m in msgs)
    assert any("first-ever sprint scaffold failed" in m for m in msgs)


# ──────────────────────────────────────────────────────────────────────
#  brief — canon bodies unreadable is a partial read, and says so
# ──────────────────────────────────────────────────────────────────────


class _CanonClient:
    """Edges read fine; the substance (titles + bodies) read raises."""

    def table(self, name):
        from cp_engine.mc2_db import Tables

        if name == Tables.SPINE_SUBSTANCE:
            raise RuntimeError("substance read failed")
        return _Q([{"from_item_id": "_authored/positioning", "to_item_id": "b"}])


class _Q:
    def __init__(self, data):
        self.data = data

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return self


def test_canon_members_partial_read_carries_a_note():
    from cp_engine.brief import canon_section, fetch_canon_members

    members, note = fetch_canon_members(_CanonClient(), "ibx-5153")
    assert [m["est_item_id"] for m in members] == ["_authored/positioning"]
    assert note and "unreadable" in note
    rendered = canon_section(members, note)
    assert "_authored/positioning" in rendered
    assert "gists missing" in rendered


# ──────────────────────────────────────────────────────────────────────
#  agenda — an unreadable sprint file is named, not rendered as empty
# ──────────────────────────────────────────────────────────────────────


def test_agenda_names_unreadable_sprint_files(tmp_path, monkeypatch):
    from cp_engine import agenda
    from cp_engine.sprints import current_sprint_week_iso

    today = date(2026, 9, 30)
    week = current_sprint_week_iso(agenda.to_datetime(today))
    wk = tmp_path / "sprints" / week
    wk.mkdir(parents=True)
    (wk / "ggl-5168-activation.md").write_text("x", encoding="utf-8")

    def _raise(_p):
        raise ValueError("malformed frontmatter")

    monkeypatch.setattr(agenda, "parse_sprint_file", _raise)
    md = agenda.build_agenda(_config(tmp_path), (_state(),), today=today)
    assert "Unreadable sprint files (1)" in md
    assert "ggl-5168-activation.md" in md
    summary = agenda.build_agenda_summary(_config(tmp_path), (_state(),), today=today)
    assert summary.to_dict()["unreadable_sprint_files"]


# ──────────────────────────────────────────────────────────────────────
#  close_out — unparseable spine files are counted for the checklist
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
#  merge_check — a failed read of the ref must not report "no content lost"
# ──────────────────────────────────────────────────────────────────────


def test_merge_check_raises_when_ref_file_cannot_be_read(tmp_path, monkeypatch):
    from cp_engine import merge_check

    real_run = subprocess.run

    def _run(args, *a, **k):
        if args[:2] == ["git", "ls-tree"]:
            return SimpleNamespace(returncode=0, stdout="sprints/W40/x.md\n", stderr="")
        if args[:2] == ["git", "show"]:
            return SimpleNamespace(returncode=128, stdout="", stderr="bad object")
        return real_run(args, *a, **k)

    monkeypatch.setattr(merge_check.subprocess, "run", _run)
    with pytest.raises(merge_check.MergeCheckError):
        merge_check.check_merge(tmp_path, ref="origin/main")


# ──────────────────────────────────────────────────────────────────────
#  priors — a failed priors read is not a debug line
# ──────────────────────────────────────────────────────────────────────


def test_priors_read_failure_is_a_warning(monkeypatch, caplog):
    from cp_engine import mc2_db, priors

    priors.clear_cache()
    monkeypatch.setattr(mc2_db, "get_client", lambda *_a, **_k: _Boom())
    caplog.set_level(logging.DEBUG, logger="cp_engine")
    assert priors.resolve_priors(None, config=None) == ""
    assert any("priors unavailable" in m for m in _warnings(caplog))
    priors.clear_cache()
