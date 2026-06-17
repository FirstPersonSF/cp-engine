"""Pure retrieval over a project's ingested source documents.

These two functions are the transport-agnostic core of the
ambient-project-sources feature: the manifest generator AND the MCP tools both
call them. They take only `(client, ids) -> data` — no MCP / CLI / FastAPI
imports, no file I/O, no global state — so they stay reusable across every
surface that wants to read a project's sources.

The readable text lives in Supabase (`rag_assets` + `asset_chunks`); the scoped
read is fronted by the `read_scoped_asset_chunks` RPC, wrapped here via
`read_scoped_chunks` (see `asset_ingest.read_scoped_chunks`). We never select
`meta` or `file_path` (large/irrelevant columns) — only explicit display fields.
"""

from __future__ import annotations

from cp_engine.asset_ingest import read_scoped_chunks

# Explicit display columns for the manifest list — NEVER `meta` / `file_path` /
# `*` (those carry large blobs and aren't needed to list a source).
_SOURCE_COLUMNS = "id, title, source_type, status, created_at"


def list_sources(
    client,
    project_id: str,
    company_id: str,
    summaries: dict[str, str] | None = None,
) -> list[dict]:
    """List a project's OWN active source documents, newest first.

    This is the manifest list: the project's own `rag_assets` rows (project-
    scoped). Account-scoped shared docs are NOT listed here — they surface
    through `pull_source`'s RPC, which already unions project + account scope.
    Keeping the list to the project's own rows is the simplest correct thing for
    a per-project manifest.

    `company_id` is accepted for signature symmetry with `pull_source` (and so
    callers can pass the same ids to both) but isn't needed for the project-
    scoped list.

    If `summaries` (a `{asset_id: summary_str}` dict) is passed, each row's
    cached summary is merged in under `summary`; otherwise `summary` is omitted.
    The manifest generator passes cached summaries; the MCP list tool passes
    none.

    Returns `[{id, title, source_type, created_at[, summary]}]`, newest first.
    """
    resp = (
        client.table("rag_assets")
        .select(_SOURCE_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    out: list[dict] = []
    for row in rows:
        entry = {
            "id": row.get("id"),
            "title": row.get("title"),
            "source_type": row.get("source_type"),
            "created_at": row.get("created_at"),
        }
        if summaries is not None:
            summary = summaries.get(row.get("id"))
            if summary is not None:
                entry["summary"] = summary
        out.append(entry)
    return out


def _title_matches(doc_title: str, row_title: str | None) -> bool:
    """Case-insensitive contains-or-equal match.

    A row matches when `doc_title.lower()` is a substring of (or exactly equals)
    `row_title.lower()`. So "Concur Storybook" finds a row titled
    "WW Internal Storybook- Concur..." only when the query is a substring of the
    stored title; the substring direction is query⊆stored. Exact-equal is the
    degenerate substring case. A row with no title never matches.
    """
    if not row_title:
        return False
    return doc_title.lower() in row_title.lower()


def pull_source(
    client,
    project_id: str,
    company_id: str,
    doc_title: str,
    query: str | None = None,
    limit: int = 50,
    embedder=None,
) -> dict:
    """Pull the chunk text of one named source document.

    Reads the project's scoped chunks (its own project-scoped assets + its
    company's account-scoped assets) via the `read_scoped_asset_chunks` RPC, then
    filters to the rows whose `title` matches `doc_title` (case-insensitive
    contains-or-equal — see `_title_matches`).

    `query` handling:
      - `query=None` (the primary path): chunks come back in recency order, all
        of the matched doc's chunks up to `limit`. `query_embedding=None` is
        passed to the RPC.
      - `query!=None`: the query string is embedded with Voyage
        (`voyage-3-large`) and passed as the RPC's `query_embedding`, so chunks
        come back vector-ranked by similarity. The embedder is constructed
        lazily (runtime-only import, keeping this module transport-agnostic) and
        can be injected via `embedder` for tests.

    Resolution to ONE document (never merge distinct docs):

      1. Exact-title (the manifest's machine path): if any matched row's title
         case-insensitively EQUALS `doc_title`, return ONLY that doc's chunks.
         The manifest names docs by full title, so the MCP pull passes the exact
         title — this is the common, correct, cheap path.
      2. Single distinct title: exactly one document's title matched the
         substring → return that doc's chunks.
      3. Ambiguous (2+ distinct titles match, none exact): do NOT merge —
         distinct titles must never collapse under one citation (provenance
         corruption). Return `{title: doc_title, chunks: [], note: "ambiguous:
         ... matched N sources: [...]"}` listing the candidate titles.
      4. No match: `{title: doc_title, chunks: [], note: "no source named ..."}`.

    For 1 and 2, title / citation_url / scope come from the first surviving row
    (all surviving rows are the same document), and `chunks` is every surviving
    row's text in returned order.
    """
    query_embedding = None
    if query is not None:
        if embedder is None:
            # Runtime-only import: keeps this module free of the heavy Voyage
            # dependency at import time and free of any transport coupling.
            from ingest.embedding_service import IngestEmbeddingService

            embedder = IngestEmbeddingService(model="voyage-3-large")
        query_embedding = embedder.embed(query)

    rows = read_scoped_chunks(
        client,
        project_id,
        company_id,
        query_embedding=query_embedding,
        limit=limit,
    )

    matched = [r for r in rows if _title_matches(doc_title, r.get("title"))]
    if not matched:
        return {
            "title": doc_title,
            "chunks": [],
            "note": f"no source named '{doc_title}' found in this project's assets",
        }

    # Resolve to a SINGLE document. Group matched rows by their (case-
    # insensitive) title while preserving each title's original casing for
    # output and the first-seen order for the ambiguity note.
    distinct: dict[str, str] = {}
    for r in matched:
        title = r.get("title") or ""
        key = title.lower()
        if key not in distinct:
            distinct[key] = title

    target = doc_title.lower()
    if target in distinct:
        # 1. Exact-title preference (the manifest's machine path).
        selected = [r for r in matched if (r.get("title") or "").lower() == target]
    elif len(distinct) == 1:
        # 2. Single distinct title matched — that one doc is unambiguous.
        selected = matched
    else:
        # 3. Ambiguous: 2+ distinct titles, none exact. Never merge.
        candidates = ", ".join(distinct.values())
        return {
            "title": doc_title,
            "chunks": [],
            "note": (
                f"ambiguous: '{doc_title}' matched {len(distinct)} sources: "
                f"[{candidates}]; pass a more specific title"
            ),
        }

    first = selected[0]
    return {
        "title": first.get("title"),
        "citation_url": first.get("citation_url"),
        "scope": first.get("scope"),
        "chunks": [r.get("text") for r in selected],
    }
