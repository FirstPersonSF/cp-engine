"""`pull_project_source` reassembles a document in its stored order (#349).

`asset_chunks.id` is a random uuid and the table has no insert timestamp, so
the rows PostgREST hands back are in no particular order. The ingest pipeline
has stamped each chunk's position into `meta.chunk_index` since 1p-lib
6a0f2db, and mc-2 migration 200 adds a typed `chunk_index` column fed from
it. The hosted pull read neither, so stamped documents came back shuffled:

  - slt-5196 "SLT 5196 - Shawn Fontaine - Transcript": 42 chunks, all
    stamped 0..41, returned as 0..8, 14, 16, 9, 18, 27, 36, 10, ... (live,
    2026-10-21);
  - "Brand Campaign Storyline Map_v02" (10 tabs): tabs interleaved;
  - "Competitive Messaging Resource Library.csv": rows out of order.

The fake below honours the select list the way PostgREST does — an
`alias:meta->>key` projection, a plain column, and a 42703 for a column the
schema does not have — so a reader that never asks for the position cannot
receive it by accident, and the pre-migration schema is testable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _MissingColumn(Exception):
    """PostgREST's answer to a select naming a column the table lacks."""


class _Q:
    def __init__(self, rows, columns):
        self._rows, self._columns, self._f, self._cols = rows, columns, [], None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        self._cols = [c.strip() for c in cols.split(",") if c.strip()]
        for c in self._cols:
            src = c.split(":", 1)[-1]
            base = src.split("->>", 1)[0]
            if self._columns is not None and base not in self._columns:
                raise _MissingColumn(
                    f"{{'code': '42703', 'message': 'column asset_chunks.{base} "
                    "does not exist'}"
                )
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def _project(self, row):
        if self._cols is None:
            return dict(row)
        out = {}
        for c in self._cols:
            alias, _, src = c.rpartition(":")
            if "->>" in src:
                base, key = src.split("->>", 1)
                val = (row.get(base) or {}).get(key)
                out[alias or key] = None if val is None else str(val)
            else:
                out[alias or src] = row.get(src)
        return out

    def execute(self):
        rows = [self._project(r) for r in self._rows if all(f(r) for f in self._f)]
        return SimpleNamespace(data=rows)


_CHUNK_COLUMNS_PRE = {"id", "asset_id", "start_seconds", "end_seconds", "text",
                      "meta", "content_hash"}
_CHUNK_COLUMNS_POST = _CHUNK_COLUMNS_PRE | {"chunk_index"}


class _DB:
    def __init__(self, chunks, *, migrated=True):
        self.chunk_columns = _CHUNK_COLUMNS_POST if migrated else _CHUNK_COLUMNS_PRE
        self.tables = {
            "rag_assets": [{"id": "t", "title": "SLT 5196 - Shawn Fontaine - Transcript",
                            "source_type": "doc", "status": "active",
                            "created_at": "2026-10-19"}],
            "asset_chunks": chunks,
        }

    def table(self, name):
        cols = self.chunk_columns if name == "asset_chunks" else None
        return _Q(self.tables.get(name, []), cols)


# The live storage order of the Fontaine transcript's first 20 chunks.
_LIVE_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 14, 16, 9, 18, 10, 19, 11, 12, 13, 15, 17]


def _stamped(order, *, column=False, meta=True):
    rows = []
    for i in order:
        row = {"id": f"u{i}", "asset_id": "t", "text": f"[{i:02d}] block",
               "start_seconds": None, "end_seconds": None,
               "meta": {"chunk_index": i} if meta else {}}
        if column:
            row["chunk_index"] = i
        rows.append(row)
    return rows


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
    return server.pull_project_source("t", **kw)


def _blocks(text):
    return [int(b[1:3]) for b in text.split("\n\n")]


def test_meta_stamped_chunks_come_back_in_document_order(server, monkeypatch):
    # Rows as they are stored today: meta.chunk_index set, column absent.
    out = _pull(server, monkeypatch, _DB(_stamped(_LIVE_ORDER), migrated=False))
    assert _blocks(out["text"]) == sorted(_LIVE_ORDER)
    assert out["order"] == "chunk_index"


def test_column_orders_after_migration_200(server, monkeypatch):
    # A writer that sets only the typed column is ordered by it too.
    out = _pull(server, monkeypatch,
                _DB(_stamped(_LIVE_ORDER, column=True, meta=False)))
    assert _blocks(out["text"]) == sorted(_LIVE_ORDER)
    assert out["order"] == "chunk_index"


def test_legacy_rows_keep_store_order_and_say_order_is_unknown(server, monkeypatch):
    # Pre-stamp ingests carry no position anywhere: the pull cannot know the
    # order, keeps what the store returned, and says so instead of implying it.
    out = _pull(server, monkeypatch, _DB(_stamped([2, 0, 1], meta=False)))
    assert _blocks(out["text"]) == [2, 0, 1]
    assert out["order"] == "unknown"
    assert "not guaranteed" in out["order_note"]


def test_partly_stamped_document_is_not_reported_as_ordered(server, monkeypatch):
    rows = _stamped([1, 0]) + _stamped([5], meta=False)
    out = _pull(server, monkeypatch, _DB(rows, migrated=False))
    assert out["order"] == "unknown"


def test_time_coded_media_orders_by_start_seconds(server, monkeypatch):
    rows = [{"id": f"m{i}", "asset_id": "t", "text": f"[{i:02d}] t",
             "start_seconds": s, "end_seconds": None, "meta": {}}
            for i, s in ((1, 30.0), (0, 0.0), (2, 60.0))]
    out = _pull(server, monkeypatch, _DB(rows, migrated=False))
    assert _blocks(out["text"]) == [0, 1, 2]
    assert out["order"] == "start_seconds"
    assert "order_note" not in out


def test_empty_source_carries_no_order_claim(server, monkeypatch):
    out = _pull(server, monkeypatch, _DB([]))
    assert out["empty"] is True
    assert "order" not in out
