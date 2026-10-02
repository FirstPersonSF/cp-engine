"""Hosted `pull_spine_element` composes the live Agreement (SOW) block.

WHAT BROKE. The retired stdio pull appended the estimator's phases /
deliverables / dates to every Agreement-layer element (`derived_block`), and
named any failed read in `warnings`. Deleting the stdio server (bde6ad5) took
the behaviour and its two tests with it; the hosted pull returned the stored
human terms only — which read exactly like the composed agreement.

WHAT THESE PIN. The happy path (block appended, `derived_block: true`), an
estimator failure (body intact, `derived_block: false`, a warning), and a
meetings failure (block still rendered, a warning) — the two deleted tests,
re-pointed at the hosted tool.

    python -m pytest prototypes/hosted-mcp/test_agreement_pull.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase  # noqa: E402

PID = "p-sow"


class _Q:
    def __init__(self, rows):
        self._rows, self._f = rows, []

    def select(self, cols, *a, **k):
        assert "*" not in cols  # never SELECT *
        return self

    def eq(self, col, val):
        self._f.append(lambda r: r.get(col) == val)
        return self

    def or_(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r: r.get(col) in vals)
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self._rows if all(f(r) for f in self._f)])


class _DB:
    def __init__(self):
        self.tables = {
            "projects": [{"id": PID, "mc_status": "Open", "company_id": None}],
            "spine_substance": [
                {"id": "s1", "project_id": PID, "est_item_id": "_authored/sow",
                 "layer": "Agreement", "status": "live", "body": "human terms",
                 "sources": ["s1"], "project_code": "ggl-5168",
                 "version_date": "2026-09-01"},
            ],
        }

    def table(self, name):
        return _Q(self.tables.get(name, []))


def _estimate():
    p1 = EstimatePhase(id="ph1", name="Discovery & Alignment", overview=None, position=0, items=(
        EstimateItem(id="d1", phase_id="ph1", kind="deliverable",
                     name="Perspectives & Possibilities Report",
                     short_description=None, position=0, library_item_id=None),
    ))
    return Estimate(id="est1", mc_project_id=PID, name="E", phases=(p1,),
                    start_date="2026-06-15")


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def db(server, monkeypatch):
    db = _DB()
    monkeypatch.setattr(server, "user_client", lambda: db)
    monkeypatch.setattr(server, "caller_subject", lambda: "u")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "resolve_project_id", lambda _c, code: code)
    return db


def _pull(server):
    return server.pull_spine_element(key="_authored/sow", project_code=PID)


def test_agreement_pull_appends_the_live_engagement_block(server, db, monkeypatch):
    seen = {}

    def fetch(c, pid):
        seen["client"], seen["pid"] = c, pid
        return _estimate()

    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", fetch)
    monkeypatch.setattr("cp_engine.estimate.fetch_schedule", lambda c, ids: [])
    monkeypatch.setattr("cp_engine.project_sources.list_project_meetings", lambda c, pid: [])
    res = _pull(server)
    assert res["derived_block"] is True, res
    assert res["body"].startswith("human terms\n\n")
    assert "Discovery & Alignment" in res["body"]
    assert "Perspectives & Possibilities Report" in res["body"]
    assert "Kickoff: 2026-06-15" in res["body"]
    assert "warnings" not in res
    # The caller's RLS client, on the element's home project.
    assert seen == {"client": db, "pid": PID}


def test_agreement_projection_failure_is_flagged(server, db, monkeypatch):
    def boom(c, pid):
        raise RuntimeError("estimator 500")

    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", boom)
    res = _pull(server)
    assert res["body"] == "human terms"
    assert res["derived_block"] is False
    assert any("estimator 500" in w for w in res["warnings"]), res


def test_agreement_meetings_failure_is_flagged(server, db, monkeypatch):
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: _estimate())
    monkeypatch.setattr("cp_engine.estimate.fetch_schedule", lambda c, ids: [])

    def boom(c, pid):
        raise RuntimeError("meetings 503")

    monkeypatch.setattr("cp_engine.project_sources.list_project_meetings", boom)
    res = _pull(server)
    assert res["derived_block"] is True
    assert "Discovery & Alignment" in res["body"]
    assert any("meetings 503" in w for w in res["warnings"]), res


def test_a_non_agreement_element_is_untouched(server, db, monkeypatch):
    db.tables["spine_substance"][0]["layer"] = "Brief"

    def never(c, pid):
        raise AssertionError("the estimator is read only for Agreement elements")

    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", never)
    res = _pull(server)
    assert res["body"] == "human terms" and "derived_block" not in res
