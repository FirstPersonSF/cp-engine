"""`pull_project_source` pages past PostgREST's row cap (#349 item 6).

The hosted pull read a document's chunks in ONE request. PostgREST caps a
response at `max-rows` (1000 on MC-2), so a document over 1,000 chunks came
back cut at chunk 1,000 — and `truncated` said false, because the only
truncation it tracked was its own character cap. The fake below enforces the
cap the way the server does: any single request returns at most 1,000 rows,
whatever `.range()` asks for, and a request without `.range()` gets the first
1,000.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

_MAX_ROWS = 1000


class _Q:
    def __init__(self, rows):
        self._rows, self._f, self._order, self._range = rows, [], None, None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def order(self, col, *a, **k):
        self._order = col
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = [dict(r) for r in self._rows if all(f(r) for f in self._f)]
        if self._order:
            rows.sort(key=lambda r: r.get(self._order))
        start, end = self._range or (0, len(rows) - 1)
        window = rows[start:end + 1]
        return SimpleNamespace(data=window[:_MAX_ROWS])


class _DB:
    def __init__(self, n):
        # Stored in reverse so a reader that forgets to sort is caught too.
        self.chunks = [
            {"id": f"u{i:06d}", "asset_id": "big", "text": f"[{i}]",
             "start_seconds": None, "end_seconds": None,
             "chunk_index": i, "meta_chunk_index": str(i)}
            for i in reversed(range(n))
        ]
        self.assets = [{"id": "big", "title": "Huge Deck", "source_type": "doc",
                        "status": "active", "created_at": "2026-10-21"}]

    def table(self, name):
        return _Q(self.chunks if name == "asset_chunks" else self.assets)


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


def _pull(server, monkeypatch, db, **kw):
    monkeypatch.setattr(server, "user_client", lambda: db)
    monkeypatch.setattr(server, "caller_subject", lambda: "u")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    return server.pull_project_source("big", max_chars=10_000_000, **kw)


def test_document_over_the_row_cap_is_read_whole_and_in_order(server, monkeypatch):
    out = _pull(server, monkeypatch, _DB(2500))
    assert out["chunk_count"] == 2500
    assert out["text"].split("\n\n") == [f"[{i}]" for i in range(2500)]
    assert out["order"] == "chunk_index"
    assert out["truncated"] is False


def test_exact_multiple_of_the_cap_is_whole(server, monkeypatch):
    out = _pull(server, monkeypatch, _DB(2000))
    assert out["chunk_count"] == 2000
    assert out["truncated"] is False


def test_a_read_stopped_by_the_bound_says_so(server, monkeypatch):
    from cp_engine import chunk_order

    monkeypatch.setattr(chunk_order, "MAX_DOC_CHUNKS", 1500)
    out = _pull(server, monkeypatch, _DB(2500))
    assert out["chunk_count"] == 1500
    assert out["truncated"] is True
    assert "1500" in out["truncated_note"]


def test_a_document_exactly_at_the_bound_is_not_flagged(server, monkeypatch):
    from cp_engine import chunk_order

    monkeypatch.setattr(chunk_order, "MAX_DOC_CHUNKS", 2000)
    out = _pull(server, monkeypatch, _DB(1999))
    assert out["chunk_count"] == 1999
    assert out["truncated"] is False
    assert "truncated_note" not in out
