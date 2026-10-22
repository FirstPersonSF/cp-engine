"""A pull says how much of a source the ingest actually extracted (#349).

The parsers record `rag_assets.meta.coverage` (document-ingest 7492bdd):
slides or pages seen against those that bore text. Before this, the stdio
`pull_source` and `list_sources` never read it, so a deck stored as titles
only read exactly like a complete one (sap-5200 "Competitive Messaging
Editorial Framework.pptx", 2026-10-07).

This file imports only what existed before the fix, so on unfixed code it
fails for the right reason (no `coverage` / `coverage_warning` in the
result), not an ImportError.
"""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.project_sources import list_sources, pull_source

TITLE = "Competitive Messaging Editorial Framework.pptx"

PARTIAL = {"unit": "slide", "total": 12, "with_text": 4,
           "without_text": [3, 5, 6, 7, 8, 9, 10, 11],
           "title_only": [], "with_notes": 0}
FULL = {"unit": "slide", "total": 3, "with_text": 3, "without_text": [],
        "title_only": [], "with_notes": 1}


class _Q:
    """PostgREST-ish: honours `alias:meta->key` and `meta->>key` projections
    and refuses `*`, so a reader that never asks for coverage cannot get it."""

    def __init__(self, rows):
        self._rows, self._f, self._cols = rows, [], None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        assert not any(c.strip() == "meta" for c in cols.split(","))
        self._cols = [c.strip() for c in cols.split(",") if c.strip()]
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def in_(self, col, vals):
        self._f.append(lambda r, c=col, v=tuple(vals): r.get(c) in v)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def range(self, *a, **k):
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


class _Client:
    def __init__(self, coverage):
        self.assets = [{
            "id": "a1", "title": TITLE, "source_type": "doc", "status": "active",
            "created_at": "2026-10-07", "project_id": "p", "scope": "project",
            "company_id": "c", "file_hash": "h", "prev_asset_id": None,
            "meta": {"coverage": coverage} if coverage is not None else {},
        }]
        # The stored chunk the RPC row above came from: the pull reads the
        # resolved document straight from the table since #349 item 6.
        self.chunks = [{"asset_id": "a1", "id": "k1", "text": "[Slide 1]\nOverview",
                        "chunk_index": 0, "meta": {"chunk_index": 0}}]

    def rpc(self, name, params):
        assert name == "read_scoped_asset_chunks"
        rows = [{"text": "[Slide 1]\nOverview", "title": TITLE, "scope": "project",
                 "citation_url": "u", "chunk_index": 0}]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))

    def table(self, name):
        return _Q(self.assets if name == "rag_assets" else self.chunks)


def test_partial_deck_pull_carries_coverage_and_a_warning():
    out = pull_source(_Client(PARTIAL), "p", "c", doc_title=TITLE)
    assert out["chunks"] == ["[Slide 1]\nOverview"]
    assert out["coverage"]["total"] == 12
    assert out["coverage"]["with_text"] == 4
    warning = out["coverage_warning"]
    assert warning.startswith("12 slides, 4 with text — 8 image-only: 3,5,6,7")
    assert "fetch_project_source" in warning


def test_complete_deck_pull_has_coverage_but_no_warning():
    out = pull_source(_Client(FULL), "p", "c", doc_title=TITLE)
    assert out["coverage"]["with_text"] == 3
    assert "coverage_warning" not in out


def test_legacy_row_without_coverage_claims_nothing():
    out = pull_source(_Client(None), "p", "c", doc_title=TITLE)
    assert "coverage" not in out
    assert "coverage_warning" not in out


def test_list_flags_partial_coverage():
    rows = list_sources(_Client(PARTIAL), "p", "c")
    assert rows[0]["coverage_flag"] == "4/12 slides with text"
    assert "coverage_flag" not in list_sources(_Client(FULL), "p", "c")[0]
