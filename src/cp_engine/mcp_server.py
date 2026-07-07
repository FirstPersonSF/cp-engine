"""Local stdio MCP server exposing a tenant's project source documents.

This is the LOCAL surface for the ambient-project-sources feature: a minimal
stdio FastMCP server that Claude Code launches (via `.mcp.json` / `cp mcp`) and
calls mid-conversation. It is NOT the deployed mcp-server scaffold.

The two tools here are WIRING ONLY: each resolves a project CODE to the MC-2 ids
and then delegates to a pure function in `cp_engine.project_sources`
(`list_sources` / `pull_source`), which does all the real work. Keeping the tool
layer this thin is deliberate — the same pure functions can later register into
the deployed FastMCP scaffold (FirstPersonSF/1p-component-library starter)
without rewriting any tool logic.

Imports of config / supabase / the pure functions live INSIDE the tool bodies on
purpose: `import cp_engine.mcp_server` stays light (no config load, no network)
so the module is cheap to import and easy to test.

Error contract: an MCP tool is called by Claude mid-conversation, so it must
ALWAYS return a structured result and NEVER raise a protocol error to the
client. Each tool returns one of: real data, a not-found/empty result, or an
`{"error": ...}` note carrying an actionable message. Every failure path
(missing/bad config, absent Supabase creds, RPC errors, embedding failures) is
caught and converted to that note rather than propagated.
"""

from __future__ import annotations

from cp_engine.mc2_db import Tables
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cp-sources")


def _tenant_root():
    """Resolve the tenant root by walking UP from the current working dir.

    Claude Code launches `cp mcp` with its cwd set to whatever directory the
    session opened in — frequently a project subdir (e.g.
    ``1p/infoblox/ibx-5153-ai-campaign``), not the tenant root. The committed
    config (``.cp-engine.toml``) lives only at the root, so we ascend until we
    find it. Falls back to cwd when no config is found in any ancestor, so the
    downstream config load raises its own clear NotATenantRepo error rather than
    this helper crashing.
    """
    from pathlib import Path

    from cp_engine.capture_session import find_tenant_root

    return find_tenant_root(Path.cwd()) or Path.cwd().resolve()


def _resolve(project_code: str):
    """Resolve a project CODE to `(client, project_id, company_id)`.

    Shared by both tools (DRY). Resolution is by `projects.id`, NOT a number
    parsed out of the code: slug codes like ``SAP-vision-update-2026`` carry no
    number, so we look the code up in `projects` to get its id, then use the
    authoritative `resolve_project_folders_by_id` for the company id.

    Returns ``None`` when the code matches no project (or its folders can't be
    resolved); callers degrade gracefully on ``None`` rather than crashing.

    Uses the non-exiting ``cp_engine.config.load`` loader (NOT the CLI's
    ``_load_config_or_die``, which calls ``sys.exit``) so a config problem
    raises a normal exception the tool wrappers can catch — never a SystemExit
    that would tear down the stdio transport.
    """
    from cp_engine.asset_ingest import resolve_project_folders_by_id
    from cp_engine.config import load as load_config
    from cp_engine import mc2_db

    config = load_config(_tenant_root())
    client = mc2_db.get_client(config)

    project_id = _resolve_project_id(client, project_code)
    if project_id is None:
        return None

    # Folders resolve only for engagements (they live in the projects table).
    # An initiative id has no projects row → folders is None; that is NOT an
    # unresolvable code. The spine tools need only project_id (company_id is
    # unused there), so degrade to a None company_id rather than bailing. The
    # source tools, which DO need folders, return empty for a None company_id —
    # correct, since initiatives have no ingested Drive/Dropbox sources.
    folders = resolve_project_folders_by_id(client, project_id)
    if folders is None:
        return client, project_id, None
    return client, folders.project_id, folders.company_id


