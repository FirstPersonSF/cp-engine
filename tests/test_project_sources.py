"""Tests for `cp_engine.project_sources` — pure list_sources + pull_source.

No Supabase / Voyage. A small fake supabase client mirrors the PostgREST chain
(`.table().select().eq().order().execute()`) for list_sources and the
`.rpc(name, params).execute()` shape for pull_source's scoped read.
"""

from __future__ import annotations

from cp_engine.project_sources import (
    _MISS_RETRY_LIMIT,
    list_sources,
    list_spine,
    pull_source,
    pull_spine,
)


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
    assert set(first.keys()) == {
        "id", "title", "source_type", "created_at", "file_hash"
    }
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


def test_list_sources_excludes_assets_with_a_successor():
    # #57: an asset another row's prev_asset_id points at is a superseded
    # predecessor — even if its status flip didn't land (backlog residue),
    # the list must only show the newest copy.
    rows = [
        {
            "id": "a-v2",
            "title": "Brief.pdf",
            "source_type": "drive",
            "status": "active",
            "created_at": "2026-07-01T00:00:00Z",
            "prev_asset_id": "a-v1",
        },
        {
            "id": "a-v1",
            "title": "Brief.pdf",
            "source_type": "drive",
            "status": "active",
            "created_at": "2026-06-01T00:00:00Z",
            "prev_asset_id": None,
        },
        {
            "id": "b",
            "title": "Other.pdf",
            "source_type": "dropbox",
            "status": "active",
            "created_at": "2026-05-01T00:00:00Z",
            "prev_asset_id": None,
        },
    ]
    client = _FakeTableClient(rows)

    out = list_sources(client, "proj-1", "co-9")

    assert [r["id"] for r in out] == ["a-v2", "b"]
    # prev_asset_id is selected (needed for the successor check) but the
    # returned entries keep the manifest shape (no chain plumbing leaked).
    assert "prev_asset_id" in client.recorder["select"]
    assert all("prev_asset_id" not in r for r in out)


def test_drop_superseded_assets_helper():
    from cp_engine.project_sources import drop_superseded_assets

    rows = [
        {"id": "new", "prev_asset_id": "old"},
        {"id": "old", "prev_asset_id": None},
        {"id": "solo", "prev_asset_id": None},
        # successor pointing OUTSIDE the set: nothing in-set to drop
        {"id": "ext", "prev_asset_id": "not-here"},
    ]
    out = drop_superseded_assets(rows)
    assert [r["id"] for r in out] == ["new", "solo", "ext"]


# ──────────────────────────────────────────────────────────────────────
#  list_spine — index of a project's live spine elements
# ──────────────────────────────────────────────────────────────────────

_SPINE_ROWS = [
    {
        "id": "sap-5171/_authored/brief/v1",
        "est_item_id": "_authored/brief",
        "framing": "Creative Directions for Jack",
        "layer": "output",
        "binding": "live",
        "status": "live",
        "serves": ["d-1", "d-2"],
        "body": "x" * 500,
    },
    {
        "id": "sap-5171/_authored/email/v1",
        "est_item_id": "_authored/email",
        "framing": "Email from Olivia",
        "layer": "Email",
        "binding": "unbound",
        "status": "live",
        "serves": [],
        "body": "y" * 6769,
    },
]


def test_list_spine_returns_live_elements_with_metadata():
    client = _FakeTableClient(_SPINE_ROWS)

    out = list_spine(client, "proj-1")

    assert client.recorder["table"] == "spine_substance"
    # explicit columns, never the body itself in the list / never `*`
    select = client.recorder["select"]
    assert "*" not in select
    # filtered to this project's LIVE elements
    assert ("project_id", "proj-1") in client.recorder["eq"]
    assert ("status", "live") in client.recorder["eq"]

    first = out[0]
    # the list carries metadata + a body LENGTH, never the full body
    # `done` resolves to None here incidentally — the fake client lacks .schema
    # so the real fetch fails fail-soft; real done coverage lives in
    # test_spine_done_read.py
    assert set(first.keys()) == {
        "est_item_id", "framing", "layer", "binding", "status",
        "serves_count", "body_len", "important", "note", "done", "scope",
        "version_label", "version_date",
    }
    assert "body" not in first
    assert first["est_item_id"] == "_authored/brief"
    assert first["serves_count"] == 2
    assert first["body_len"] == 500
    # an unbound element reports zero served items
    assert out[1]["serves_count"] == 0


