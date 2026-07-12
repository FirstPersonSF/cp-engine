# tests/test_prep_drift.py — estimate-drift warnings in prep (#65)
from datetime import date, datetime, timezone

import cp_engine.prep_planning as pp
from cp_engine.prep_planning import (
    ProjectPlanningBlock,
    _fetch_drift_warnings,
    _render_project_block,
)
from cp_engine.state import ProjectState


def _state(code="ggl-5168", source="engagement"):
    return ProjectState(
        code=code, name=code, source=source, company_kind="client",
        company_code="GGL", company_name="Google", status="Open",
        is_internal=False, owner="drew",
        last_touched=datetime(2026, 6, 1, tzinfo=timezone.utc),
        deadline=None, one_line_summary=None,
    )


def _block(drift=()):
    return ProjectPlanningBlock(
        project=_state(), exec_summary=None, milestones=(), client_asks=(),
        sprint_open_asks=(), urgent=(), fetch_error=None, drift=drift,
    )


def test_fetch_skips_without_client_and_for_initiatives():
    assert _fetch_drift_warnings(None, _state(), date(2026, 7, 11)) == ()
    assert _fetch_drift_warnings(object(), _state("mission-control"),
                                 date(2026, 7, 11)) == ()


def test_fetch_fail_soft_on_broken_client():
    class _Boom:
        def table(self, name):
            raise RuntimeError("db down")
    assert _fetch_drift_warnings(_Boom(), _state(), date(2026, 7, 11)) == ()


def test_fetch_composes_warnings(monkeypatch):
    class _T:
        def select(self, c): return self
        def eq(self, c, v): return self
        def execute(self):
            return type("R", (), {"data": [{"id": "mcpid"}]})()
    class _C:
        def table(self, n): return _T()

    est = object()
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate",
                        lambda c, pid: est)
    monkeypatch.setattr("cp_engine.estimate.fetch_schedule",
                        lambda c, eid: [], raising=True)
    monkeypatch.setattr(
        "cp_engine.project_sources.list_project_meetings",
        lambda c, pid: [])
    monkeypatch.setattr(
        "cp_engine.agreement_projection.drift_warnings",
        lambda e, b, m, today=None: ["⚠ x — past due ~2026-06-22, no done-mark"])
    # fetch_schedule reads est.id
    est_id_holder = type("E", (), {"id": "e1"})()
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate",
                        lambda c, pid: est_id_holder)
    out = _fetch_drift_warnings(_C(), _state(), date(2026, 7, 11))
    assert out == ("⚠ x — past due ~2026-06-22, no done-mark",)


def test_block_renders_drift_section():
    out = "\n".join(_render_project_block(
        _block(drift=("⚠ Workshop — linked meeting 2026-07-30 "
                      "vs estimate ~2026-07-20 (+10d)",))))
    assert "**⚠ Estimate drift:**" in out
    assert "- ⚠ Workshop — linked meeting 2026-07-30" in out


def test_block_without_drift_has_no_section():
    assert "Estimate drift" not in "\n".join(_render_project_block(_block()))
