"""Project-scoped reads on the stdio server say when the work is finished (#279).

WHAT BROKE. `projects.mc_status = 'Archived'` is a project lifecycle state,
independent of `spine_substance.archived`, and no MCP verb read it:
`list_spine_elements("SNT-labscon-26")` on a project archived in July returned
its live elements exactly as a project opened last week would. Measured
2026-09-16: 22 archived projects, 6 live elements on them, and nothing in any
response to tell a session it was reading finished work.

WHAT THESE PIN. The same four stdio verbs, called once against an Archived
project and once against an Open one, through a fake PostgREST whose
`projects` table is the only source of the status: the archived answer carries
the signal, the live answer is unchanged in shape, and archived DATA is still
returned (annotate, never filter — the issue's point about reversible,
still-useful finished work).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import cp_engine.mcp_server as srv
from cp_engine import project_status as ps

ARCHIVED, LIVE = "p-archived", "p-live"


class _Q:
    def __init__(self, rows, log):
        self._rows, self._log, self._f = rows, log, []

    def select(self, cols, *a, **k):
        # The never-SELECT-* rule, enforced where it can be.
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self._f.append(lambda r: r.get(col) in set(vals))
        return self

    def execute(self):
        self._log.append(1)
        return SimpleNamespace(data=[r for r in self._rows if all(f(r) for f in self._f)])


class _DB:
    def __init__(self):
        self.reads = []
        self.tables = {"projects": [
            {"id": ARCHIVED, "mc_status": "Archived"},
            {"id": LIVE, "mc_status": "Open"},
            {"id": "p-closed", "mc_status": "Closed"},
            {"id": "p-holding", "mc_status": "Holding"},
        ]}

    def table(self, name):
        return _Q(self.tables.get(name, []), self.reads)


@pytest.fixture
def db(monkeypatch):
    db = _DB()
    monkeypatch.setattr(srv, "_resolve", lambda code: (db, code, "cid"))
    monkeypatch.setattr(
        srv, "_resolve_commitments",
        lambda code: (db, {"id": code, "code": code, "kind": "project"}),
    )
    rows = [{"est_item_id": "_authored/brief", "framing": "Brief"}]
    monkeypatch.setattr("cp_engine.project_sources.list_spine",
                        lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr("cp_engine.project_sources.list_sources",
                        lambda *a, **k: [{"id": "a1", "title": "SOW"}])
    monkeypatch.setattr("cp_engine.project_sources.pull_spine",
                        lambda *a, **k: {"est_item_id": "_authored/brief", "body": "b",
                                         "layer": "Brief", "note": "own note"})
    monkeypatch.setattr("cp_engine.commitments.list_commitments",
                        lambda *a, **k: [{"id": "c1", "description": "Send deck"}])
    return db


_LIST_VERBS = {
    "list_spine_elements": lambda code: srv.list_spine_elements(code),
    "list_project_sources": lambda code: srv.list_project_sources(code),
    "list_commitments": lambda code: srv.list_commitments(code),
}


@pytest.mark.parametrize("verb", sorted(_LIST_VERBS))
def test_an_archived_projects_list_leads_with_the_signal_and_keeps_its_data(db, verb):
    out = _LIST_VERBS[verb](ARCHIVED)
    assert out[0]["project_status"] == "Archived"
    assert out[0]["archived"] is True
    assert "ARCHIVED" in out[0]["note"]
    # Annotated, not filtered: the real rows are all still there.
    assert len(out) == 2 and "note" not in out[1]


@pytest.mark.parametrize("verb", sorted(_LIST_VERBS))
def test_a_live_projects_list_is_unchanged(db, verb):
    """No new row for current work — a caller iterating rows sees the shape
    it always did."""
    out = _LIST_VERBS[verb](LIVE)
    assert len(out) == 1 and "note" not in out[0] and "project_status" not in out[0]


def test_pull_spine_element_carries_status_without_clobbering_the_elements_note(db):
    archived = srv.pull_spine_element(ARCHIVED, "brief")
    assert archived["project_status"] == "Archived" and archived["archived"] is True
    assert "ARCHIVED" in archived["project_note"]
    assert archived["note"] == "own note"      # the element's importance note survives
    assert archived["body"] == "b"             # and the data is returned

    live = srv.pull_spine_element(LIVE, "brief")
    assert live["project_status"] == "Open"
    assert "archived" not in live and "project_note" not in live


def test_one_status_read_per_call(db):
    """The ask was one lookup, not a query per step of the verb."""
    srv.pull_spine_element(ARCHIVED, "brief")
    assert len(db.reads) == 1


# ── the shared vocabulary ───────────────────────────────────────────────


@pytest.mark.parametrize("status,note", [
    ("Archived", True), ("Closed", True), ("Holding", False), ("Open", False), ("Deal", False),
])
def test_only_finished_statuses_carry_a_note(status, note):
    """Closed and Archived are finished work; Holding is paused CURRENT work
    and "weigh this as history" would be wrong advice for it."""
    fields = ps.status_fields(status, "x-1")
    assert fields["project_status"] == status
    assert ("project_note" in fields) is note
    assert fields.get("archived", False) is (status == "Archived")


def test_the_finished_set_is_a_subset_of_the_tenant_vocabulary():
    """Derived from the engine's own status list, so a renamed status cannot
    leave this module annotating a value that no longer exists."""
    from cp_engine import MC_STATUSES

    assert ps.FINISHED_STATUSES <= set(MC_STATUSES)
    assert {"Closed", "Archived"} == ps.FINISHED_STATUSES


def test_a_failed_status_read_adds_nothing():
    """Unknown must never read as "no status" — and must never fail the read."""
    out = ps.annotate_project({"body": "b"}, object(), "p1", "x")
    assert out == {"body": "b"}


def test_error_results_are_left_alone():
    assert ps.annotate({"error": "nope"}, "Archived") == {"error": "nope"}
    assert ps.annotate([{"error": "nope"}], "Archived") == [{"error": "nope"}]