def test_list_spine_empty():
    client = _FakeTableClient([])
    assert list_spine(client, "proj-1") == []


# ──────────────────────────────────────────────────────────────────────
#  pull_spine — full body of one live spine element
# ──────────────────────────────────────────────────────────────────────


def test_pull_spine_by_exact_est_item_id():
    client = _FakeTableClient(_SPINE_ROWS)

    out = pull_spine(client, "proj-1", "_authored/email")

    assert client.recorder["table"] == "spine_substance"
    assert ("project_id", "proj-1") in client.recorder["eq"]
    assert ("status", "live") in client.recorder["eq"]
    assert out["est_item_id"] == "_authored/email"
    assert out["framing"] == "Email from Olivia"
    assert out["layer"] == "Email"
    assert out["binding"] == "unbound"
    assert out["serves"] == []
    # the WHOLE body comes back (this is the point of the tool)
    assert out["body"] == "y" * 6769


def test_pull_spine_by_title_substring():
    client = _FakeTableClient(_SPINE_ROWS)

    out = pull_spine(client, "proj-1", "olivia")

    assert out["est_item_id"] == "_authored/email"
    assert out["body"] == "y" * 6769


def test_pull_spine_not_found_returns_note():
    client = _FakeTableClient(_SPINE_ROWS)

    out = pull_spine(client, "proj-1", "nonexistent-element")

    assert out["body"] == ""
    assert "no spine element" in out["error"].lower()


def test_pull_spine_ambiguous_title_returns_note():
    client = _FakeTableClient(_SPINE_ROWS)

    # "e" is a substring of both "...for Jack" (no) — pick a token in BOTH framings
    out = pull_spine(client, "proj-1", "from")  # only matches the email...
    assert out["est_item_id"] == "_authored/email"

    # a substring matching 2+ distinct elements, none exact → ambiguous note
    rows = [
        {**_SPINE_ROWS[0], "framing": "Report draft one"},
        {**_SPINE_ROWS[1], "framing": "Report draft two"},
    ]
    out2 = pull_spine(_FakeTableClient(rows), "proj-1", "report")
    assert out2["body"] == ""
    assert "ambiguous" in out2["error"].lower()


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


# Two DISTINCT documents whose titles both CONTAIN the substring "Brief".
# A substring query of "Brief" must NOT merge their chunks under one title.
_AMBIGUOUS_BRIEF_ROWS = [
    {
        "text": "creative brief chunk",
        "citation_url": "https://drive/u1",
        "title": "Creative Brief",
        "scope": "project",
    },
    {
        "text": "brief v2 chunk",
        "citation_url": "https://drive/u2",
        "title": "Brief v2",
        "scope": "account",
    },
]


def test_pull_source_ambiguous_title_does_not_merge():
    # "Brief" is a substring of BOTH "Creative Brief" and "Brief v2", with no
    # exact match. The two distinct docs must NOT collapse into one result.
    client = _FakeRpcClient(_AMBIGUOUS_BRIEF_ROWS)
    out = pull_source(client, "proj-1", "co-9", doc_title="Brief")

    # No merged chunks, no mislabeling — just an ambiguity note.
    assert out["chunks"] == []
    assert "ambiguous" in out["note"]
    assert "Creative Brief" in out["note"]
    assert "Brief v2" in out["note"]
    # Provenance is never corrupted: neither doc's content/citation is returned.
    assert "creative brief chunk" not in out.get("chunks", [])
    assert "brief v2 chunk" not in out.get("chunks", [])


# "Brief" is BOTH an exact title AND a substring of its sibling "Creative
# Brief". Exact-title preference must win — return only the exact doc.
_EXACT_PLUS_SIBLING_ROWS = [
    {
        "text": "exact brief chunk",
        "citation_url": "https://drive/exact",
        "title": "Brief",
        "scope": "project",
    },
    {
        "text": "creative brief chunk",
        "citation_url": "https://drive/creative",
        "title": "Creative Brief",
        "scope": "account",
    },
]


def test_pull_source_exact_title_wins_over_substring_siblings():
    client = _FakeRpcClient(_EXACT_PLUS_SIBLING_ROWS)
    out = pull_source(client, "proj-1", "co-9", doc_title="Brief")

    # Only the EXACT "Brief" doc — not "Creative Brief".
    assert out["chunks"] == ["exact brief chunk"]
    assert out["title"] == "Brief"
    assert out["citation_url"] == "https://drive/exact"
    assert out["scope"] == "project"
    assert "note" not in out


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


