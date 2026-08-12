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
import re
from pathlib import Path

from cp_engine.asset_ingest import FileRef, download_file, read_scoped_chunks
from cp_engine.spine_done import derive_done, fetch_project_done_map
from cp_engine import mc2_db
from cp_engine.mc2_db import RagAssetRow, Tables

logger = logging.getLogger(__name__)

# Explicit display columns for the manifest list — NEVER `meta` / `file_path` /
# `*` (those carry large blobs and aren't needed to list a source).
# `file_hash` is the cache key for the manifest's per-doc summaries: it changes
# when the doc's content changes, so an unchanged doc keeps its cached summary
# and skips the LLM call. It's a short scalar, not a blob, so it's safe to list.
_SOURCE_COLUMNS = mc2_db.RAG_ASSET_LIST_COLUMNS

# Widened window for the no-query pull's second attempt. The recency-ordered
# read is scoped to the WHOLE project+account chunk pool, so an older doc can
# sit entirely below the caller's `limit`. 2000 chunks comfortably covers a
# project's full corpus while still bounding the transfer.
_MISS_RETRY_LIMIT = 2000


def drop_superseded_assets(rows: list[dict]) -> list[dict]:
    """Drop assets that have a SUCCESSOR in `rows` (issue #57's read guard).

    A successor is another row whose `prev_asset_id` points at this row's `id`
    — the chain a same-title re-ingest (or the pipeline's own same-path
    new_version) writes. Superseded predecessors normally also flip to
    `status='superseded'` and never reach here; this guard covers the residue
    where both ends of a chain are still active (pre-cleanup backlog, or a
    supersede that half-landed), so `list_sources` / the `_sources.md`
    manifest never show both copies of one document.

    Shared helper on purpose: every list surface (MCP `list_project_sources`,
    the manifest generator) flows through `list_sources`, which applies this.
    Order is preserved.
    """
    predecessor_ids = {
        r.get("prev_asset_id") for r in rows if r.get("prev_asset_id")
    }
    if not predecessor_ids:
        return rows
    return [r for r in rows if r.get("id") not in predecessor_ids]


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

    Assets with a SUCCESSOR (another asset's `prev_asset_id` points at them)
    are excluded via `drop_superseded_assets` — see that helper. `pull_source`
    gets the equivalent guarantee from the `read_scoped_asset_chunks` RPC's
    `status='active'` filter (superseded predecessors are status-flipped and
    their chunks deleted at supersede time).

    Returns `[{id, title, source_type, created_at[, summary]}]`, newest first.
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .select(_SOURCE_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .execute()
    )
    rows = drop_superseded_assets(getattr(resp, "data", None) or [])
    out: list[dict] = []
    for raw in rows:
        row = RagAssetRow.from_row(raw)
        entry = {
            "id": row.id,
            "title": row.title,
            "source_type": row.source_type,
            "created_at": row.created_at,
            "file_hash": row.file_hash,
        }
        if summaries is not None:
            summary = summaries.get(row.id)
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

            from cp_engine.asset_ingest import _configure_pipeline_once

            # The embedder reads voyage_api_key off document-ingest's settings
            # singleton. Unconfigured, that singleton is DefaultIngestSettings,
            # which has NO voyage_api_key field — the key is invisible even when
            # VOYAGE_API_KEY is in the environment. Wire AssetIngestSettings
            # (env-backed) in first, exactly as the ingest pipeline does.
            _configure_pipeline_once()

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
    if not matched and limit < _MISS_RETRY_LIMIT:
        # The scoped read returns only the top `limit` chunks ACROSS the whole
        # scope, then we title-filter here — so a doc whose chunks fall outside
        # that window is invisible even though it EXISTS. This bites two ways:
        #   - query=None: recency-ordered, so an OLD doc drops below the window.
        #   - query!=None: vector-ranked, so a doc whose chunks rank below the
        #     top `limit` for THIS query (common for a short xlsx against many
        #     semantically-closer PDFs) drops out — reported as "no source" even
        #     though the title is right there in the manifest (the bug: resolving
        #     a doc BY TITLE must never depend on how its chunks rank).
        # Before declaring "no source", widen once to a window big enough to
        # cover every doc a project realistically holds. Keep the query_embedding
        # so a successful widen still returns the query-ranked chunks.
        rows = read_scoped_chunks(
            client,
            project_id,
            company_id,
            query_embedding=query_embedding,
            limit=_MISS_RETRY_LIMIT,
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

    # Document order on the no-query path (#152): the RPC returns
    # meta.chunk_index (stamped at ingest since 1p-lib 6a0f2db) and meta.page;
    # pre-stamp rows have neither and keep their RPC order (stable sort).
    # A query ranks by relevance — leave that order alone.
    if query is None:
        def _doc_order(r: dict) -> tuple:
            ci, page = r.get("chunk_index"), r.get("page")
            return (
                0 if ci is not None else 1, ci if ci is not None else 0,
                0 if page is not None else 1, page if page is not None else 0,
            )
        selected = sorted(selected, key=_doc_order)

    first = selected[0]
    return {
        "title": first.get("title"),
        "citation_url": first.get("citation_url"),
        "scope": first.get("scope"),
        # Cap at the caller's `limit` so the widened-window retry can't return
        # more chunks than the primary path ever would.
        "chunks": [r.get("text") for r in selected[:limit]],
    }


# ──────────────────────────────────────────────────────────────────────
#  Curate the SOURCE STORE — archive / rename ingested assets (#126)
# ──────────────────────────────────────────────────────────────────────


def _resolve_source_asset(client, owner_id: str, key: str):
    """Resolve `key` (asset uuid or EXACT title) to ONE active asset.

    Owner-scoped across both owner columns (engagement `project_id` OR
    initiative `initiative_id` — the caller's resolved id lives in exactly
    one). Returns the asset row, or `{"candidates": [...]}` when an exact
    title matches several rows (recurring recordings share titles — the
    caller picks an id), or None when nothing matches.
    """
    from cp_engine.mc2_db import Tables

    query = (
        client.table(Tables.RAG_ASSETS)
        .select("id, title, source_type, status, created_at")
        .or_(f"project_id.eq.{owner_id},initiative_id.eq.{owner_id}")
        .eq("status", "active")
    )
    import re as _re

    if _re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", key.lower()):
        rows = query.eq("id", key).limit(2).execute().data or []
    else:
        rows = query.eq("title", key).order("created_at", desc=True).execute().data or []
    if not rows:
        return None
    if len(rows) > 1:
        return {
            "candidates": [
                {"id": r["id"], "title": r["title"], "created_at": r["created_at"]}
                for r in rows
            ]
        }
    return rows[0]


def archive_source(client, owner_id: str, key: str) -> dict:
    """Archive one ingested source: status active → 'archived' (#126).

    Same soft-delete semantics as mc-2's archive route (mig 115): the row,
    its chunks, and spine provenance survive; the doc leaves every active
    read (list, pull, retrieval RPC). Durable against re-ingest since the
    dedup guard respects archived rows (document-ingest 491fa2f).
    """
    resolved = _resolve_source_asset(client, owner_id, key)
    if resolved is None:
        return {"error": f"no active source matching '{key}' for this project"}
    if "candidates" in resolved:
        return {
            "note": f"'{key}' matches {len(resolved['candidates'])} active "
            "sources — pass an id",
            **resolved,
        }
    from cp_engine.mc2_db import Tables

    client.table(Tables.RAG_ASSETS).update({"status": "archived"}).eq(
        "id", resolved["id"]
    ).eq("status", "active").execute()
    return {"archived": True, "id": resolved["id"], "title": resolved["title"]}


def rename_source(client, owner_id: str, key: str, new_title: str) -> dict:
    """Retitle one ingested source (#126).

    The tool for same-title DISTINCT documents (recurring recordings) that
    predate ingest-time date-suffixing — retitle the older copy instead of
    archiving real content. Readers resolve by title, so the new title is
    live immediately; `_sources.md` follows on next sync.
    """
    new_title = (new_title or "").strip()
    if not new_title:
        return {"error": "new_title must be non-empty"}
    resolved = _resolve_source_asset(client, owner_id, key)
    if resolved is None:
        return {"error": f"no active source matching '{key}' for this project"}
    if "candidates" in resolved:
        return {
            "note": f"'{key}' matches {len(resolved['candidates'])} active "
            "sources — pass an id",
            **resolved,
        }
    from cp_engine.mc2_db import Tables

    client.table(Tables.RAG_ASSETS).update({"title": new_title}).eq(
        "id", resolved["id"]
    ).execute()
    return {
        "renamed": True,
        "id": resolved["id"],
        "old_title": resolved["title"],
        "new_title": new_title,
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
_SPINE_LIST_COLUMNS = mc2_db.SPINE_LIST_COLUMNS

# Pull columns: everything needed to return one element with full body + context.
_SPINE_PULL_COLUMNS = mc2_db.SPINE_PULL_COLUMNS

# Resolve columns: enough to identify ONE live element AND its row `id` (needed
# for a targeted update). Includes `id` (which _spine_element does NOT surface,
# so pull_spine's public return is unaffected). `rel_path` is here for transcript
# promotion (item 3) — no in-repo caller reads it yet, so don't trim it as unused.
# Never `*`.
_SPINE_RESOLVE_COLUMNS = mc2_db.SPINE_RESOLVE_COLUMNS


def _unarchived(rows: list[dict]) -> list[dict]:
    """Drop archived rows from a spine_substance fetch.

    An element is retired by archiving it (retire_spine_element, the dashboard,
    or a manual repair), and a retired element must be invisible to every read
    path — list, pull, and both resolvers — or a stale title keeps matching
    keys and an archived element resurfaces in listings (#47). `archived` may
    be NULL on pre-column rows, so test falsiness, not equality with False.
    """
    return [r for r in rows if not r.get("archived")]


def _element_key(r: dict) -> tuple:
    """Identity key for ONE spine element within a scoped fetch.

    `est_item_id` is only unique PER PROJECT (authored ids are
    `_authored/<label-slug>`), so an account-scoped row must also carry its
    ORIGIN `project_id` in the key — two sibling projects can each promote
    `_authored/janet-dossier` and both are legitimately-distinct elements.
    Project-arm rows all share one project_id (the fetch filters on it), so
    the eid+scope pair already identifies them; keying them without project_id
    keeps behavior stable for callers whose column set omits it."""
    scope = _row_scope(r)
    return (r.get("est_item_id"), scope,
            r.get("project_id") if scope == "account" else None)


def _version_rank(r: dict) -> tuple:
    """Version ordering for rows of ONE element: numeric label, then date."""
    from cp_engine.substance import version_number

    return (version_number(r.get("version_label")),
            str(r.get("version_date") or ""))


def _one_live_per_element(rows: list[dict]) -> list[dict]:
    """Collapse duplicate live rows to ONE per element (`_element_key`) — the
    latest by version ordering (#113 defense).

    `spine_substance` stores one row per VERSION; a live-only fetch should
    yield exactly one row per element, but the data can carry two `status='live'`
    version rows for the same element (e.g. sap-5174's e94d0a03: an authored v7
    plus a distilled v6 the substance mirror re-flipped live). Every read path
    must tolerate that — a listing that emits the same element twice, or a pull
    that resolves to the stale older body, turns one dirty row into wrong
    answers everywhere. Keeps the row with the highest numeric version_label
    (fallback: version_date, then last-fetched) and WARNS when it drops one (a
    collapsed row is dirty data someone should clean, not silently mask).
    Deduped elements keep first-seen order; rows without an est_item_id are
    never merged (unidentifiable) and are appended at the END, after the keyed
    elements — callers relying on interleaving must not pass id-less rows."""
    best: dict[tuple, dict] = {}
    order: list = []
    passthrough: list[dict] = []
    for r in rows:
        if not r.get("est_item_id"):
            passthrough.append(r)
            continue
        k = _element_key(r)
        if k not in best:
            best[k] = r
            order.append(k)
        else:
            kept, dropped = ((r, best[k]) if _version_rank(r) >= _version_rank(best[k])
                             else (best[k], r))
            best[k] = kept
            logger.warning(
                "spine dedup (#113): element %s has multiple live version rows; "
                "keeping %s, hiding %s — the hidden row is dirty data and "
                "should be superseded in MC-2",
                k[0], kept.get("version_label"), dropped.get("version_label"),
            )
    return [best[k] for k in order] + passthrough


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
        the already-computed element-key→row map, so the caller can render
        candidate names WITHOUT re-filtering `rows` (keeps the dedup in one place).

    Distinctness is by `_element_key`, not bare est_item_id: two account-scoped
    elements promoted from different origin projects can share an
    `_authored/<slug>` id — that's a genuine ambiguity (report it), never a
    first-fetched-wins pick. Exception: an exact-id match prefers the caller's
    OWN project-arm row over a same-slug account element (the pre-existing
    scope-arm contract — promote's sibling-collision guard relies on it).
    """
    exact = {_element_key(r): r for r in rows if r.get("est_item_id") == key}
    if exact:
        own = [r for r in exact.values() if _row_scope(r) != "account"]
        if own:
            return own[0], None, None
        if len(exact) > 1:
            return None, "ambiguous", exact
        return next(iter(exact.values())), None, None
    matched = [r for r in rows if _title_matches(key, r.get("framing"))]
    if not matched:
        return None, "no-match", None
    distinct = {_element_key(r): r for r in matched}
    if len(distinct) > 1:
        return None, "ambiguous", distinct
    return next(iter(distinct.values())), None, None


def _row_scope(row: dict) -> str:
    """A row's scope, defaulting pre-migration rows to 'project'."""
    return row.get("scope") or "project"


def _fetch_scoped(client, project_id: str, company_id: str | None,
                  columns: str, *, live_only: bool = True) -> list[dict]:
    """Fetch a project's spine rows PLUS its company's account-scoped rows.

    The scope ladder (mig 104): an element promoted to account scope keeps its
    `project_id` as provenance but becomes readable from every project of its
    `company_id`. Two arms, no dedup needed:

      - project arm: `project_id` rows EXCLUDING scope='account' (an account
        row would otherwise appear twice for its originating project);
      - account arm: `company_id` rows WITH scope='account' (skipped when
        `company_id` is None — initiatives have no company, so no account
        reads and no promotion).
    """
    q = (client.table(Tables.SPINE_SUBSTANCE).select(columns)
         .eq("project_id", project_id))
    if live_only:
        q = q.eq("status", "live")
    rows = [r for r in (getattr(q.execute(), "data", None) or [])
            if _row_scope(r) != "account"]
    if company_id is not None:
        q = (client.table(Tables.SPINE_SUBSTANCE).select(columns)
             .eq("company_id", company_id).eq("scope", "account"))
        if live_only:
            q = q.eq("status", "live")
        rows.extend(getattr(q.execute(), "data", None) or [])
    return rows


def filter_account_by_relevance(
    rows: list[dict], project_est_ids: set[str]
) -> tuple[list[dict], int]:
    """Drop account-scoped people who name no work in THIS project (#179 step 4).

    The account arm above is undifferentiated: a dossier promoted to account
    scope is readable from every project of the company. Measured 2026-08-11,
    that means sap-5171 (display ads) reads all 16 SAP dossiers — 171k chars,
    including the CPO and the President of Concur Travel, both interviewed for
    sap-5174's vision work and irrelevant to display ads.

    A stakeholder's `serves` names the work they are RELEVANT to (the hosted
    `set_spine_element` writes it without claiming `binding='live'`, since a
    person is not work in progress). This uses it to narrow the roster.

    TWO DELIBERATE ABSTENTIONS, both because the links do not exist yet — 0 of
    the tenant's 18 account stakeholders carry `serves` today:

      - a dossier with EMPTY `serves` is KEPT. Hiding un-migrated data is a
        data-loss bug wearing a feature's clothes; the roster degrades to
        today's behaviour until someone links it.
      - only account-scoped rows are considered. A project's own stakeholders
        are already its own.

    Returns `(rows, hidden_count)` so a caller can report what it narrowed
    rather than silently shrinking the list.
    """
    kept: list[dict] = []
    hidden = 0
    for r in rows:
        layer = re.sub(r"[^a-z]", "", str(r.get("layer") or "").lower())
        serves = r.get("serves") or []
        if (
            layer in ("stakeholders", "stakeholder")
            and _row_scope(r) == "account"
            and serves
            and not any(s in project_est_ids for s in serves)
        ):
            hidden += 1
            continue
        kept.append(r)
    return kept, hidden


def resolve_live_element(client, project_id: str, key: str,
                         company_id: str | None = None) -> dict | None:
    """Resolve `key` to the single matching LIVE spine_substance row, or None.

    Same discipline as `pull_spine` (exact est_item_id → single distinct framing
    substring → else None on no-match OR ambiguity). With `company_id`, the
    company's account-scoped elements resolve too (the scope ladder). Returns
    the raw row dict INCLUDING its `id`/`project_id` columns (needed for a
    targeted update — an account row's project_id is its provenance project,
    which may differ from the caller's). Never `SELECT *`.
    """
    rows = _one_live_per_element(_unarchived(
        _fetch_scoped(client, project_id, company_id, _SPINE_RESOLVE_COLUMNS)
    ))
    row, _reason, _ = _match_one_live(rows, key)
    return row


def resolve_element_versions(
    client, project_id: str, key: str, *, columns: str,
    company_id: str | None = None,
) -> tuple[str | None, list[dict]]:
    """Resolve `key` to an element and return ALL its versions (any status).

    The write-side counterpart to `resolve_live_element`: `add_spine_version`
    needs every version of the element (to compute the next version number and
    to demote the current live row), not just the live one. Resolution reuses
    the exact same matcher the read tools use — `key` may be an exact
    `est_item_id` OR a distinct `framing` substring — but scopes by
    `project_id` (the UUID the code resolver produces), NOT by a `project_code`
    string that may not match the slug stored on the row (the bug this fixes).

    Returns `(est_item_id, versions)`:
      - resolved   → `(est_item_id, [all version rows, newest-first])`,
      - no/ambiguous match → `(None, [])`.

    `columns` is the caller's SELECT list (it needs more columns than the read
    path's resolve set, e.g. `id`/`sources`/`origin` for the version rebuild).
    """
    rows = _fetch_scoped(client, project_id, company_id, columns,
                         live_only=False)
    # Match against the LIVE, unarchived rows only (a superseded framing could
    # otherwise resolve an element that no longer has a live version, and a
    # retired element must not accept new versions), mirroring the read path —
    # INCLUDING its one-live-per-element dedup (#113): while an element is
    # double-live, the stale row's framing must not resolve on the write path
    # when the read path no longer surfaces it (resolver divergence). Then
    # return that element's FULL version history, scoped to the matched
    # element's identity (`_element_key`: est_item_id + scope + origin project
    # for account rows), so a same-slug sibling element never mixes into an
    # account element's history.
    live_rows = _one_live_per_element(
        _unarchived([r for r in rows if r.get("status") == "live"]))
    match, _reason, _ = _match_one_live(live_rows, key)
    if match is None:
        return None, []
    est_item_id = match.get("est_item_id")
    match_key = _element_key(match)
    versions = [r for r in rows if _element_key(r) == match_key]
    # Newest-first by version ordering. This is load-bearing for the write
    # path: while an element is double-live, downstream carry-forward
    # (spine_authoring.build_version_rows) bases the new version on the FIRST
    # live row it sees — fetch order must not let the stale live row donate
    # its framing/sources/serves to the next authored version.
    versions.sort(key=_version_rank, reverse=True)
    return est_item_id, versions


def _in_filter(value: str | None, wanted: str | None) -> bool:
    """True when `value` passes a comma-list filter (None filter = pass all).

    Matching is case-insensitive and whitespace-tolerant; a row with a NULL
    value only passes when no filter is set (it can't match any named value).
    """
    if not wanted:
        return True
    allowed = {w.strip().lower() for w in wanted.split(",") if w.strip()}
    if not allowed:
        return True
    return (value or "").lower() in allowed


def _fold_layer(value: str) -> str:
    """Normalize a layer label for matching: casefold + drop ONE trailing 's'.

    Live layer labels drift plural/mixed ("Decisions", "Stakeholders",
    "Client feedback") while callers pass singular ("Decision",
    "stakeholder"); folding both sides makes them equivalent.
    """
    value = value.strip().casefold()
    return value[:-1] if value.endswith("s") else value


# Signal/noise facet (#158 gap 5): the auto-stub layers — one row per
# ingested doc, a pointer not a card. tier='working' is the orientation
# call ("show me the authored working set"); tier='stubs' is the inverse.
_STUB_LAYERS = frozenset({"sourcematerial"})


def _tier_filter(layer, tier: str | None) -> bool:
    if not tier or tier.lower() in ("", "all"):
        return True
    is_stub = re.sub(r"[^a-z]", "", str(layer or "").lower()) in _STUB_LAYERS
    if tier.lower() in ("working", "authored"):
        return not is_stub
    if tier.lower() == "stubs":
        return is_stub
    return True  # unknown tier value: pass everything rather than hide rows


def _layer_filter(value: str | None, wanted: str | None) -> bool:
    """True when a row's `layer` passes a comma-list layer filter.

    Like `_in_filter` (None filter = pass all; NULL value only passes an
    unset filter) but tolerant of real-world layer label drift: each wanted
    term matches casefolded, singular/plural-equivalent (`_fold_layer`), and
    as a substring — so "decision" matches "Decisions" and "feedback"
    matches "Client feedback".
    """
    if not wanted:
        return True
    terms = [w for w in wanted.split(",") if w.strip()]
    if not terms:
        return True  # blank comma-list = no filter (parity with _in_filter)
    allowed = [f for f in (_fold_layer(t) for t in terms) if f]
    if not allowed:
        # Every term folded to "" (e.g. layer="s"): an empty term would
        # substring-match EVERY label, silently turning a degenerate filter
        # into match-all. Treat it as no-match instead, so the caller gets
        # the zero-rows hint rather than the full listing.
        return False
    if value is None:
        return False
    folded = _fold_layer(value)
    return any(term in folded for term in allowed)


def _source_link(asset: dict) -> dict:
    """The typed source link shape MC-2's dashboard writes (manage-by-id)."""
    return {"type": "rag_asset", "id": asset["id"], "title": asset["title"]}


def modify_element_sources(client, project_id: str, key: str,
                           source_title: str, *, add: bool,
                           company_id: str | None = None) -> dict:
    """Attach/detach one ingested source document on a spine element.

    Resolves `key` to ONE live element (same discipline as pull_spine) and
    `source_title` to ONE of the project's active rag_assets (exact title
    first, else a unique case-insensitive substring — mirroring pull_source's
    resolution ladder, minus chunk reads). The typed link
    `{"type": "rag_asset", "id", "title"}` is then added to / removed from the
    `sources` array of EVERY version row — sources are an element-level fact,
    like `serves` — deduped by asset id exactly as MC-2's PATCH /substance
    add_source/remove_source actions do. Adding an already-attached id is a
    no-op (`already: true`); removing an id that isn't attached returns a
    structured note. Never raises past the MCP boundary.
    """
    est_item_id, versions = resolve_element_versions(
        client, project_id, key,
        columns=mc2_db.SPINE_SOURCES_EDIT_COLUMNS, company_id=company_id,
    )
    if est_item_id is None:
        return {"note": f"no single live element matching '{key}'"}

    assets = list_sources(client, project_id, company_id or "")
    exact = [a for a in assets
             if (a.get("title") or "").lower() == source_title.lower()]
    matched = exact or [a for a in assets
                        if _title_matches(source_title, a.get("title"))]
    if not matched:
        return {"note": f"no active source titled '{source_title}'"}
    if len(matched) > 1:
        titles = sorted(a.get("title") or "" for a in matched)
        return {"note": f"ambiguous: '{source_title}' matched "
                        f"{len(matched)} sources: {titles}"}
    link = _source_link(matched[0])

    def _attached(entries: list) -> bool:
        return any(isinstance(s, dict) and s.get("type") == "rag_asset"
                   and s.get("id") == link["id"] for s in entries)

    live = next((r for r in versions if r.get("status") == "live"), None)
    current_live = list((live or {}).get("sources") or [])
    if add and _attached(current_live):
        return {"est_item_id": est_item_id, "source": link,
                "already": True, "sources": current_live}
    if not add and not _attached(current_live):
        return {"note": f"'{link['title']}' is not attached to "
                        f"'{est_item_id}'"}

    result_sources = current_live
    for row in versions:
        entries = list(row.get("sources") or [])
        if add:
            new = entries if _attached(entries) else [*entries, link]
        else:
            new = [s for s in entries
                   if not (isinstance(s, dict) and s.get("type") == "rag_asset"
                           and s.get("id") == link["id"])]
        (client.table(Tables.SPINE_SUBSTANCE).update({"sources": new})
         .eq("id", row["id"]).execute())
        if row is live:
            result_sources = new
    return {"est_item_id": est_item_id, "source": link,
            "attached" if add else "removed": True,
            "sources": result_sources}


def _resolve_source_element(client, project_id: str, key: str,
                            company_id: str | None) -> dict | None:
    """Resolve `key` to ONE element usable as PROVENANCE — INCLUDING retired ones.

    The provenance case (#104) is precisely "fold a now-retired raw card into a
    synthesis card", so unlike the read/write resolvers this must match archived
    elements too. Resolution: exact est_item_id, else a distinct framing
    substring, across ALL of the project's (+ account's) elements regardless of
    status/archived. Returns the newest matching row (carrying est_item_id,
    framing, archived) or None on no-match / ambiguity.
    """
    rows = _fetch_scoped(client, project_id, company_id,
                         mc2_db.SPINE_RESOLVE_COLUMNS, live_only=False)
    exact = [r for r in rows if r.get("est_item_id") == key]
    if exact:
        return exact[0]
    matched = [r for r in rows if _title_matches(key, r.get("framing"))]
    distinct = {r.get("est_item_id"): r for r in matched}
    if len(distinct) != 1:
        return None  # no-match or ambiguous
    return next(iter(distinct.values()))


def modify_element_provenance(client, project_id: str, key: str,
                              source_key: str, *, add: bool,
                              company_id: str | None = None) -> dict:
    """Attach/detach ANOTHER spine element as provenance on a target element (#104).

    The tiering-rule counterpart to `modify_element_sources`: where that attaches
    an ingested `rag_asset`, this attaches a `{"type": "spine_element", "id",
    "title", "retired"}` link — e.g. a raw feedback email folded in as provenance
    under a synthesis card. `key` resolves to ONE LIVE target element (the
    survivor); `source_key` resolves to ONE element that MAY BE RETIRED (the
    folded-in raw material). The link is a property of the target's versions, so
    it SURVIVES the source's retirement — the whole point. Deduped by
    (type, id) so an element source never collides with a rag_asset of the same
    id. Adding an already-attached element is a no-op (`already: true`);
    detaching one that isn't attached returns a structured note.
    """
    est_item_id, versions = resolve_element_versions(
        client, project_id, key,
        columns=mc2_db.SPINE_SOURCES_EDIT_COLUMNS, company_id=company_id,
    )
    if est_item_id is None:
        return {"note": f"no single live element matching '{key}'"}

    src = _resolve_source_element(client, project_id, source_key, company_id)
    if src is None:
        return {"note": f"no single element matching source '{source_key}'"}
    src_eid = src.get("est_item_id")
    if src_eid == est_item_id:
        return {"note": "an element cannot be its own provenance"}
    link = {"type": "spine_element", "id": src_eid,
            "title": src.get("framing") or src_eid,
            "retired": bool(src.get("archived"))}

    def _attached(entries: list) -> bool:
        return any(isinstance(s, dict) and s.get("type") == "spine_element"
                   and s.get("id") == src_eid for s in entries)

    live = next((r for r in versions if r.get("status") == "live"), None)
    current_live = list((live or {}).get("sources") or [])
    if add and _attached(current_live):
        return {"est_item_id": est_item_id, "source": link,
                "already": True, "sources": current_live}
    if not add and not _attached(current_live):
        return {"note": f"'{link['title']}' is not attached to '{est_item_id}'"}

    result_sources = current_live
    for row in versions:
        entries = list(row.get("sources") or [])
        if add:
            new = entries if _attached(entries) else [*entries, link]
        else:
            new = [s for s in entries
                   if not (isinstance(s, dict)
                           and s.get("type") == "spine_element"
                           and s.get("id") == src_eid)]
        (client.table(Tables.SPINE_SUBSTANCE).update({"sources": new})
         .eq("id", row["id"]).execute())
        if row is live:
            result_sources = new
    return {"est_item_id": est_item_id, "source": link,
            "attached" if add else "removed": True,
            "sources": result_sources}


def list_spine(client, project_id: str, company_id: str | None = None, *,
               layer: str | None = None, scope: str | None = None,
               binding: str | None = None, compact: bool = False,
               tier: str | None = None) -> list[dict]:
    """List a project's LIVE spine elements (index, not bodies).

    Returns `[{est_item_id, framing, layer, binding, status, serves_count,
    body_len, important, note, done, scope, version_label, version_date}]`
    for every live element of the project PLUS, when `company_id` is given,
    the company's account-scoped elements (promoted stakeholders etc. — the
    scope ladder; `scope` tells the two apart). `important`/`note` are the
    element's importance flag + annotation; `done` is true/false/null (null =
    n/a, i.e. not bound to a real work-item); `version_label`/`version_date`
    are the live version's label + authored date (the staleness signals —
    spine rows carry no updated_at). The full body is never returned here
    (only its length) — call `pull_spine` for one element's text.

    `compact=True` trims each row to the orientation set — `{est_item_id,
    framing, layer, binding, body_len, version_label, scope, important,
    has_note}` (`has_note` a bool, not the note text) — roughly a fifth of
    the full listing's token cost on a large spine. Detail fields (`status`,
    `serves_count`, `done`, `version_date`, the note text) are dropped;
    re-list without `compact` when you need them.

    `layer`/`scope`/`binding` are optional comma-list filters (e.g.
    layer="Note,Decision", scope="project", binding="unbound"), matched
    case-insensitively; omitted filters pass everything, so the no-filter
    call is unchanged. The `layer` filter additionally folds singular/plural
    and matches substrings (`_layer_filter`) — live layer labels drift
    ("Decisions", "Client feedback") and the filter should meet them where
    they are. When the layer term itself matches NOTHING on the spine, the
    result is a single note row carrying a `hint` list of the distinct layer
    values that exist — a silent `[]` would leave the caller guessing at
    labels. (When the layer DID match but scope/binding emptied the
    combination, the result stays a plain `[]` — the layer vocabulary isn't
    the problem there.) The note/hint row is returned even under
    `compact=True`: callers iterating rows should treat a row without
    `est_item_id` as a note, per the MCP idiom. Never raises: the MCP tool
    boundary converts failures to a structured note.
    """
    all_rows = _one_live_per_element(_unarchived(
        _fetch_scoped(client, project_id, company_id, _SPINE_LIST_COLUMNS)
    ))
    rows = [r for r in all_rows
            if _layer_filter(r.get("layer"), layer)
            and _in_filter(_row_scope(r), scope)
            and _in_filter(r.get("binding"), binding)
            and _tier_filter(r.get("layer"), tier)]
    if layer and not rows:
        # A layer filter that matches nothing must be self-explaining, not a
        # silent [] — surface the labels that DO exist so the caller can
        # re-aim (the layer vocabulary is live data, not a fixed enum). But
        # only blame the layer filter when the layer term ITSELF matched
        # nothing: when it DID match and scope/binding emptied the
        # combination, a hint would steer the caller at the wrong filter —
        # keep those on the plain-[] contract the other filters already have.
        if not any(_layer_filter(r.get("layer"), layer) for r in all_rows):
            layers = sorted({r.get("layer") for r in all_rows
                             if r.get("layer")})
            return [{"note": f"layer filter {layer!r} matched no elements",
                     "hint": layers}]
        return []
    if compact:
        out = [
            {
                "est_item_id": row.get("est_item_id"),
                "framing": row.get("framing"),
                "layer": row.get("layer"),
                "binding": row.get("binding"),
                "body_len": len(row.get("body") or ""),
                "important": bool(row.get("important")),
                "has_note": bool(row.get("note")),
                "scope": _row_scope(row),
                "version_label": row.get("version_label"),
            }
            for row in rows
        ]
        out.sort(key=lambda r: not r["important"])
        return out
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
                "scope": _row_scope(row),
                "version_label": row.get("version_label"),
                "version_date": row.get("version_date"),
            }
        )
    # Important elements sort first; list.sort is stable so within-group order
    # (the query's existing layer ordering) is preserved.
    out.sort(key=lambda r: not r["important"])
    return out


def pull_spine(client, project_id: str, key: str,
               company_id: str | None = None) -> dict:
    """Pull ONE live spine element's full body + context by id or title.

    `key` resolves to a single element, mirroring `pull_source`'s discipline of
    never merging distinct elements:

      1. Exact `est_item_id` match (the machine path: `list_spine` returns these).
      2. Title (`framing`) substring, case-insensitive (`_title_matches`):
         - exactly one distinct element matched → return it,
         - 2+ distinct elements matched → `{body: "", error: "ambiguous: ..."}`.
      3. No match → `{body: "", error: "no spine element ..."}`.

    Returns `{est_item_id, framing, layer, binding, status, serves, sources,
    version_label, body, important, note, done}` on success (`note` is the
    element's importance annotation; `done` is true/false/null where null = n/a /
    not bound to a real work-item). Failure paths return an `error` key. Never
    raises.
    """
    rows = _one_live_per_element(_unarchived(
        _fetch_scoped(client, project_id, company_id, _SPINE_PULL_COLUMNS)
    ))

    row, reason, distinct = _match_one_live(rows, key)
    if row is not None:
        result = _spine_element(row)
        # Surface derived `done` only on a successful single-element pull.
        # Best-effort: if the estimator schema is unreachable the fetch may
        # raise, so we fail-soft to an empty map (→ derive_done returns None).
        # Never let a `done` lookup break the pull.
        try:
            done_map = fetch_project_done_map(client, project_id)
        except Exception:  # noqa: BLE001 — done is best-effort; never break the pull
            logger.warning(
                "pull_spine: done-map fetch failed for project_id=%s; "
                "degrading `done` to None",
                project_id,
                exc_info=True,
            )
            done_map = {}
        result["done"] = derive_done(row.get("est_item_id"), done_map)
        return result
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
        "scope": _row_scope(row),
    }


# ──────────────────────────────────────────────────────────────────────
#  List a project's linked MEETINGS (fathom_meetings)
# ──────────────────────────────────────────────────────────────────────
#
# Fathom meetings are linked to a project via `fathom_meetings.project_id`
# (migration 084). This is the read counterpart for a cp session: "what
# meetings does this project have, and which are in RAG?" — without a RAG call.
# We NEVER select the heavy `transcript` (jsonb) blob and NEVER the large full
# `summary` text into a list view — only explicit scalar columns + the two
# *_at timestamps we collapse to booleans.
_MEETING_LIST_COLUMNS = (
    "recording_id, title, meeting_date, work_item_id, "
    "summary_embedded_at, transcript_promoted_at, fathom_url"
)


def list_project_meetings(client, project_id: str) -> list[dict]:
    """List a project's linked Fathom meetings, newest first (index, not bodies).

    Returns `[{recording_id, title, meeting_date, work_item_id, fathom_url,
    summary_embedded, transcript_promoted}]` for every meeting linked to the
    project via `fathom_meetings.project_id`. The two booleans are derived from
    the `summary_embedded_at` / `transcript_promoted_at` timestamps (each is
    True iff its `*_at` field is non-null) — they tell a caller "is this meeting
    in RAG" without surfacing the timestamps or the heavy `transcript`/`summary`
    payloads. NEVER `select *` and NEVER the `transcript` column. Ordered by
    `meeting_date` descending (most recent first).
    """
    resp = (
        client.table(Tables.FATHOM_MEETINGS)
        .select(_MEETING_LIST_COLUMNS)
        .eq("project_id", project_id)
        .order("meeting_date", desc=True)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return [
        {
            "recording_id": row.get("recording_id"),
            "title": row.get("title"),
            "meeting_date": row.get("meeting_date"),
            "work_item_id": row.get("work_item_id"),
            "fathom_url": row.get("fathom_url"),
            "summary_embedded": row.get("summary_embedded_at") is not None,
            "transcript_promoted": row.get("transcript_promoted_at") is not None,
        }
        for row in rows
    ]


# ──────────────────────────────────────────────────────────────────────
#  Re-fetch the ORIGINAL binary via persisted coords
# ──────────────────────────────────────────────────────────────────────

# Re-fetch coords persisted on `rag_assets` (Task 3) + display fields. NEVER `*`
# / `meta` (large blobs) — only what's needed to reconstruct a FileRef and cite.
_REFETCH_COLUMNS = mc2_db.RAG_ASSET_REFETCH_COLUMNS


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
            client.table(Tables.RAG_ASSETS)
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


def _dropbox_folder_path(connector, folder_id: str) -> str:
    """Resolve MC-2's stored `mc_dropbox_folder_id` to a `/literal/path`.

    The stored value is either already a literal path (`/Clients/Acme/…`) or an
    `id:<body>` form (both of which `files_list_folder` accepts on the read
    side). `upload_file`, though, needs a literal destination path to append a
    filename to — an `id:` prefix has no `/…/name` shape. So: a value that
    already starts with `/` is returned as-is; an `id:`/bare-id value is
    resolved to its `path_display` via `files_get_metadata`. Raises on an
    unresolvable id (caller converts to a structured error).
    """
    if folder_id.startswith("/"):
        return folder_id.rstrip("/")
    meta = connector.dbx.files_get_metadata(folder_id)
    path = getattr(meta, "path_display", None)
    if not path:
        raise ValueError(
            f"Dropbox folder id {folder_id!r} has no path_display "
            "(cannot build an upload destination)"
        )
    return path.rstrip("/")


def push_to_dropbox(
    connector, folder_id: str, local_path, dest_name: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Upload a local file INTO a project's Dropbox folder.

    The write-back counterpart to `fetch_source`: takes the project's stored
    `mc_dropbox_folder_id`, resolves it to a literal folder path, and uploads
    `local_path` there as `dest_name` (defaults to the local filename). Uses the
    connector's `upload_file`, which refuses to clobber an existing file unless
    `overwrite=True`. Returns `{dropbox_path, name, size, overwrote}` on success
    or a structured `{error}` on any failure — NEVER raises (MCP tool boundary).
    """
    src = Path(local_path)
    if not src.exists():
        return {"error": f"local file not found: {local_path}"}
    if not src.is_file():
        return {"error": f"not a file: {local_path}"}

    name = dest_name or src.name
    try:
        folder_path = _dropbox_folder_path(connector, folder_id)
    except Exception as exc:  # noqa: BLE001 — MCP tool boundary, never raise
        return {"error": f"could not resolve Dropbox folder: {exc}"}

    dropbox_path = f"{folder_path}/{name}"
    try:
        meta = connector.upload_file(str(src), dropbox_path, overwrite=overwrite)
    except Exception as exc:  # noqa: BLE001 — MCP tool boundary, never raise
        # The connector raises ComponentFileExistsError when the file exists and
        # overwrite is False; surface that as a clear, actionable message rather
        # than a class name the caller can't act on.
        if type(exc).__name__ == "ComponentFileExistsError":
            return {
                "error": f"a file named {name!r} already exists at "
                f"{dropbox_path!r}; call again with overwrite=True to replace it"
            }
        return {"error": f"upload failed: {exc}"}

    return {
        "dropbox_path": getattr(meta, "path_display", dropbox_path),
        "name": name,
        "size": getattr(meta, "size", src.stat().st_size),
        "overwrote": bool(overwrite),
    }


def _drive_comments(file_id: str) -> list[dict]:
    """Read a Drive file's comments via the Drive API (comments.list). Works for
    Google Docs (whose comments exist ONLY here, not in any export) AND for
    Office files stored in Drive. Returns the normalized comment shape; never
    raises — a Drive/auth failure returns []."""
    try:
        from cloud_storage.google_drive_connector import GoogleDriveConnector

        conn = GoogleDriveConnector(service_account_file=None)
        conn._authenticate()
        fields = ("comments(author/displayName,content,quotedFileContent/value,"
                  "createdTime,resolved,replies(author/displayName,content,createdTime))")
        out: list[dict] = []
        page = None
        while True:
            resp = conn.service.comments().list(
                fileId=file_id, fields=f"nextPageToken,{fields}",
                pageToken=page, includeDeleted=False,
            ).execute()
            for c in resp.get("comments", []):
                out.append({
                    "author": (c.get("author") or {}).get("displayName") or "Unknown",
                    "date": c.get("createdTime"),
                    "anchored_text": (c.get("quotedFileContent") or {}).get("value"),
                    "comment": c.get("content") or "",
                    "resolved": c.get("resolved", False),
                    "replies": [
                        {"author": (r.get("author") or {}).get("displayName") or "Unknown",
                         "date": r.get("createdTime"), "comment": r.get("content") or ""}
                        for r in c.get("replies", [])
                    ],
                })
            page = resp.get("nextPageToken")
            if not page:
                break
        return out
    except Exception:  # noqa: BLE001 — MCP boundary
        return []


def pull_document_comments(client, project_id: str, doc_title: str,
                           dest_dir) -> dict:
    """Read reviewer comments on an ingested document (#108).

    Comments/annotations are dropped by the ingest parsers, so this reads them
    live: for a Drive-hosted asset via the Drive API (covers Google Docs, whose
    comments exist nowhere else); otherwise by downloading the original Office
    binary and parsing its comment XML (docx/pptx/xlsx). Title resolution mirrors
    fetch_source. Returns {title, provider, comment_count, comments:[...]} where
    each comment is {author, date, anchored_text, comment, replies[]}, or a
    structured {note}/{error}. NEVER raises — MCP tool boundary."""
    from cp_engine.doc_comments import extract_comments

    try:
        rows = (
            client.table(Tables.RAG_ASSETS)
            .select(_REFETCH_COLUMNS)
            .eq("project_id", project_id)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        return {"error": f"lookup failed: {exc}"}

    target = doc_title.strip().lower()
    matched = [r for r in rows if _title_matches(doc_title, r.get("title"))]
    exact = [r for r in matched if (r.get("title") or "").lower() == target]
    selected = exact or matched
    if not selected:
        return {"error": f"no source titled {doc_title!r} in project"}
    if len(selected) > 1 and not exact:
        titles = sorted({r.get("title") or "" for r in selected})
        return {"note": f"ambiguous: '{doc_title}' matched {len(titles)} sources: {titles}"}
    row = selected[0]

    provider = row.get("source_provider")
    file_id = row.get("source_file_id")
    title = row.get("title")

    # Drive path — the only one that reaches Google Docs comments, and works
    # without downloading a binary.
    if provider == "drive" and file_id:
        comments = _drive_comments(file_id)
        return {"title": title, "provider": "drive",
                "comment_count": len(comments), "comments": comments}

    # Office-binary path — download the original and parse its comment XML.
    if not provider or not file_id:
        return {"note": f"{title!r} has no re-fetch coords (ingested before the "
                        "live-link layer; re-ingest to enable comment reads)"}
    file_ref = FileRef(source=provider, id=file_id, name=title or "source",
                       mime_type=None, size=None, modified=None,
                       path=row.get("source_path"))
    try:
        local = download_file(file_ref, Path(dest_dir))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"download failed: {exc}"}
    comments = extract_comments(str(local))
    return {"title": title, "provider": provider,
            "comment_count": len(comments), "comments": comments}


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
) -> list[dict]:
    """Write `<project_dir>/_sources.md` — one entry per active source doc.

    Each entry carries a short LLM summary cached per-doc in
    `<project_dir>/_sources.cache.json` keyed by a content hash (`file_hash`),
    so unchanged docs are NEVER re-summarized across runs. The manifest is fully
    regenerated from the current `list_sources`, so removed assets drop out; we
    prune their cache entries too. Returns the written asset entries (the #153
    announcement pass consumes their `created_at`; count = ``len()``).

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
    # The full entry list (id/title/source_type/created_at/summary) — sync's
    # new-source announcements (#153) consume it; `len()` is the old count.
    return assets
