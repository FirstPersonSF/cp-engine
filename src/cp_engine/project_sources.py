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

import json
import logging
from pathlib import Path

from cp_engine.asset_ingest import read_scoped_chunks

logger = logging.getLogger(__name__)

# Explicit display columns for the manifest list — NEVER `meta` / `file_path` /
# `*` (those carry large blobs and aren't needed to list a source).
# `file_hash` is the cache key for the manifest's per-doc summaries: it changes
# when the doc's content changes, so an unchanged doc keeps its cached summary
# and skips the LLM call. It's a short scalar, not a blob, so it's safe to list.
_SOURCE_COLUMNS = "id, title, source_type, status, created_at, file_hash"


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
            "file_hash": row.get("file_hash"),
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


# ──────────────────────────────────────────────────────────────────────
#  Manifest generator — `_sources.md` with cached per-doc summaries
# ──────────────────────────────────────────────────────────────────────

_SUMMARY_UNAVAILABLE = "(summary unavailable)"
_CACHE_FILENAME = "_sources.cache.json"
_MANIFEST_FILENAME = "_sources.md"


def _doc_hash(asset: dict) -> str:
    """Stable cache key for one asset.

    Prefer the content `file_hash` (changes when the doc content changes — the
    right key, so a re-uploaded but identical doc is a cache HIT). Fall back to
    `<id>:<created_at>` when no file_hash is present (older rows / fakes).
    """
    file_hash = asset.get("file_hash")
    if file_hash:
        return str(file_hash)
    return f"{asset.get('id')}:{asset.get('created_at')}"


def _summarize_doc(client, project_id: str, company_id: str, title: str, llm) -> str:
    """One short LLM summary of a doc, from a sample of its chunks.

    Best-effort: any failure (no chunks, no API key, LLM error) returns the
    `(summary unavailable)` sentinel rather than raising — the caller must never
    abort the whole manifest over one doc.
    """
    try:
        pulled = pull_source(
            client, project_id, company_id, doc_title=title, limit=6
        )
        chunks = [c for c in (pulled.get("chunks") or []) if c]
        if not chunks:
            return _SUMMARY_UNAVAILABLE
        excerpt = "\n".join(chunks)[:2000]
        prompt = (
            "In 1-2 sentences, say what this document is and what it contains. "
            "Be concrete. "
            f"Document title: {title}. Excerpt:\n{excerpt}"
        )
        summary = (llm(prompt) or "").strip()
        return summary or _SUMMARY_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 — best-effort per doc
        logger.warning(
            "source summary failed for %r: %s", title, exc, exc_info=True
        )
        return _SUMMARY_UNAVAILABLE


def _default_llm(prompt: str) -> str:
    """Default summary LLM: one Anthropic call via plan_from_transcript._call_claude.

    Model pinned to `claude-opus-4-7` (do NOT bump); api_key=None reads
    ANTHROPIC_API_KEY from the environment.
    """
    from cp_engine.plan_from_transcript import _call_claude

    return _call_claude(prompt, model="claude-opus-4-7", api_key=None)


def _render_manifest(assets: list[dict]) -> str:
    """Render the `_sources.md` body — frontmatter + engine-managed region."""
    lines = [
        f"- **{a.get('title')}** · {a.get('source_type')} — {a.get('summary')}"
        for a in assets
    ]
    region = "\n".join(lines)
    header = (
        "---\n"
        f"Project sources — {len(assets)} ingested document(s)\n"
        "Generated by cp-engine. Pull a source's content with the "
        "pull_project_source MCP tool.\n"
        "---\n"
    )
    body = "<!-- cp-engine:start sources -->\n"
    if region:
        body += region + "\n"
    body += "<!-- cp-engine:end sources -->\n"
    return header + body


def write_sources_manifest(
    client,
    project_dir,
    project_id: str,
    company_id: str,
    llm=None,
    today=None,
) -> int:
    """Write `<project_dir>/_sources.md` — one entry per active source doc.

    Each entry carries a short LLM summary cached per-doc in
    `<project_dir>/_sources.cache.json` keyed by a content hash (`file_hash`),
    so unchanged docs are NEVER re-summarized across runs. The manifest is fully
    regenerated from the current `list_sources`, so removed assets drop out; we
    prune their cache entries too. Returns the number of assets written.

    `llm` is an injected `(prompt: str) -> str` callable (for tests); the default
    is one Anthropic call (`claude-opus-4-7`). `today` is accepted for signature
    symmetry / future dated headers and is currently unused.
    """
    del today  # reserved; manifest header carries no date today
    if llm is None:
        llm = _default_llm

    project_dir = Path(project_dir)
    assets = list_sources(client, project_id, company_id)

    cache_path = project_dir / _CACHE_FILENAME
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except (json.JSONDecodeError, OSError):
            cache = {}  # tolerate corrupt/unreadable cache → re-summarize

    new_cache: dict[str, dict] = {}
    for asset in assets:
        asset_id = asset.get("id")
        current_hash = _doc_hash(asset)
        cached = cache.get(asset_id)
        if cached and cached.get("hash") == current_hash:
            summary = cached.get("summary") or _SUMMARY_UNAVAILABLE
        else:
            summary = _summarize_doc(
                client, project_id, company_id, asset.get("title"), llm
            )
        asset["summary"] = summary
        # Only keep cache keys for current asset ids → removed assets pruned.
        new_cache[asset_id] = {"hash": current_hash, "summary": summary}

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / _MANIFEST_FILENAME).write_text(
        _render_manifest(assets), encoding="utf-8"
    )
    cache_path.write_text(
        json.dumps(new_cache, indent=2, sort_keys=True), encoding="utf-8"
    )
    return len(assets)