class _SequencedRpcClient:
    """Like _FakeRpcClient but returns a different canned row-set per call."""

    def __init__(self, row_sets):
        self._row_sets = list(row_sets)
        self.calls: list[dict] = []

    def rpc(self, name, params):
        self.calls.append({"name": name, "params": params})
        return self

    def execute(self):
        return _FakeExecute(list(self._row_sets[len(self.calls) - 1]))


def test_pull_source_miss_widens_window_and_finds_older_doc():
    # The recency window is scoped to ALL of the project's chunks, so a doc
    # older than the newest `limit` chunks is invisible on the first read.
    # The pull must widen once (to _MISS_RETRY_LIMIT) before giving up.
    old_doc_rows = [
        {
            "text": "Old doc chunk",
            "citation_url": "https://drive/old#p1",
            "title": "Older Strategy Doc.pdf",
            "scope": "project",
        }
    ]
    client = _SequencedRpcClient([_CHUNK_ROWS, _CHUNK_ROWS + old_doc_rows])

    out = pull_source(client, "proj-1", "co-9", doc_title="Older Strategy Doc.pdf")

    assert len(client.calls) == 2
    assert client.calls[0]["params"]["p_limit"] == 50
    retry = client.calls[1]["params"]
    assert retry["p_limit"] == _MISS_RETRY_LIMIT
    assert retry["p_query_embedding"] is None
    assert out["chunks"] == ["Old doc chunk"]
    assert "note" not in out


def test_pull_source_query_mode_miss_widens_and_finds_doc():
    # The bug (2026-07-21): the RPC returns only the top `limit` chunks ACROSS
    # the whole scope, ranked by the query. A doc whose chunks rank BELOW that
    # window for this query (e.g. a short xlsx against many semantically-closer
    # PDFs) fell out and was reported "no source" — even though its title is in
    # the manifest. Resolving a doc BY TITLE must not depend on chunk ranking:
    # a query miss must widen once (keeping the query embedding) before giving up.
    xlsx_rows = [
        {
            "text": "Solution Area | Use Case | # Capabilities",
            "citation_url": "",
            "title": "Infoblox_Solutions_Framework_Use_Cases_Capabilities.xlsx",
            "scope": "project",
        }
    ]
    # First (narrow) read ranks the xlsx out of view; the widen surfaces it.
    client = _SequencedRpcClient([_CHUNK_ROWS, _CHUNK_ROWS + xlsx_rows])
    embedder = _FakeEmbedder([0.1, 0.2, 0.3])

    out = pull_source(
        client,
        "proj-1",
        "co-9",
        doc_title="Infoblox_Solutions_Framework_Use_Cases_Capabilities.xlsx",
        query="Executive View one-line business outcome per use case",
        embedder=embedder,
    )

    assert len(client.calls) == 2
    assert client.calls[0]["params"]["p_limit"] == 50
    retry = client.calls[1]["params"]
    assert retry["p_limit"] == _MISS_RETRY_LIMIT
    # The widen KEEPS the query embedding so chunks stay query-ranked.
    assert retry["p_query_embedding"] == [0.1, 0.2, 0.3]
    assert out["chunks"] == ["Solution Area | Use Case | # Capabilities"]
    assert "note" not in out


def test_pull_source_query_mode_genuine_miss_still_reports_after_widen():
    # A doc that truly isn't in the scope still returns the note — but only
    # after the widen has ruled it out (2 calls, not 1).
    client = _SequencedRpcClient([_CHUNK_ROWS, _CHUNK_ROWS])
    embedder = _FakeEmbedder([0.1, 0.2, 0.3])

    out = pull_source(
        client,
        "proj-1",
        "co-9",
        doc_title="Nonexistent Doc",
        query="anything",
        embedder=embedder,
    )

    assert len(client.calls) == 2
    assert "no source named 'Nonexistent Doc'" in out["note"]


