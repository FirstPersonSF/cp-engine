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
import os
from pathlib import Path

from cp_engine.asset_ingest import FileRef, download_file, read_scoped_chunks
from cp_engine.spine_done import derive_done, fetch_project_done_map

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
#  Read the SPINE — a project's authored/distilled elements
# ──────────────────────────────────────────────────────────────────────
#
# The spine (`spine_substance`) holds a project's distilled memory: authored
# deliverables, ingested-and-distilled emails/sources, decisions, etc. — keyed
# on `project_id`. These two pure functions are the read counterpart to the MCP
# write tools (`create_spine_element` / `add_spine_version`): `list_spine` is the
# "what's in the spine" index, `pull_spine` returns one element's full body.
# Both read only LIVE versions (superseded history is excluded). As with the
# source readers, we never `select *` and never carry the (large) body into the
# list — only a body LENGTH.

# List columns: metadata + body (so we can derive body_len) but never `*`. The
# body is selected only to compute its length; it is NOT returned in the list.
_SPINE_LIST_COLUMNS = (
    "est_item_id, framing, layer, binding, status, serves, body, important, note"
)

# Pull columns: everything needed to return one element with full body + context.
_SPINE_PULL_COLUMNS = (
    "est_item_id, framing, layer, binding, status, serves, sources, "
    "version_label, body, important, note"
)

# Resolve columns: enough to identify ONE live element AND its row `id` (needed
# for a targeted update). Includes `id` (which _spine_element does NOT surface,
# so pull_spine's public return is unaffected). Never `*`.
_SPINE_RESOLVE_COLUMNS = (
    "id, est_item_id, framing, status, important, note"
)


def _match_one_live(rows: list[dict], key: str):
    """Resolve `key` against already-fetched live rows to ONE element.

    The single source of truth for spine `key` resolution (shared by
    `pull_spine` and `resolve_live_element`):

      1. Exact `est_item_id` match → that row.
      2. Title (`framing`) substring (case-insensitive, `_title_matches`):
         exactly one distinct element matched → that row.

    Returns `(row, reason, distinct)`:
      - unique resolution → `(row, None, None)`,
      - no title match     → `(None, "no-match", None)`,
      - 2+ distinct        → `(None, "ambiguous", distinct)` where `distinct` is
        the already-computed est_item_id→row map, so the caller can render
        candidate names WITHOUT re-filtering `rows` (keeps the dedup in one place).
    """
    exact = [r for r in rows if r.get("est_item_id") == key]
    if exact:
        return exact[0], None, None
    matched = [r for r in rows if _title_matches(key, r.get("framing"))]
    if not matched:
        return None, "no-match", None
    distinct = {r.get("est_item_id"): r for r in matched}
    if len(distinct) > 1:
        return None, "ambiguous", distinct
    return next(iter(distinct.values())), None, None


