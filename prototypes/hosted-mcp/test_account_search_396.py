"""A project-scoped semantic_search reads the company's account documents (#396).

`list_project_sources`, `read_scoped_asset_chunks` and the fetch resolver all
union a workstream's own assets with its company's `scope='account'` ones.
`semantic_search` did not: `match_chunks_for_project` filters on
`project_id` alone, so a file filed under the account (or a program) node —
brand guidelines, research — was invisible to every child's search. Written
the way #225's tests are: write on the parent, read from the child.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

CHILD, ACCT = "p-child", "p-acct"


class _Q:
    def __init__(self, rows):
        self._rows, self._f = rows, []

    def select(self, cols, *a, **k):
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r: r.get(col) in vals)
        return self

    def or_(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self._rows if all(f(r) for f in self._f)])


class _DB:
    """The RPC answers per `filter_project_id`, like mig 206's scoped branch."""

    def __init__(self):
        self.tables = {
            "projects": [{"id": CHILD, "company_id": "co", "mc_status": "Open"},
                         {"id": ACCT, "company_id": "co", "mc_status": "Open"}],
            "rag_assets": [
                {"id": "a-own", "project_id": CHILD, "company_id": "co", "scope": "project",
                 "status": "active", "title": "SOW"},
                {"id": "a-brand", "project_id": ACCT, "company_id": "co", "scope": "account",
                 "status": "active", "title": "Brand guidelines"},
                {"id": "a-acct-private", "project_id": ACCT, "company_id": "co",
                 "scope": "project", "status": "active", "title": "Acct notes"},
            ],
        }
        self.chunks = {
            CHILD: [{"chunk_id": "k-own", "asset_id": "a-own", "title": "SOW", "similarity": 0.5}],
            ACCT: [{"chunk_id": "k-brand", "asset_id": "a-brand", "title": "Brand guidelines",
                    "similarity": 0.8},
                   {"chunk_id": "k-priv", "asset_id": "a-acct-private", "title": "Acct notes",
                    "similarity": 0.9}],
        }
        self.rpc_calls = []
        self.fail_assets = False

    def table(self, name):
        if name == "rag_assets" and self.fail_assets:
            raise RuntimeError("rag_assets down")
        return _Q(self.tables.get(name, []))

    def rpc(self, name, params):
        self.rpc_calls.append((name, params.get("filter_project_id")))
        if name == "match_chunks_for_project":
            return _Q(self.chunks.get(params.get("filter_project_id"), []))
        return _Q([])


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
    monkeypatch.setattr(server, "embedding_available", lambda: (True, ""))
    monkeypatch.setattr(server, "embed_query", lambda _t: [0.1])
    return db


def test_child_search_finds_the_account_document(server, db):
    out = server.semantic_search("brand colours", project_code=CHILD)
    assert "error" not in out, out
    ids = [r["chunk_id"] for r in out["results"]]
    assert ids == ["k-brand", "k-own"]  # best first across both arms
    brand = out["results"][0]
    assert brand["asset_scope"] == "account" and brand["title"] == "Brand guidelines"
    assert "asset_scope" not in out["results"][1]


def test_the_parent_nodes_own_project_scope_files_stay_private(server, db):
    out = server.semantic_search("notes", project_code=CHILD)
    assert "k-priv" not in [r["chunk_id"] for r in out["results"]]


def test_limit_holds_across_both_arms(server, db):
    out = server.semantic_search("x", project_code=CHILD, limit=1)
    assert [r["chunk_id"] for r in out["results"]] == ["k-brand"]


def test_corpus_wide_search_is_unchanged(server, db):
    server.semantic_search("x")
    assert [c for c in db.rpc_calls if c[0] == "match_chunks_for_project"] == [
        ("match_chunks_for_project", None)]


def test_a_failed_account_arm_is_said_not_hidden(server, db):
    db.fail_assets = True
    out = server.semantic_search("x", project_code=CHILD)
    assert [r["chunk_id"] for r in out["results"]] == ["k-own"]
    assert "rag_assets down" in out["account_search_error"]


def test_merge_is_pure_and_dedupes(server):
    rows = server.merge_account_hits(
        [{"chunk_id": "a", "asset_id": "x", "similarity": 0.2}],
        [{"chunk_id": "a", "asset_id": "acct", "similarity": 0.2},
         {"chunk_id": "b", "asset_id": "acct", "similarity": 0.3},
         {"chunk_id": "c", "asset_id": "other", "similarity": 0.9}],
        {"acct"}, 5)
    assert [r["chunk_id"] for r in rows] == ["b", "a"]
