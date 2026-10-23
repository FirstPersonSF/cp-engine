"""#352 write side — `replaces_key` on the create verbs, and sources per version.

WHAT BROKE (1). Replacing an element with a new one left no trace: the
`supersedes` edge the read side (#352 item 1) shows was writable only through
`promote_to_canon(replaces_key=)` or a separate `create_spine_relation` call
nobody made. ibx-5153's talking-points v02 and branch-2 v2 went stale with
nothing pointing at them.

WHAT BROKE (2). `add_element_source` wrote the link onto EVERY version row, so
attaching an input to a deliverable whose current version had shipped claimed
the shipped version drew on it.

WHAT THESE PIN.
  * `create_spine_element` / `add_spine_document` take `replaces_key`: after
    the insert, an ACTIVE `supersedes` edge new -> old through the same
    `_insert_lifecycle_edge` `promote_to_canon` uses, returned in the result.
    A key that does not resolve is refused BEFORE the insert.
  * `add_element_source` asks the guarded function for the LIVE row only
    (`p_live_only`), and marks the link `for_next` when the live version is a
    shipped deliverable; `for_next_version` overrides. `remove_element_source`
    still strips every version. `add_spine_version` clears `for_next` on the
    sources it carries forward. Pull and seal-sweep label `for_next` links.

    python -m pytest prototypes/hosted-mcp/test_supersession_write_352.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

PID = "p-ibx"
CODE = "ibx-5153-ai-campaign"
OLD = "_authored/talking-points"


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self._f, self._insert = db, name, [], None

    def select(self, cols, *a, **k):
        assert "*" not in cols  # never SELECT *
        return self

    def eq(self, col, val):
        self._f.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r: r.get(col) in vals)
        return self

    def ilike(self, col, pattern):
        needle = pattern.strip("%").lower()
        self._f.append(lambda r: needle in str(r.get(col) or "").lower())
        return self

    def or_(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def insert(self, row):
        self._insert = row
        return self

    def execute(self):
        if self._insert is not None:
            self.db.inserts.append((self.name, self._insert))
            self.db.tables.setdefault(self.name, []).append(dict(self._insert))
            return SimpleNamespace(data=[dict(self._insert)])
        rows = self.db.tables.get(self.name, [])
        return SimpleNamespace(data=[dict(r) for r in rows if all(f(r) for f in self._f)])


class _RPC:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpcs.append((self.name, self.params))
        return SimpleNamespace(data=self.db.rpc_result.get(self.name, 1))


class _DB:
    def __init__(self):
        self.tables = {
            "spine_substance": [
                {"id": f"{CODE}/{OLD}/v1", "project_id": PID, "project_code": CODE,
                 "est_item_id": OLD, "framing": "Talking points v02",
                 "status": "live", "layer": "Synthesis", "version_label": "v1",
                 "archived": False},
            ],
            "spine_relations": [],
        }
        self.inserts: list = []
        self.rpcs: list = []
        self.rpc_result: dict = {}

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        return _RPC(self, name, params)


@pytest.fixture
def db():
    return _DB()


@pytest.fixture
def server(monkeypatch, db):
    monkeypatch.setenv("SUPABASE_URL", "http://example.invalid")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    monkeypatch.setattr(mod, "user_client", lambda: db)
    monkeypatch.setattr(mod, "caller_subject", lambda: "u-1")
    monkeypatch.setattr(mod, "caller_email", lambda: "drew@example.invalid")
    monkeypatch.setattr(mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(mod, "resolve_write_scope",
                        lambda _c, code: {"id": PID, "kind": "project",
                                          "project_code": CODE})
    monkeypatch.setattr(mod, "resolve_company_id", lambda _c, _p: None)
    monkeypatch.setattr(mod, "upsert_auto_step", lambda *a, **k: {"ok": True})
    return mod


def _fn(tool):
    return getattr(tool, "fn", tool)


def _spine_inserts(db):
    return [row for name, row in db.inserts if name == "spine_substance"]


def _edges(db):
    return [row for name, row in db.inserts if name == "spine_relations"]


# --- (1) replaces_key ------------------------------------------------------

def test_create_with_replaces_key_writes_an_active_supersedes_edge(server, db):
    out = _fn(server.create_spine_element)(
        CODE, "Talking points v03", "the reset", layer="synthesis",
        replaces_key=OLD)
    assert "error" not in out, out
    (edge,) = _edges(db)
    assert edge["kind"] == "supersedes"
    assert edge["from_item_id"] == "_authored/talking-points-v03"
    assert edge["to_item_id"] == OLD
    assert edge["status"] == "active"
    assert out["replaces"]["superseded"] == OLD
    assert out["replaces"]["supersedes_edge"]["created"] is True


def test_create_with_an_unresolvable_replaces_key_writes_nothing(server, db):
    out = _fn(server.create_spine_element)(
        CODE, "Talking points v03", "the reset", replaces_key="no-such-card")
    assert "error" in out, out
    assert _spine_inserts(db) == [] and _edges(db) == []


def test_create_without_replaces_key_writes_no_edge(server, db):
    out = _fn(server.create_spine_element)(CODE, "Fresh card", "body")
    assert "error" not in out and "replaces" not in out
    assert _edges(db) == []


def test_add_spine_document_passes_replaces_key_through(server, db):
    out = _fn(server.add_spine_document)(
        CODE, "Branch-2 assessment v3", content="new state", replaces_key=OLD)
    assert "error" not in out, out
    (edge,) = _edges(db)
    assert (edge["kind"], edge["to_item_id"]) == ("supersedes", OLD)
    assert out["replaces"]["superseded"] == OLD


def test_add_spine_document_refuses_a_bad_replaces_key_before_writing(server, db):
    out = _fn(server.add_spine_document)(
        CODE, "Branch-2 assessment v3", content="x", replaces_key="ghost")
    assert "error" in out
    assert _spine_inserts(db) == []


def test_replaces_key_is_declared_in_the_registry(server):
    tools = {t.name: t for t in server.mcp_server._tool_manager.list_tools()}
    for name in ("create_spine_element", "add_spine_document"):
        assert "replaces_key" in tools[name].parameters["properties"], name
    assert "for_next_version" in tools["add_element_source"].parameters["properties"]


# --- (2) sources per version ----------------------------------------------

def _deliverable_versions(layer="Deliverables"):
    return [
        {"id": "d/v1", "est_item_id": "_authored/deck", "status": "superseded",
         "layer": layer, "version_label": "v1", "sources": []},
        {"id": "d/v2", "est_item_id": "_authored/deck", "status": "live",
         "layer": layer, "version_label": "v2", "version_date": "2026-10-10",
         "sources": []},
    ]


def _seal(db, created_at="2026-10-12T09:00:00Z"):
    db.tables["spine_relations"].append(
        {"project_id": PID, "kind": "absorbed_by", "status": "active",
         "from_item_id": "_authored/r2-feedback", "to_item_id": "_authored/deck",
         "created_at": created_at})


def _attach(server, monkeypatch, versions, **kw):
    monkeypatch.setattr(server, "resolve_element_versions",
                        lambda _c, pid, key: ("_authored/deck", versions, None))
    monkeypatch.setattr(server, "_resolve_active_asset",
                        lambda _c, pid, t: ({"id": "a-1", "title": "R3 notes"}, None))
    return _fn(server.add_element_source)(CODE, "deck", "R3 notes", **kw)


def test_attach_asks_for_the_live_row_only(server, db, monkeypatch):
    _attach(server, monkeypatch, _deliverable_versions("Synthesis"))
    ((name, params),) = db.rpcs
    assert name == "spine_element_modify_source"
    assert params["p_live_only"] is True
    assert "for_next" not in params["p_entry"]


def test_attach_to_an_unsealed_deliverable_is_not_for_next(server, db, monkeypatch):
    """An in-progress deliverable version has not shipped — no seal, no flag."""
    out = _attach(server, monkeypatch, _deliverable_versions())
    ((_, params),) = db.rpcs
    assert "for_next" not in params["p_entry"]
    assert not out.get("for_next")


def test_a_seal_older_than_the_live_version_is_not_for_next(server, db, monkeypatch):
    _seal(db, "2026-10-01T00:00:00Z")
    _attach(server, monkeypatch, _deliverable_versions())
    assert "for_next" not in db.rpcs[-1][1]["p_entry"]


def test_attach_to_a_shipped_deliverable_is_for_next(server, db, monkeypatch):
    _seal(db)
    out = _attach(server, monkeypatch, _deliverable_versions())
    ((_, params),) = db.rpcs
    assert params["p_entry"]["for_next"] is True
    assert out.get("for_next") is True


def test_for_next_version_overrides(server, db, monkeypatch):
    _seal(db)
    _attach(server, monkeypatch, _deliverable_versions(), for_next_version=False)
    assert "for_next" not in db.rpcs[-1][1]["p_entry"]
    _attach(server, monkeypatch, _deliverable_versions("Synthesis"),
            for_next_version=True)
    assert db.rpcs[-1][1]["p_entry"]["for_next"] is True


def test_detach_still_strips_every_version(server, db, monkeypatch):
    versions = _deliverable_versions()
    for v in versions:
        v["sources"] = [{"type": "rag_asset", "id": "a-1", "title": "R3 notes"}]
    monkeypatch.setattr(server, "resolve_element_versions",
                        lambda _c, pid, key: ("_authored/deck", versions, None))
    monkeypatch.setattr(server, "_resolve_active_asset",
                        lambda _c, pid, t: ({"id": "a-1", "title": "R3 notes"}, None))
    _fn(server.remove_element_source)(CODE, "deck", "R3 notes")
    ((_, params),) = db.rpcs
    assert not params.get("p_live_only")


def test_new_version_clears_for_next_on_carried_sources(server, db, monkeypatch):
    a = {"type": "rag_asset", "id": "a", "title": "Fed v2"}
    b = {"type": "rag_asset", "id": "b", "title": "For v3", "for_next": True}
    base = {"id": f"{CODE}/_authored/deck/v2", "est_item_id": "_authored/deck",
            "project_id": PID, "project_code": CODE, "status": "live",
            "layer": "Deliverables", "version_label": "v2", "framing": "Deck",
            "sources": [a, b]}
    monkeypatch.setattr(server, "resolve_element_versions",
                        lambda _c, pid, key: ("_authored/deck", [base], None))
    monkeypatch.setattr(server, "_prior_version_preview",
                        lambda _c, b: {"version_label": "v2"})
    out = _fn(server.add_spine_version)(CODE, key="deck", body="v3 body")
    assert "error" not in out, out
    (row,) = _spine_inserts(db)
    assert row["sources"] == [a, {"type": "rag_asset", "id": "b", "title": "For v3"}]


def test_pull_labels_next_version_sources(server, db, monkeypatch):
    b = {"type": "rag_asset", "id": "b", "title": "For v3", "for_next": True}
    db.tables["spine_substance"].append(
        {"id": "d/v2", "project_id": PID, "project_code": CODE,
         "est_item_id": "_authored/deck", "framing": "Deck", "status": "live",
         "layer": "Deliverables", "version_label": "v2", "archived": False,
         "body": "deck", "version_date": "2026-10-20", "sources": [b]})
    monkeypatch.setattr(server, "resolve_project_id", lambda _c, code: PID)
    out = _fn(server.pull_spine_element)(key="_authored/deck", project_code=PID)
    assert out.get("sources_for_next_version") == [b], out


def test_seal_sweep_round_dict_separates_for_next(server):
    from datetime import date

    from cp_engine.seal_sweep import build_rounds

    a = {"type": "rag_asset", "id": "a", "title": "Fed v2"}
    b = {"type": "rag_asset", "id": "b", "title": "For v3", "for_next": True}
    rows = [{"est_item_id": "_authored/deck", "framing": "Deck",
             "layer": "Deliverables", "version_label": "v2",
             "version_date": "2026-10-20", "sources": [a, b]}]
    (rnd,) = build_rounds(rows, [], today=date(2026, 10, 22))
    out = server._round_dict(rnd)
    assert [s["id"] for s in out["sources"]] == ["a"]
    assert [s["id"] for s in out["sources_for_next_version"]] == ["b"]
