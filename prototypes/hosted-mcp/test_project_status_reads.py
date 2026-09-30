"""Hosted project-scoped reads say when the work is finished (#279).

WHAT BROKE. Archiving a project in MC-2 sets `projects.mc_status =
'Archived'` — a project lifecycle state, independent of any element's
`archived` flag — and no hosted verb read it. `list_spine_elements`,
`pull_spine_element`, `semantic_search`, `list_project_sources`,
`get_project_state` and `list_commitments` answered identically for live and
retired work. Search is the sharp end: a polished brief from a project
archived in July outranks last week's draft on presentation alone, and nothing
in the response said which was which.

WHAT THESE PIN. Each of the six verbs, against an Archived project and a live
one, through a fake PostgREST where `projects.mc_status` is the ONLY place the
status lives — so a pass means the verb read it, not that a fixture echoed it.
Archived data must still come back (annotate, never filter: finished work is
the best evidence of how we work, and archived is reversible).

    python -m pytest prototypes/hosted-mcp/test_project_status_reads.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

ARCHIVED, LIVE, CLOSED = "p-archived", "p-live", "p-closed"


class _Q:
    def __init__(self, rows):
        self._rows, self._f = rows, []

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

    def or_(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self._rows if all(f(r) for f in self._f)])


class _DB:
    def __init__(self, rpcs=None):
        self.tables = {
            "projects": [
                {"id": ARCHIVED, "mc_status": "Archived", "company_id": None},
                {"id": LIVE, "mc_status": "Open", "company_id": None},
                {"id": CLOSED, "mc_status": "Closed", "company_id": None},
            ],
            "spine_substance": [
                {"id": "s-arch", "project_id": ARCHIVED, "est_item_id": "_authored/brief",
                 "status": "live", "body": "old brief", "project_code": "snt-labscon-26",
                 "version_date": "2026-07-01"},
                {"id": "s-live", "project_id": LIVE, "est_item_id": "_authored/brief",
                 "status": "live", "body": "new brief", "project_code": "ibx-5153",
                 "version_date": "2026-09-01"},
            ],
            "rag_assets": [
                {"id": "a-arch", "project_id": ARCHIVED, "title": "Old SOW", "status": "active"},
                {"id": "a-live", "project_id": LIVE, "title": "New SOW", "status": "active"},
            ],
            "commitments": [
                {"id": "c-arch", "project_id": ARCHIVED, "status": "open"},
                {"id": "c-live", "project_id": LIVE, "status": "open"},
            ],
            "spine_relations": [],
        }
        self.rpcs = rpcs or {}

    def table(self, name):
        return _Q(self.tables.get(name, []))

    def rpc(self, name, _params):
        return _Q(self.rpcs.get(name, []))


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
    # Codes ARE ids here, so resolution is not what is under test.
    monkeypatch.setattr(server, "resolve_project_id", lambda _c, code: code)
    monkeypatch.setattr(
        server, "read_spine_rows",
        lambda c, pid, cols: [dict(r, framing="Brief", layer="Brief")
                              for r in db.tables["spine_substance"] if r["project_id"] == pid],
    )
    return db


_VERBS = {
    "list_spine_elements": lambda s, pid: s.list_spine_elements(pid),
    "list_project_sources": lambda s, pid: s.list_project_sources(pid),
    "list_commitments": lambda s, pid: s.list_commitments(pid),
    "pull_spine_element": lambda s, pid: s.pull_spine_element(key="_authored/brief", project_code=pid),
}
_PAYLOAD = {
    "list_spine_elements": "elements", "list_project_sources": "sources",
    "list_commitments": "commitments", "pull_spine_element": "body",
}


@pytest.mark.parametrize("verb", sorted(_VERBS))
def test_an_archived_project_says_so_and_still_returns_its_data(server, db, verb):
    out = _VERBS[verb](server, ARCHIVED)
    assert "error" not in out, out
    assert out["project_status"] == "Archived"
    assert out["archived"] is True
    assert "ARCHIVED" in out["project_note"]
    assert out[_PAYLOAD[verb]], "archived data must be annotated, never filtered"


@pytest.mark.parametrize("verb", sorted(_VERBS))
def test_a_live_project_carries_its_status_and_no_warning(server, db, verb):
    out = _VERBS[verb](server, LIVE)
    assert out["project_status"] == "Open"
    assert "archived" not in out and "project_note" not in out


def test_closed_is_finished_work_too(server, db):
    out = server.list_commitments(CLOSED)
    assert out["project_status"] == "Closed"
    assert "CLOSED" in out["project_note"] and "archived" not in out


def test_pull_reads_the_elements_HOME_project_not_the_optional_scope(server, db):
    """Unscoped pull: the only project in play is the row's own."""
    db.tables["spine_substance"] = [db.tables["spine_substance"][0]]
    out = server.pull_spine_element(key="_authored/brief")
    assert out["project_status"] == "Archived"


def test_semantic_search_marks_each_hit_from_finished_work(server, db, monkeypatch):
    """Corpus-wide search is where a finished project's polished brief
    competes with the live one. Each archived hit is marked; the live hit is
    left exactly as it was; nothing is dropped."""
    db.rpcs = {
        "match_spine_context": [
            {"id": "s-arch", "framing": "Old brief", "project_code": "snt-labscon-26"},
            {"id": "s-live", "framing": "New brief", "project_code": "ibx-5153"},
        ],
        "match_chunks_for_project": [
            {"chunk_id": "k1", "asset_id": "a-arch", "title": "Old SOW", "similarity": 0.9},
            {"chunk_id": "k2", "asset_id": "a-live", "title": "New SOW", "similarity": 0.5},
        ],
    }
    monkeypatch.setattr(server, "embedding_available", lambda: (True, ""))
    monkeypatch.setattr(server, "embed_query", lambda _t: [0.1])

    out = server.semantic_search("brief")
    assert "error" not in out, out
    by_chunk = {r["chunk_id"]: r for r in out["results"]}
    assert by_chunk["k1"]["project_status"] == "Archived" and by_chunk["k1"]["archived"]
    assert "project_status" not in by_chunk["k2"]
    ctx = {c["framing"]: c for c in out["spine_context"]}
    assert ctx["Old brief"]["project_status"] == "Archived"
    assert "project_status" not in ctx["New brief"]
    assert out["finished_project_hits"] == 2
    assert len(out["results"]) == 2 and len(out["spine_context"]) == 2

    scoped = server.semantic_search("brief", project_code=ARCHIVED)
    assert scoped["project_status"] == "Archived" and scoped["archived"] is True


def test_get_project_state_carries_the_status(server, db, monkeypatch, tmp_path):
    """The tree read resolves no MC-2 id of its own; a frozen cp.md next to
    live DB verbs is exactly where "is this still current?" needs answering."""
    (tmp_path / "snt").mkdir()
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "tree_root", lambda: tmp_path)
    monkeypatch.setattr(server, "find_project_dir", lambda root, code: tmp_path / "snt")
    monkeypatch.setattr(server, "extract_exec_summary", lambda p: ("**Status:** done", None))
    monkeypatch.setattr(server, "find_sprint_file", lambda *a: (None, "2026-W30", None))
    monkeypatch.setattr(server, "tree_provenance", lambda: {})

    archived = server.get_project_state(ARCHIVED)
    assert archived["project_status"] == "Archived" and archived["archived"] is True
    assert archived["exec_summary"] == "**Status:** done"
    live = server.get_project_state(LIVE)
    assert live["project_status"] == "Open" and "project_note" not in live
