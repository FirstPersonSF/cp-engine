"""#363: hosted search and listing gaps.

1. `list_project_sources` on a mature project (ibx-5153, 140 docs) returned
   ~83K characters — over the tool-result limit — with no way to ask for one
   title or for what just landed. It now takes `title_contains` and `limit`.
2. An unscoped `semantic_search` hit a Postgres statement timeout (`57014`)
   and returned a raw RPC error; it now says to scope the search.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _Q:
    def __init__(self, rows):
        self._rows, self._f, self._lo, self._hi = rows, [], 0, None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r, c=col: r.get(c) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        return self

    def execute(self):
        rows = [dict(r) for r in self._rows if all(f(r) for f in self._f)]
        return SimpleNamespace(data=rows[self._lo:(self._hi + 1 if self._hi is not None else None)])


def _asset(i, title, created):
    return {"id": i, "project_id": "p-job", "company_id": "co-1", "scope": "project",
            "title": title, "status": "active", "created_at": created}


class _DB:
    def __init__(self):
        self.tables = {
            "projects": [{"id": "p-job", "company_id": "co-1", "mc_status": "Open"}],
            "rag_assets": [
                _asset("a", "IBX-5153_Campaign-Brief_v10.md", "2026-10-27T00:29:38"),
                _asset("b", "IBX-5153_Campaign-Concepts_v11_editorial-pass.md", "2026-10-27T00:29:32"),
                _asset("c", "ibx_5153_ai_campaign (2026-10-26)", "2026-10-26T17:32:00"),
                _asset("d", "Brand_Studio_Strategy FY27_BrandCampPreso.pptx", "2026-10-19T21:42:00"),
            ],
            "asset_chunks": [
                {"id": f"c-{x}", "asset_id": x, "text": "t", "start_seconds": None}
                for x in "abcd"
            ],
        }

    def table(self, name):
        return _Q(self.tables.get(name, []))


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
    monkeypatch.setattr(server, "_owner_columns", lambda _c: ("project_id",))
    return db


# ── list_project_sources: title_contains / limit ─────────────────────


def test_title_contains_returns_only_matching_sources(server, db):
    out = server.list_project_sources("p-job", title_contains="campaign-")

    assert [s["title"] for s in out["sources"]] == [
        "IBX-5153_Campaign-Brief_v10.md",
        "IBX-5153_Campaign-Concepts_v11_editorial-pass.md",
    ]
    assert out["count"] == 2
    assert out["filtered_from"] == 4


def test_limit_returns_the_newest_first(server, db):
    out = server.list_project_sources("p-job", limit=2)

    assert [s["asset_id"] for s in out["sources"]] == ["a", "b"]
    assert out["filtered_from"] == 4


def test_a_filter_that_matches_nothing_says_so(server, db):
    out = server.list_project_sources("p-job", title_contains="nope")

    assert out["sources"] == []
    assert "nope" in out.get("note", "")


def test_no_filter_keeps_the_full_listing(server, db):
    out = server.list_project_sources("p-job")

    assert out["count"] == 4
    assert "filtered_from" not in out


# ── semantic_search: an unscoped timeout says to scope ────────────────


class _TimeoutRPC:
    def __init__(self, name):
        self.name = name

    def execute(self):
        if self.name == "match_chunks_for_project":
            raise RuntimeError(
                "{'code': '57014', 'message': 'canceling statement due to statement timeout'}"
            )
        return SimpleNamespace(data=[])


def test_unscoped_search_timeout_tells_the_caller_to_scope(server, db, monkeypatch):
    db.rpc = lambda name, params: _TimeoutRPC(name)
    monkeypatch.setattr(server, "embedding_available", lambda: (True, ""))
    monkeypatch.setattr(server, "embed_query", lambda text: [0.1, 0.2])

    out = server.semantic_search("go/safety health topics list pages")

    assert out["results"] == []
    assert "project_code" in out["error"]
    assert "timed out" in out["error"]
