"""The stdio `pull_source` says whether its chunk order is guaranteed (#349).

It already sorts by `chunk_index` (#152) when the scoped-read RPC returns
one; what it never did was tell the reader when it could NOT — a pre-stamp
document came back in RPC order looking exactly like an ordered one. This
file imports only what exists before the fix, so it fails on unfixed code
for the right reason (a missing `order` flag), not an ImportError.
"""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.project_sources import pull_source


class _Rpc:
    def __init__(self, rows):
        self._rows = rows

    def rpc(self, name, params):
        assert name == "read_scoped_asset_chunks"
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=list(self._rows)))


def _row(text, ci=None, page=None):
    return {"text": text, "title": "Storyline Map", "scope": "project",
            "citation_url": "u", "chunk_index": ci, "page": page}


def test_fully_indexed_document_is_reported_ordered():
    out = pull_source(_Rpc([_row("b", 1), _row("a", 0)]), "p", "c",
                      doc_title="Storyline Map")
    assert out["chunks"] == ["a", "b"]
    assert out["order"] == "chunk_index"
    assert "order_note" not in out


def test_unindexed_document_is_flagged_order_unknown():
    out = pull_source(_Rpc([_row("x"), _row("y")]), "p", "c",
                      doc_title="Storyline Map")
    assert out["chunks"] == ["x", "y"]  # fallback unchanged: RPC order
    assert out["order"] == "unknown"
    assert "not guaranteed" in out["order_note"]


def test_page_fallback_is_still_order_unknown():
    # Page labels order PAGES, not the chunks within one — not a guarantee.
    out = pull_source(_Rpc([_row("p2", page=2), _row("p1", page=1)]), "p", "c",
                      doc_title="Storyline Map")
    assert out["chunks"] == ["p1", "p2"]
    assert out["order"] == "unknown"


def test_partly_indexed_document_is_flagged_order_unknown():
    out = pull_source(_Rpc([_row("a", 0), _row("z")]), "p", "c",
                      doc_title="Storyline Map")
    assert out["order"] == "unknown"