def resolve_live_element(client, project_id: str, key: str) -> dict | None:
    """Resolve `key` to the single matching LIVE spine_substance row, or None.

    Same discipline as `pull_spine` (exact est_item_id → single distinct framing
    substring → else None on no-match OR ambiguity). Returns the raw row dict
    INCLUDING its `id` column (needed for a targeted update on a callsite that
    wants to write the row back). Never `SELECT *`.
    """
    resp = (
        client.table("spine_substance")
        .select(_SPINE_RESOLVE_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "live")
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    row, _reason, _ = _match_one_live(rows, key)
    return row


def list_spine(client, project_id: str) -> list[dict]:
    """List a project's LIVE spine elements (index, not bodies).

    Returns `[{est_item_id, framing, layer, binding, status, serves_count,
    body_len, important, note, done}]` for every live element of the project.
    `important`/`note` are the element's importance flag + annotation; `done` is
    true/false/null (null = n/a, i.e. not bound to a real work-item). The full
    body is never returned here (only its length) — call `pull_spine` for one
    element's text. Never raises: the MCP tool boundary converts failures to a
    structured note.
    """
    resp = (
        client.table("spine_substance")
        .select(_SPINE_LIST_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "live")
        .order("layer", desc=False)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    # Fetch the project's done-map ONCE (not per row — no N+1). `done` is
    # best-effort: if the estimator schema is unreachable the fetch may raise,
    # so we fail-soft to an empty map, which makes `derive_done` return None for
    # every element. Never let a `done` lookup break the listing.
    try:
        done_map = fetch_project_done_map(client, project_id)
    except Exception:  # noqa: BLE001 — done is best-effort; never break the listing
        logger.warning(
            "list_spine: done-map fetch failed for project_id=%s; "
            "degrading `done` to None for all elements",
            project_id,
            exc_info=True,
        )
        done_map = {}
    out: list[dict] = []
    for row in rows:
        serves = row.get("serves") or []
        body = row.get("body") or ""
        out.append(
            {
                "est_item_id": row.get("est_item_id"),
                "framing": row.get("framing"),
                "layer": row.get("layer"),
                "binding": row.get("binding"),
                "status": row.get("status"),
                "serves_count": len(serves),
                "body_len": len(body),
                "important": bool(row.get("important")),
                "note": row.get("note"),
                "done": derive_done(row.get("est_item_id"), done_map),
            }
        )
    # Important elements sort first; list.sort is stable so within-group order
    # (the query's existing layer ordering) is preserved.
    out.sort(key=lambda r: not r["important"])
    return out


def pull_spine(client, project_id: str, key: str) -> dict:
    """Pull ONE live spine element's full body + context by id or title.

    `key` resolves to a single element, mirroring `pull_source`'s discipline of
    never merging distinct elements:

      1. Exact `est_item_id` match (the machine path: `list_spine` returns these).
      2. Title (`framing`) substring, case-insensitive (`_title_matches`):
         - exactly one distinct element matched → return it,
         - 2+ distinct elements matched → `{body: "", error: "ambiguous: ..."}`.
      3. No match → `{body: "", error: "no spine element ..."}`.

    Returns `{est_item_id, framing, layer, binding, status, serves, sources,
    version_label, body, important, note}` on success (`note` is the element's
    importance annotation). Failure paths return an `error` key. Never raises.
    """
    resp = (
        client.table("spine_substance")
        .select(_SPINE_PULL_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "live")
        .execute()
    )
    rows = getattr(resp, "data", None) or []

    row, reason, distinct = _match_one_live(rows, key)
    if row is not None:
        return _spine_element(row)
    if reason == "no-match":
        return {
            "body": "",
            "error": f"no spine element matching '{key}' in this project",
        }
    # ambiguous: 2+ distinct elements matched the title substring. Reuse the
    # already-computed `distinct` from _match_one_live — do NOT re-filter rows.
    candidates = ", ".join(
        (r.get("framing") or r.get("est_item_id") or "?") for r in distinct.values()
    )
    return {
        "body": "",
        "error": (
            f"ambiguous: '{key}' matched {len(distinct)} elements: "
            f"[{candidates}]; pass an est_item_id or a more specific title"
        ),
    }


def _spine_element(row: dict) -> dict:
    """Shape one spine_substance row into the pull_spine return contract."""
    return {
        "est_item_id": row.get("est_item_id"),
        "framing": row.get("framing"),
        "layer": row.get("layer"),
        "binding": row.get("binding"),
        "status": row.get("status"),
        "serves": row.get("serves") or [],
        "sources": row.get("sources") or [],
        "version_label": row.get("version_label"),
        "body": row.get("body") or "",
        "important": bool(row.get("important")),
        "note": row.get("note"),
    }


# ──────────────────────────────────────────────────────────────────────
#  Re-fetch the ORIGINAL binary via persisted coords
# ──────────────────────────────────────────────────────────────────────

# Re-fetch coords persisted on `rag_assets` (Task 3) + display fields. NEVER `*`
# / `meta` (large blobs) — only what's needed to reconstruct a FileRef and cite.
_REFETCH_COLUMNS = "title, source_provider, source_file_id, source_path, url"


def fetch_source(client, project_id: str, doc_title: str, dest_dir) -> dict:
    """Download an ingested source's ORIGINAL binary to `dest_dir`.

    Looks up the asset's persisted re-fetch coords (`source_provider`,
    `source_file_id`, `source_path` — stamped on ingest), reconstructs a
    `FileRef`, and reuses `asset_ingest.download_file` to fetch the original from
    Drive/Dropbox. Title resolution mirrors `pull_source`: case-insensitive
    contains-or-equal match (`_title_matches`), with exact-title preferred over a
    substring match.

    Returns `{local_path, title, provider, url}` on success, or a structured
    `{error}` on any failure (no match, missing coords on a pre-live-link row,
    lookup error, or download error). NEVER raises — this is called by an MCP
    tool, which must surface a message rather than crash.
    """
    try:
        rows = (
            client.table("rag_assets")
            .select(_REFETCH_COLUMNS)
            .eq("project_id", project_id)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 — MCP tool boundary, never raise
        return {"error": f"lookup failed: {exc}"}

    target = doc_title.strip().lower()
    matched = [r for r in rows if _title_matches(doc_title, r.get("title"))]
    exact = [r for r in matched if (r.get("title") or "").lower() == target]
    selected = exact or matched
    if not selected:
        return {"error": f"no source titled {doc_title!r} in project"}
    row = selected[0]

    if not row.get("source_provider") or not row.get("source_file_id"):
        return {
            "error": f"{row.get('title')!r} has no re-fetch coords "
            f"(ingested before the live-link layer; re-ingest to enable)"
        }

    file_ref = FileRef(
        source=row["source_provider"],
        id=row["source_file_id"],
        name=row.get("title") or "source",
        mime_type=None,
        size=None,
        modified=None,
        path=row.get("source_path"),
    )
    try:
        local = download_file(file_ref, Path(dest_dir))
    except Exception as exc:  # noqa: BLE001 — MCP tool boundary, never raise
        return {"error": f"download failed: {exc}"}

    return {
        "local_path": str(local),
        "title": row.get("title"),
        "provider": row["source_provider"],
        "url": row.get("url"),
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


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp file + `os.replace`).

    Avoids torn/truncated files if the process is interrupted mid-write: write
    to a sibling `.tmp`, then `os.replace` (atomic on POSIX) swaps it in. Used
    for both the manifest and the (gitignored, local-only) cache.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _render_manifest(assets: list[dict]) -> str:
    """Render the `_sources.md` body — frontmatter header + one line per doc.

    The WHOLE file is engine-owned and fully regenerated every sync (the
    `Generated by cp-engine` header says so). No `cp-engine:start/end` region
    markers: those imply human-editable territory outside the region, but the
    code overwrites the entire file, so any hand edit would be lost. The markers
    promised a contract the code doesn't honor — so we don't show them.
    """
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
    body = region + "\n" if region else ""
    return header + body


def write_sources_manifest(
    client,
    project_dir,
    project_id: str,
    company_id: str,
    llm=None,
    today=None,
    max_new_summaries: int = 25,
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

    `max_new_summaries` caps how many FRESH summaries one call makes (cache hits
    are always free). A cold N-asset project fills over ~ceil(N/cap) syncs, 25 at
    a time, instead of N serial Claude calls in one — a gentle cold-start.

    Failure / deferral semantics (both rely on the same uncached-retry mechanic):
      - A summary that FAILS (`_SUMMARY_UNAVAILABLE`) is shown in the manifest
        but NOT cached, so the next sync retries it (no cache entry → miss).
      - A cache MISS beyond `max_new_summaries` is deferred: shown as
        `(summary unavailable)` and NOT cached, so the next sync picks it up.
    Caching the sentinel would poison the doc forever (hash unchanged → cache
    HIT → never re-summarized even after the API recovers).
    """
    del today  # reserved; manifest header carries no date today
    if llm is None:
        llm = _default_llm

    project_dir = Path(project_dir)
    assets = list_sources(client, project_id, company_id)

    # The cache is pure local memoization (gitignored, NOT committed — see the
    # cp tenant's .gitignore). Tolerate a corrupt/unreadable cache by ignoring it.
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
    new_summaries = 0
    for asset in assets:
        asset_id = asset.get("id")
        current_hash = _doc_hash(asset)
        cached = cache.get(asset_id)
        if cached and cached.get("hash") == current_hash:
            # Cache HIT — always free, never counts against the cap.
            summary = cached.get("summary") or _SUMMARY_UNAVAILABLE
            new_cache[asset_id] = {"hash": current_hash, "summary": summary}
        elif new_summaries >= max_new_summaries:
            # Cache MISS but over this sync's cap → defer. Show unavailable, do
            # NOT cache → next sync re-tries it (uncached → miss).
            summary = _SUMMARY_UNAVAILABLE
        else:
            new_summaries += 1
            summary = _summarize_doc(
                client, project_id, company_id, asset.get("title"), llm
            )
            # I1: only persist a SUCCESSFUL summary. A failed summary is shown
            # this run but left uncached so the next sync retries it.
            if summary != _SUMMARY_UNAVAILABLE:
                new_cache[asset_id] = {"hash": current_hash, "summary": summary}
        asset["summary"] = summary

    # `new_cache` only holds current, successfully-summarized asset ids → removed
    # and failed/deferred assets are absent (pruned / left for retry).
    project_dir.mkdir(parents=True, exist_ok=True)
    # Atomic temp+rename writes so an interrupt can't leave a torn manifest or
    # cache. The cache is gitignored / local-only memoization.
    _atomic_write(project_dir / _MANIFEST_FILENAME, _render_manifest(assets))
    _atomic_write(
        cache_path, json.dumps(new_cache, indent=2, sort_keys=True)
    )
    return len(assets)
