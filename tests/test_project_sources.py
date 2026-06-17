"""Tests for `cp_engine.project_sources` — pure list_sources + pull_source.

No Supabase / Voyage. A small fake supabase client mirrors the PostgREST chain
(`.table().select().eq().order().execute()`) for list_sources and the
`.rpc(name, params).execute()` shape for pull_source's scoped read.
"""

from __future__ import annotations

from cp_engine.project_sources import list_sources, pull_source


# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeExecute:
    def __init__(self, data):
        self.data = data


class _FakeTableQuery:
    """Records select() columns, eq() filters, order() args; returns canned rows."""

    def __init__(self, data, recorder):
        self._data = data
        self._recorder = recorder

    def select(self, columns):
        self._recorder["select"] = columns
        return self

    def eq(self, col, val):
        self._recorder.setdefault("eq", []).append((col, val))
        return self

    def order(self, col, desc=False):
        self._recorder["order"] = (col, desc)
        return self

    def execute(self):
        return _FakeExecute(self._data)


class _FakeTableClient:
    def __init__(self, rows):
        self._rows = rows
        self.recorder: dict = {}

    def table(self, name):
        self.recorder["table"] = name
        return _FakeTableQuery(self._rows, self.recorder)


class _FakeRpcClient:
    """Records the `.rpc(name, params).execute()` call; returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[dict] = []

    def rpc(self, name, params):
        self.calls.append({"name": name, "params": params})
        return self

    def execute(self):
        return _FakeExecute(list(self._rows))


# ──────────────────────────────────────────────────────────────────────
#  list_sources
# ──────────────────────────────────────────────────────────────────────


_ASSET_ROWS = [
    # newest active
    {
        "id": "a-new",
        "title": "Concur Storybook",
        "source_type": "drive",
        "status": "active",
        "created_at": "2026-06-10T00:00:00Z",
    },
    # older active
    {
        "id": "a-old",
        "title": "SOW.pdf",
        "source_type": "dropbox",
        "status": "active",
        "created_at": "2026-06-01T00:00:00Z",
    },
    # archived — excluded by the status='active' filter (fake returns only what
    # the real query would; we hand it pre-filtered active rows below)
]


def test_list_sources_returns_active_newest_first():
    # The fake stands in for the DB: it returns the rows the query would, i.e.
    # already status='active'-filtered, in the order='created_at desc' order.
    client = _FakeTableClient(_ASSET_ROWS)

    out = list_sources(client, "proj-1", "co-9")

    assert client.recorder["table"] == "rag_assets"
    # explicit columns, never meta/file_path/*
    select = client.recorder["select"]
    assert "*" not in select
    assert "meta" not in select
    assert "file_path" not in select
    for col in ("id", "title", "source_type", "status", "created_at"):
        assert col in select
    # filtered on project + active, ordered newest first
    assert ("project_id", "proj-1") in client.recorder["eq"]
    assert ("status", "active") in client.recorder["eq"]
    assert client.recorder["order"] == ("created_at", True)

    assert [r["id"] for r in out] == ["a-new", "a-old"]
    first = out[0]
    assert set(first.keys()) == {"id", "title", "source_type", "created_at"}
    assert first["title"] == "Concur Storybook"
    assert first["source_type"] == "drive"
    assert "summary" not in first


def test_list_sources_excludes_non_active_via_filter():
    # Prove the eq('status','active') filter is applied. The fake can't filter,
    # so we assert the filter was requested (the DB would drop non-active rows).
    client = _FakeTableClient(_ASSET_ROWS)
    list_sources(client, "proj-1", "co-9")
    assert ("status", "active") in client.recorder["eq"]


def test_list_sources_merges_summaries():
    client = _FakeTableClient(_ASSET_ROWS)
    out = list_sources(
        client,
        "proj-1",
        "co-9",
        summaries={"a-new": "A storybook for Concur."},
    )
    by_id = {r["id"]: r for r in out}
    assert by_id["a-new"]["summary"] == "A storybook for Concur."
    # No cached summary for a-old → key omitted, not None.
    assert "summary" not in by_id["a-old"]


def test_list_sources_empty():
    client = _FakeTableClient([])
    assert list_sources(client, "proj-1", "co-9") == []


# ──────────────────────────────────────────────────────────────────────
#  pull_source
# ──────────────────────────────────────────────────────────────────────


# Note: the match rule is plain case-insensitive substring (query ⊆ stored
# title), per the documented contains-or-equal contract. So the query
# "Concur Storybook" matches a stored title that CONTAINS that phrase, e.g.
# "WW Internal Concur Storybook 2026" — not a word-reordered variant.
_CHUNK_ROWS = [
    {
        "text": "Concur chunk one",
        "citation_url": "https://drive/concur#p1",
        "title": "WW Internal Concur Storybook 2026",
        "scope": "project",
    },
    {
        "text": "Master deck chunk",
        "citation_url": "https://drive/deck#p1",
        "title": "Master Deck.pdf",
        "scope": "account",
    },
    {
        "text": "Concur chunk two",
        "citation_url": "https://drive/concur#p2",
        "title": "WW Internal Concur Storybook 2026",
        "scope": "project",
    },
]


def test_pull_source_filters_to_named_doc_in_order():
    client = _FakeRpcClient(_CHUNK_ROWS)

    out = pull_source(client, "proj-1", "co-9", doc_title="Concur Storybook")

    # RPC was called with no embedding (recency) and the given limit.
    call = client.calls[0]
    assert call["name"] == "read_scoped_asset_chunks"
    assert call["params"]["p_project_id"] == "proj-1"
    assert call["params"]["p_company_id"] == "co-9"
    assert call["params"]["p_query_embedding"] is None
    assert call["params"]["p_limit"] == 50

    # Only the Concur doc's chunks, in returned order.
    assert out["chunks"] == ["Concur chunk one", "Concur chunk two"]
    assert out["title"] == "WW Internal Concur Storybook 2026"
    assert out["citation_url"] == "https://drive/concur#p1"
    assert out["scope"] == "project"
    assert "note" not in out


def test_pull_source_case_insensitive_substring():
    client = _FakeRpcClient(_CHUNK_ROWS)
    out = pull_source(client, "proj-1", "co-9", doc_title="concur")
    assert out["chunks"] == ["Concur chunk one", "Concur chunk two"]


def test_pull_source_exact_title_match():
    client = _FakeRpcClient(_CHUNK_ROWS)
    out = pull_source(
        client, "proj-1", "co-9", doc_title="Master Deck.pdf"
    )
    assert out["chunks"] == ["Master deck chunk"]
    assert out["scope"] == "account"


def test_pull_source_no_match_returns_note():
    client = _FakeRpcClient(_CHUNK_ROWS)
    out = pull_source(client, "proj-1", "co-9", doc_title="Nonexistent Doc")
    assert out["chunks"] == []
    assert out["title"] == "Nonexistent Doc"
    assert "no source named 'Nonexistent Doc'" in out["note"]


def test_pull_source_respects_limit():
    client = _FakeRpcClient(_CHUNK_ROWS)
    pull_source(client, "proj-1", "co-9", doc_title="Concur", limit=12)
    assert client.calls[0]["params"]["p_limit"] == 12


# ──────────────────────────────────────────────────────────────────────
#  pull_source — query mode (Voyage embed wired)
# ──────────────────────────────────────────────────────────────────────


class _FakeEmbedder:
    def __init__(self, vector):
        self._vector = vector
        self.embedded: list[str] = []

    def embed(self, text):
        self.embedded.append(text)
        return self._vector


def test_pull_source_query_mode_passes_embedding():
    client = _FakeRpcClient(_CHUNK_ROWS)
    embedder = _FakeEmbedder([0.1, 0.2, 0.3])

    out = pull_source(
        client,
        "proj-1",
        "co-9",
        doc_title="Concur",
        query="what is the brand voice?",
        embedder=embedder,
    )

    # The query was embedded and passed to the RPC as p_query_embedding.
    assert embedder.embedded == ["what is the brand voice?"]
    assert client.calls[0]["params"]["p_query_embedding"] == [0.1, 0.2, 0.3]
    # Title filtering still applies on top of the vector-ranked rows.
    assert out["chunks"] == ["Concur chunk one", "Concur chunk two"]
