"""Task C9 — read_scoped_chunks: the scoped read query (consumption payoff).

This is how engines retrieve a project's readable asset text: its OWN
project-scoped assets PLUS its company's account-scoped assets, excluding
archived, optionally vector-ordered by a query embedding.

The cross-table JOIN + OR-across-two-columns + pgvector `<=>` ordering can't be
expressed cleanly through supabase-py's PostgREST builder, so the SQL lives in a
Postgres RPC (`read_scoped_asset_chunks`, created by the Phase B migration). cp
just CALLS the RPC. These tests lock the cp-side RPC call contract: the function
name and the params passed. They deliberately do NOT test the SQL filtering /
ordering — that's in the DB and is a Phase D integration check.
"""

from __future__ import annotations

from cp_engine.asset_ingest import read_scoped_chunks


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeRpcClient:
    """Records the single `.rpc(name, params).execute()` call and returns canned data."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []  # list of {name, params}

    def rpc(self, name, params):
        self.calls.append({"name": name, "params": params})
        return self

    def execute(self):
        return _FakeResponse(list(self._rows))


_ROWS = [
    {
        "text": "chunk one",
        "citation_url": "https://drive/abc#p1",
        "title": "Brief.docx",
        "scope": "project",
    },
    {
        "text": "chunk two",
        "citation_url": "https://drive/def#p2",
        "title": "Master Deck.pdf",
        "scope": "account",
    },
]


def test_read_scoped_calls_rpc_with_params():
    client = _FakeRpcClient(_ROWS)

    out = read_scoped_chunks(client, "proj-1", "co-9", limit=5)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["name"] == "read_scoped_asset_chunks"
    assert call["params"]["p_project_id"] == "proj-1"
    assert call["params"]["p_company_id"] == "co-9"
    assert call["params"]["p_limit"] == 5
    # No embedding given → param present but None (recency fallback in SQL).
    assert call["params"]["p_query_embedding"] is None
    assert out == _ROWS


def test_read_scoped_passes_query_embedding_when_given():
    client = _FakeRpcClient(_ROWS)
    embedding = [0.1, 0.2, 0.3]

    read_scoped_chunks(
        client, "proj-1", "co-9", query_embedding=embedding, limit=20
    )

    params = client.calls[0]["params"]
    assert params["p_query_embedding"] == embedding
    assert params["p_limit"] == 20


def test_read_scoped_defaults_no_embedding():
    client = _FakeRpcClient([])

    read_scoped_chunks(client, "proj-1", "co-9")

    params = client.calls[0]["params"]
    assert params["p_query_embedding"] is None
    # Default limit is 20 per the function signature / RPC default.
    assert params["p_limit"] == 20
