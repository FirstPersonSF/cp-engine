"""Hosted `pull_project_source` / `list_project_sources` report extraction
coverage (#349).

The parsers record `rag_assets.meta.coverage` (document-ingest 7492bdd):
slides or pages seen against those that bore text. The hosted tools never
read it, so a deck stored as titles only (sap-5200) and a PDF that lost
13 of 19 pages (Navan eBook) pulled with `truncated: false` and nothing
else to say.

The fake honours the select list the way PostgREST does (`alias:meta->key`
returns the jsonb value, `meta->>key` its text), and refuses `*` and a bare
`meta`, so the tool cannot get coverage without asking for it and cannot get
it by pulling the whole blob.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

PARTIAL_PDF = {"unit": "page", "total": 19, "with_text": 6,
               "without_text": [2, 3, 4, 5, 7, 8, 9, 10, 15, 16, 17, 18, 19],
               "method": "text"}


class _Q:
    def __init__(self, rows):
        self._rows, self._f, self._cols = rows, [], None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        self._cols = [c.strip() for c in cols.split(",") if c.strip()]
        assert "meta" not in self._cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def in_(self, col, vals):
        self._f.append(lambda r, c=col, v=tuple(vals): r.get(c) in v)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def _project(self, row):
        out = {}
        for c in self._cols:
            alias, _, src = c.rpartition(":")
            if "->>" in src:
                base, key = src.split("->>", 1)
                val = (row.get(base) or {}).get(key)
                out[alias or key] = None if val is None else str(val)
            elif "->" in src:
                base, key = src.split("->", 1)
                out[alias or key] = (row.get(base) or {}).get(key)
            else:
                out[alias or src] = row.get(src)
        return out

    def execute(self):
        return SimpleNamespace(
            data=[self._project(r) for r in self._rows if all(f(r) for f in self._f)]
        )


class _DB:
    def __init__(self, coverage):
        self.tables = {
            "rag_assets": [{
                "id": "t", "title": "Navan eBook.pdf", "source_type": "pdf",
                "status": "active", "created_at": "2026-10-07", "project_id": "p",
                "scope": "project",
                "meta": {"coverage": coverage} if coverage is not None else {},
            }],
            "asset_chunks": [{"id": "c0", "asset_id": "t", "text": "page one",
                              "start_seconds": None, "end_seconds": None,
                              "chunk_index": 0, "meta": {"chunk_index": 0}}],
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


def _patch(server, monkeypatch, db):
    monkeypatch.setattr(server, "user_client", lambda: db)
    monkeypatch.setattr(server, "caller_subject", lambda: "u")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)


def test_partial_pdf_pull_carries_coverage_and_a_warning(server, monkeypatch):
    _patch(server, monkeypatch, _DB(PARTIAL_PDF))
    out = server.pull_project_source("t")
    assert out["text"] == "page one"
    assert out["coverage"]["total"] == 19
    assert out["coverage"]["with_text"] == 6
    assert out["coverage_warning"].startswith(
        "19 pages, 6 with text — 13 without text: 2,3,4,5,7"
    )


def test_legacy_row_pull_claims_nothing(server, monkeypatch):
    _patch(server, monkeypatch, _DB(None))
    out = server.pull_project_source("t")
    assert "coverage" not in out
    assert "coverage_warning" not in out


def test_list_flags_partial_coverage(server, monkeypatch):
    _patch(server, monkeypatch, _DB(PARTIAL_PDF))
    monkeypatch.setattr(server, "resolve_project_id", lambda c, code: "p")
    monkeypatch.setattr(server, "_owner_columns", lambda c: ["project_id"])
    monkeypatch.setattr(server, "_with_project_status", lambda d, *a, **k: d)
    out = server.list_project_sources("x")
    assert out["sources"][0]["coverage_flag"] == "6/19 pages with text"