def test_pull_source_no_query_sorts_by_chunk_index():
    # #152: the RPC's per-doc order used to be arbitrary (created_at ties for
    # every chunk of one doc). meta.chunk_index now records document order —
    # the no-query path must sort by it regardless of RPC return order.
    rows = [
        {"text": "third", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": 2, "page": None},
        {"text": "first", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": 0, "page": None},
        {"text": "second", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": 1, "page": None},
    ]
    out = pull_source(_FakeRpcClient(rows), "proj-1", "co-9", doc_title="Doc A")
    assert out["chunks"] == ["first", "second", "third"]


def test_pull_source_no_query_pre_stamp_rows_keep_rpc_order():
    # Rows ingested before chunk_index existed (no key at all) must keep the
    # RPC's order — the sort is stable and unkeyed rows all tie.
    rows = [
        {"text": "one", "title": "Doc A", "scope": "project", "citation_url": "u"},
        {"text": "two", "title": "Doc A", "scope": "project", "citation_url": "u"},
    ]
    out = pull_source(_FakeRpcClient(rows), "proj-1", "co-9", doc_title="Doc A")
    assert out["chunks"] == ["one", "two"]


def test_pull_source_no_query_page_orders_unstamped_pdf_rows():
    # Pre-stamp PDF ingests carry meta.page — used as the fallback ordinal.
    rows = [
        {"text": "p2", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": None, "page": 2},
        {"text": "p1", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": None, "page": 1},
    ]
    out = pull_source(_FakeRpcClient(rows), "proj-1", "co-9", doc_title="Doc A")
    assert out["chunks"] == ["p1", "p2"]


def test_pull_source_query_mode_keeps_relevance_order():
    # A query ranks by relevance — the doc-order sort must NOT apply.
    rows = [
        {"text": "best hit", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": 5, "page": None},
        {"text": "next hit", "title": "Doc A", "scope": "project",
         "citation_url": "u", "chunk_index": 0, "page": None},
    ]
    out = pull_source(
        _FakeRpcClient(rows), "proj-1", "co-9", doc_title="Doc A",
        query="anything", embedder=_FakeEmbedder([0.1]),
    )
    assert out["chunks"] == ["best hit", "next hit"]


# ──────────────────────────────────────────────────────────────────────
#  resolve_element_versions — the write-path element resolver
# ──────────────────────────────────────────────────────────────────────


class _FilterQuery:
    """A select query that APPLIES its .eq() filters (unlike _FakeTableQuery,
    which only records them) — needed to prove project_id scoping."""

    def __init__(self, rows):
        self._rows = rows
        self._filters: list[tuple[str, object]] = []

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def execute(self):
        data = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._filters)
        ]
        return _FakeExecute(data)


class _FilterClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FilterQuery(self._rows)


def _sub_row(**over):
    row = {
        "id": "row-1",
        "est_item_id": "_authored/hyp",
        "project_id": "pid-1",
        "project_code": "ibx-5153-ai-campaign",
        "status": "live",
        "version_label": "v1",
        "framing": "Latest hypothesis",
        "body": "b",
        "sources": [],
        "origin": "authored",
    }
    row.update(over)
    return row


def test_resolve_element_versions_by_est_item_id():
    from cp_engine.project_sources import resolve_element_versions

    client = _FilterClient([
        _sub_row(id="v1", version_label="v1", status="superseded"),
        _sub_row(id="v2", version_label="v2", status="live"),
        # A different project's element with the same slug must NOT leak in.
        _sub_row(id="other", project_id="pid-OTHER"),
    ])
    eid, versions = resolve_element_versions(
        client, "pid-1", "_authored/hyp", columns="id, est_item_id, status",
    )
    assert eid == "_authored/hyp"
    assert {v["id"] for v in versions} == {"v1", "v2"}  # project-scoped, all statuses


def test_resolve_element_versions_by_framing_substring():
    from cp_engine.project_sources import resolve_element_versions

    client = _FilterClient([_sub_row(id="v1")])
    eid, versions = resolve_element_versions(
        client, "pid-1", "latest hypothesis", columns="id, est_item_id, status, framing",
    )
    assert eid == "_authored/hyp"
    assert len(versions) == 1


def test_resolve_element_versions_no_match():
    from cp_engine.project_sources import resolve_element_versions

    client = _FilterClient([_sub_row()])
    eid, versions = resolve_element_versions(
        client, "pid-1", "_authored/nope", columns="id, est_item_id, status",
    )
    assert eid is None
    assert versions == []


def test_resolve_element_versions_scopes_by_project_id():
    """The whole point: an element resolves under its project_id even though its
    stored project_code slug differs from any short code a caller might type."""
    from cp_engine.project_sources import resolve_element_versions

    client = _FilterClient([_sub_row(project_code="ibx-5153-ai-campaign")])
    # Resolver was handed the project's UUID (pid-1), not a code string.
    eid, versions = resolve_element_versions(
        client, "pid-1", "_authored/hyp", columns="id, est_item_id, status, project_code",
    )
    assert eid == "_authored/hyp"
    assert versions[0]["project_code"] == "ibx-5153-ai-campaign"