def _resolve_project_id(client, project_code: str) -> str | None:
    """Resolve a project identifier to its `projects.id`, bridging two id forms.

    The canonical key everything in MC-2 joins on is `projects.code`, which for
    nearly every project is a company-prefixed SLUG (``IBX-platform-sales-
    readiness-summit``, ``GGL-activation``) — NOT ``<company>-<number>``. But
    cp-engine's working-dir slug and ``cp.md`` Facts derive a ``<company>-
    <number>`` id (``ibx-5192``) that does NOT match ``projects.code``. So an
    exact-code lookup misses for any caller passing the working-dir form.

    Resolution order:
      1. Exact ``projects.code`` match (handles a caller that passes the real
         slug code, e.g. from MC-2 directly).
      2. ``slug_full_job_name(full_job_name) == project_code`` — the canonical
         on-disk id since v0.35.0 (``ibx-5153-ai-campaign``), what cp.md Facts,
         the working-dir name, and CLAUDE.md all use. The number lives in the
         MIDDLE of this slug (inside ``full_job_name``), so branch 3 below can't
         see it; this branch reverses ``slug_full_job_name`` instead.
      3. Fallback: parse ``<companyprefix>-<number>`` and match
         ``companies.code`` (case-insensitive) + ``projects.number``. This is
         the legacy bridge for the number-last form (``ibx-5192``).

    Returns the project id, or ``None`` when nothing resolves.
    """
    from cp_engine.state import slug_full_job_name

    rows = (
        client.table(Tables.PROJECTS)
        .select("id")
        .eq("code", project_code)
        .limit(1)
        .execute()
        .data
        or []
    )
    if rows:
        return rows[0]["id"]

    # Exact RAW full_job_name match (the display form, e.g.
    # "IBX 5167 DDI Platform Video"). Fathom stores this verbatim in
    # fathom_meetings.project_tags, so the meetings flow passes it here directly
    # — it is neither the slug code nor the slugified on-disk id, so without this
    # branch every tagged meeting fails to resolve.
    rows = (
        client.table(Tables.PROJECTS)
        .select("id")
        .eq("full_job_name", project_code)
        .limit(1)
        .execute()
        .data
        or []
    )
    if rows:
        return rows[0]["id"]

    # Match the canonical on-disk id: slug_full_job_name(full_job_name).
    # Scope the scan to company-prefixed candidates (the slug always starts with
    # the company prefix) so this stays cheap, then compare the slugified
    # full_job_name in Python — avoids needing a slugify function in SQL.
    prefix = project_code.split("-", 1)[0]
    if prefix:
        candidates = (
            client.table(Tables.PROJECTS)
            .select("id, full_job_name")
            .ilike("code", f"{prefix}-%")
            .execute()
            .data
            or []
        )
        for row in candidates:
            if slug_full_job_name(row.get("full_job_name")) == project_code:
                return row["id"]

    # Fallback: <companyprefix>-<number> (the legacy number-last form).
    # An initiative slug (`mission-control`) has no trailing number, so this
    # branch is skipped and we fall through to the initiatives lookup below.
    prefix, sep, tail = project_code.rpartition("-")
    if not sep or not tail.isdigit():
        return _resolve_initiative_id(client, project_code)
    number = int(tail)
    # companies.code is stored UPPERCASE (e.g. `IBX`) while the working-dir
    # prefix is lowercase (`ibx`); match case-insensitively. The prefix has no
    # %/_ (it's a slugified company code), so ilike treats it as a literal.
    companies = (
        client.table(Tables.COMPANIES)
        .select("id")
        .ilike("code", prefix)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not companies:
        return None
    company_id = companies[0]["id"]
    rows = (
        client.table(Tables.PROJECTS)
        .select("id")
        .eq("company_id", company_id)
        .eq("number", number)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0]["id"] if rows else None


def _resolve_initiative_id(client, code: str) -> str | None:
    """Resolve an INITIATIVE slug code (`mission-control`, `storyos`) to its id.

    Initiatives live in their own `initiatives` table — parallel to projects but
    with no client/company side and no Drive/Dropbox folders — so the project
    bridge in `_resolve_project_id` never matches them. Their id lands in
    `spine_substance.project_id` exactly like a project's, so spine tools work
    once we hand it back. Returns the id, or None when nothing matches.
    """
    rows = (
        client.table(Tables.INITIATIVES)
        .select("id")
        .eq("code", code)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0]["id"] if rows else None


@mcp.tool()
def list_project_sources(project_code: str) -> list[dict]:
    """List a project's ingested source documents (title, type) for this tenant."""
    from cp_engine.project_sources import list_sources

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return [{"note": f"code '{project_code}' resolved to no project"}]
        client, pid, cid = resolved
        return list_sources(client, pid, cid)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return [{"error": f"failed to list sources for '{project_code}': {exc}"}]


@mcp.tool()
def pull_project_source(
    project_code: str, doc_title: str, query: str | None = None
) -> dict:
    """Pull a source document's content (chunked text + citation) by title.

    Optionally rank by a query for relevance instead of full-doc recency.
    """
    from cp_engine.config import load as load_config
    from cp_engine.project_sources import pull_source
    from cp_engine.sync_mc2 import _load_ingest_creds

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {
                "title": doc_title,
                "chunks": [],
                "note": f"project '{project_code}' not found",
            }
        client, pid, cid = resolved
        # A query-ranked pull embeds the query (Voyage), so VOYAGE_API_KEY must be
        # in the environment. A local MCP session resolves it from the mc-2 .env,
        # not the process env (mirrors the spine-promote path). Harmless when
        # `query` is None (no embedding happens).
        if query:
            _load_ingest_creds(load_config(_tenant_root()))
        return pull_source(client, pid, cid, doc_title, query=query)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return {
            "title": doc_title,
            "chunks": [],
            "error": f"failed to pull '{doc_title}' from '{project_code}': {exc}",
        }


@mcp.tool()
def fetch_project_source(project_code: str, doc_title: str) -> dict:
    """Download an ingested source's ORIGINAL file to a local path and return it.

    Use when the embedded text isn't enough and you need the actual binary
    (e.g. a .pptx to inspect hidden slides, an image, original layout). The
    returned `local_path` is on this machine — Read it directly.
    """
    import tempfile

    from cp_engine.project_sources import fetch_source

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        # TODO: cp-fetch-* temp dirs accumulate (never cleaned up); a periodic
        # sweep of stale cp-fetch-* dirs would reclaim the space. Left as-is.
        dest = tempfile.mkdtemp(prefix="cp-fetch-")
        return fetch_source(client, pid, doc_title, dest)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return {
            "error": f"failed to fetch '{doc_title}' from '{project_code}': {exc}"
        }


@mcp.tool()
def list_spine_elements(project_code: str) -> list[dict]:
    """List a project's LIVE spine elements (the distilled-memory index).

    Returns one row per element: est_item_id, framing (title), layer, binding,
    status, serves_count, body_len. Use this to see what's in a project's spine,
    then `pull_spine_element` to read one element's full body.
    """
    from cp_engine.project_sources import list_spine

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            # A structured note, NOT a bare [] — an unresolvable code must never
            # masquerade as a genuinely empty spine (the v0.39.0 false-negative).
            return [{"note": f"code '{project_code}' resolved to no project"}]
        client, pid, _cid = resolved
        return list_spine(client, pid)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return [{"error": f"failed to list spine for '{project_code}': {exc}"}]


@mcp.tool()
def pull_spine_element(project_code: str, key: str) -> dict:
    """Pull ONE live spine element's full body by est_item_id or title.

    `key` is an est_item_id (e.g. `_authored/email-from-olivia...`, exact, the
    machine path) or a case-insensitive substring of the element's title
    (`framing`). Returns the full body + context (layer, binding, serves,
    sources, version_label). Returns an `error` key when nothing/ambiguously
    matches; a successful element may carry its own importance `note`.
    """
    from cp_engine.project_sources import pull_spine

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {
                "body": "",
                "error": f"project '{project_code}' not found",
            }
        client, pid, _cid = resolved
        return pull_spine(client, pid, key)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return {
            "body": "",
            "error": f"failed to pull spine element '{key}' from '{project_code}': {exc}",
        }


@mcp.tool()
def list_project_meetings(project_code: str) -> list[dict]:
    """List a project's linked Fathom meetings (works for engagements + initiatives).

    Returns one row per meeting linked to the project: recording_id, title,
    meeting_date, work_item_id, fathom_url, plus two RAG-state flags —
    `summary_embedded` (the meeting summary is embedded into the RAG store) and
    `transcript_promoted` (its transcript has been promoted into the spine).
    Newest first. This is the read surface so a cp session can see a project's
    meetings and whether each is in RAG without a RAG call; the heavy transcript
    and full summary text are never returned here.
    """
    # Aliased import: the MCP tool and the pure helper share the name
    # `list_project_meetings` but live in different modules.
    from cp_engine.project_sources import (
        list_project_meetings as _list_project_meetings,
    )

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            # A structured note, NOT a bare [] — an unresolvable code must never
            # masquerade as a project with genuinely no meetings (the v0.39.0
            # false-negative).
            return [{"note": f"code '{project_code}' resolved to no project"}]
        client, pid, _cid = resolved
        return _list_project_meetings(client, pid)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return [{"error": f"failed to list meetings for '{project_code}': {exc}"}]


@mcp.tool()
def create_spine_element(project_code: str, label: str, type: str,
                         body: str = "", serves: list[str] | None = None) -> dict:
    """Create a new AUTHORED spine element (live v1) in MC-2.

    `type` is the element kind (email|note|source|brief|decision|stakeholder|
    agreement|synthesis|output|activity|retrospective|research|deliverable|
    timeline|clientfeedback). It is normalized to the spine UI's
    canonical `layer` (e.g. email→Email, source→Source material), so an element
    you author here groups under the same layer the dashboard shows and its
    by-layer filters match. `serves` optionally binds it to
    work-item ids. Returns {element_id, version_label}. The element is live
    immediately and mirrors to the repo on the next cp sync.
    """
    from datetime import datetime, timezone

    from cp_engine.authored_element import build_create_rows

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        rows = build_create_rows(
            project_id=pid, project_code=project_code, label=label, type_=type,
            body=body, serves=serves or [],
            now_iso=datetime.now(timezone.utc).isoformat(),
        )
        # Guard against silently clobbering an existing element with this slug.
        # Scope by project_id (the UUID), NOT project_code: the caller's code
        # string may differ from the slug stored on the row (e.g. `ibx-5153`
        # vs `ibx-5153-ai-campaign`), and a project_code filter would MISS the
        # collision and let a duplicate slug through — the write-side twin of
        # the add_spine_version resolver gap.
        eid = rows[0]["est_item_id"]
        existing = (client.table(Tables.SPINE_SUBSTANCE).select("id")
                    .eq("project_id", pid).eq("est_item_id", eid)
                    .limit(1).execute().data or [])
        if existing:
            return {"error": f"an element '{eid}' already exists; add a version instead"}
        client.table(Tables.SPINE_SUBSTANCE).upsert(rows, on_conflict="id").execute()
        live = next(r for r in rows if r["status"] == "live")
        return {"element_id": live["est_item_id"], "version_label": live["version_label"]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to create element in {project_code!r}: {exc}"}


@mcp.tool()
def add_spine_version(project_code: str, element_id: str, body: str,
                      version_note: str | None = None) -> dict:
    """Add a new version to an existing AUTHORED spine element.

    Supersedes the prior live version (a targeted status update) and creates a
    new live version carrying the `version_note` ("what changed"). Returns
    {element_id, version_label}. `element_id` is the element's est_item_id
    (e.g. `_authored/latest-hypothesis`) OR a distinct `framing` (title)
    substring — the same key forms `pull_spine_element` accepts.
    """
    from datetime import datetime, timezone

    from cp_engine.authored_element import build_version_rows
    from cp_engine.project_sources import resolve_element_versions

    _SEL = ("id, est_item_id, est_item_kind, phase, binding, layer, placement, "
            "serves, version_label, version_date, status, framing, body, sources, "
            "origin, important, note, project_code")
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        # Resolve by project_id (the UUID) + the SAME key matcher the read path
        # uses. The prior code filtered `.eq("project_code", <passed code>)`,
        # which fails whenever the caller's code string differs from the slug
        # stored on the row (e.g. `ibx-5153` vs `ibx-5153-ai-campaign`) — the
        # read/write resolver divergence. Accepts est_item_id OR framing now.
        canonical_id, prior = resolve_element_versions(client, pid, element_id, columns=_SEL)
        if canonical_id is None:
            return {"error": f"no authored element {element_id!r} in {project_code!r}"}
        # Carry the element's OWN stored project_code onto the new rows (the
        # canonical slug), not whatever code form the caller passed in.
        row_project_code = next(
            (v.get("project_code") for v in prior if v.get("project_code")),
            project_code,
        )
        # Demote prior live row(s) via targeted update (no full-row rebuild —
        # mirrors the mc-2 endpoint; avoids clobbering prior sources/version_note).
        for v in prior:
            if v.get("status") == "live":
                client.table(Tables.SPINE_SUBSTANCE).update({"status": "superseded"}).eq("id", v["id"]).execute()
        rows = build_version_rows(
            project_id=pid, project_code=row_project_code, est_item_id=canonical_id,
            prior_versions=prior, body=body, version_note=version_note,
            now_iso=datetime.now(timezone.utc).isoformat(),
        )
        client.table(Tables.SPINE_SUBSTANCE).upsert(rows, on_conflict="id").execute()
        live = next(r for r in rows if r["status"] == "live")
        return {"element_id": live["est_item_id"], "version_label": live["version_label"]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to add version in {project_code!r}: {exc}"}


@mcp.tool()
def set_spine_element(project_code: str, key: str,
                      important: bool | None = None,
                      note: str | None = None,
                      layer: str | None = None) -> dict:
    """Set the `important` flag, standing `note`, and/or `layer` on a spine element.

    `key` is an est_item_id (exact) or a case-insensitive `framing` (title)
    substring resolved to ONE live element (same discipline as
    pull_spine_element). Args left None are not touched (partial update). Marking
    an element important surfaces it first in list_spine_elements and promotes
    its source transcript to RAG. Promotion fires only on a false→true
    transition (not when already important), is engagement-only (initiative
    elements are deferred), and is non-fatal — its outcome surfaces under
    `promotion` in the return, never as a tool {error}, so importance is always
    set. `promote_spine_transcript` is the standalone tool to run/retry promotion
    on its own. `layer` re-files the element under a spine layer (retrospective,
    research, synthesis, decisions, client feedback, timeline, …) — the value is
    normalized to the UI's canonical string and applied to EVERY version of the
    element so its history stays in one layer. Returns {est_item_id, important,
    note[, layer][, promotion]}, or a structured {note}/{error} on miss.
    """
    from cp_engine.authored_element import canon_layer
    from cp_engine.project_sources import resolve_live_element
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, company_id = resolved
        row = resolve_live_element(client, pid, key)
        if row is None:
            return {"note": f"no single live element matching '{key}' in {project_code!r}"}
        # Capture the PRIOR important flag before the patch so we can detect a
        # genuine false→true transition (which triggers RAG promotion below).
        prior_important = bool(row.get("important"))
        patch = {}
        if important is not None:
            patch["important"] = bool(important)
        if note is not None:
            patch["note"] = note
        # Importance is ALWAYS set first — promotion never blocks it.
        if patch:
            client.table(Tables.SPINE_SUBSTANCE).update(patch).eq("id", row["id"]).execute()
        canonical_layer = None
        if layer is not None:
            # Layer describes the element KIND, so every version row moves
            # together — a partial move would scatter one element's history
            # across layers.
            canonical_layer = canon_layer(layer)
            (client.table(Tables.SPINE_SUBSTANCE).update({"layer": canonical_layer})
             .eq("project_id", pid).eq("est_item_id", row["est_item_id"]).execute())
        result = {
            "est_item_id": row["est_item_id"],
            "important": patch.get("important", row.get("important")),
            "note": patch.get("note", row.get("note")),
        }
        if canonical_layer is not None:
            result["layer"] = canonical_layer
        # Promote the source transcript to RAG ONLY on a genuine false→true flip
        # (not when already True — no redundant re-embed — nor when untouched/False).
        # Promotion is NON-FATAL: a failure surfaces under "promotion", never as a
        # tool {error}, so importance:True is always returned.
        if important is True and not prior_important:
            try:
                from cp_engine.config import load as load_config
                from cp_engine.spine_promote import promote_transcript
                from cp_engine.sync_mc2 import _load_ingest_creds, _load_supabase_creds
                root = _tenant_root()
                config = load_config(root)
                supabase_url, supabase_key = _load_supabase_creds(config)
                _load_ingest_creds(config)
                result["promotion"] = promote_transcript(
                    client, root, project_code, pid, company_id, row,
                    supabase_url=supabase_url, supabase_key=supabase_key,
                )
            except Exception as exc:  # noqa: BLE001 — promotion is non-fatal
                result["promotion"] = {"ok": False, "reason": f"promotion error: {exc}"}
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to set element '{key}' in {project_code!r}: {exc}"}


@mcp.tool()
def promote_spine_transcript(project_code: str, key: str) -> dict:
    """Promote a spine element's source transcript into the RAG store.

    Embeds the element's underlying transcript (resolved via its rel_path) into
    rag_assets so it's retrievable via pull_project_source. Idempotent: calling
    again re-runs promotion (the retry door for a failed embed). Engagement-only
    for now — an initiative element returns a 'not yet supported' note.
    Returns {est_item_id, promotion: {ok, ...}} or a structured {note}/{error}.
    """
    from cp_engine.config import load as load_config
    from cp_engine.project_sources import resolve_live_element
    from cp_engine.spine_promote import promote_transcript
    from cp_engine.sync_mc2 import _load_ingest_creds, _load_supabase_creds
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, company_id = resolved
        row = resolve_live_element(client, pid, key)
        if row is None:
            return {"note": f"no single live element matching '{key}' in {project_code!r}"}
        # Same cred source the rest of the engine uses (asset_ingest resolves
        # creds the identical way: _load_supabase_creds over the loaded config).
        root = _tenant_root()
        config = load_config(root)
        supabase_url, supabase_key = _load_supabase_creds(config)
        # Also export OPENAI/VOYAGE keys so the ingest pipeline's client factory
        # finds them (a local MCP session resolves these from the mc-2 .env, not
        # the process env — unlike the webhook, whose container env has them).
        _load_ingest_creds(config)
        result = promote_transcript(
            client, root, project_code, pid, company_id, row,
            supabase_url=supabase_url, supabase_key=supabase_key,
        )
        return {"est_item_id": row.get("est_item_id"), "promotion": result}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to promote '{key}' in {project_code!r}: {exc}"}


def run_stdio() -> None:
    """Run the server over stdio (what Claude Code launches via .mcp.json)."""
    mcp.run(transport="stdio")
