"""`cp_engine.chunk_order` — the one ordering rule both pull paths share (#349)."""
from __future__ import annotations

from cp_engine.chunk_order import (
    UNKNOWN_ORDER_NOTE,
    chunk_position,
    is_missing_column,
    order_chunks,
    order_fields,
)


def test_position_prefers_the_column_then_the_projection_then_meta():
    assert chunk_position({"chunk_index": 3, "meta_chunk_index": "9"}) == 3
    assert chunk_position({"chunk_index": None, "meta_chunk_index": "9"}) == 9
    assert chunk_position({"meta": {"chunk_index": 4}}) == 4
    assert chunk_position({"meta": {"chunk_index": "4"}}) == 4
    assert chunk_position({"meta": {}}) is None


def test_position_rejects_values_that_are_not_an_index():
    for bad in (True, -1, 1.5, "", "x", "4-5"):
        assert chunk_position({"chunk_index": bad}) is None


def test_positions_sort_numerically_not_lexically():
    rows = [{"meta_chunk_index": s} for s in ("10", "2", "1")]
    out, label = order_chunks(rows)
    assert [r["meta_chunk_index"] for r in out] == ["1", "2", "10"]
    assert label == "chunk_index"


def test_duplicate_positions_are_not_a_document_order():
    # Two same-titled assets read as one (the RPC carries no asset id) give
    # 0,0,1,1 — sorting that interleaves two documents.
    rows = [{"chunk_index": i} for i in (0, 1, 0, 1)]
    out, label = order_chunks(rows)
    assert out == rows and label == "unknown"


def test_fallback_key_applies_only_when_order_is_unproven():
    rows = [{"page": 2}, {"page": 1}]
    out, label = order_chunks(rows, fallback_key=lambda r: r["page"])
    assert [r["page"] for r in out] == [1, 2] and label == "unknown"


def test_no_rows_claims_no_order():
    assert order_chunks([]) == ([], None)
    assert order_fields(None) == {}


def test_order_fields_note_only_when_unknown():
    assert order_fields("chunk_index") == {"order": "chunk_index"}
    assert order_fields("unknown") == {"order": "unknown",
                                       "order_note": UNKNOWN_ORDER_NOTE}


def test_missing_column_matches_only_the_named_column():
    assert is_missing_column(Exception(
        "{'code': '42703', 'message': 'column asset_chunks.chunk_index does not exist'}"))
    assert not is_missing_column(Exception("42501 permission denied for table asset_chunks"))
    assert not is_missing_column(Exception("column asset_chunks.text does not exist"))
