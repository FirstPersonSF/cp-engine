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
    from cp_engine.sync_mc2 import MC2Backend

    config = load_config(_tenant_root())
    client = MC2Backend().connect(config)

    project_id = _resolve_project_id(client, project_code)
    if project_id is None:
        return None

    folders = resolve_project_folders_by_id(client, project_id)
    if folders is None:
        return None
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
      2. Fallback: parse ``<companyprefix>-<number>`` and match
         ``companies.code`` (case-insensitive) + ``projects.number``. This is
         what bridges ``ibx-5192`` → the row whose code is the slug.

    Returns the project id, or ``None`` when nothing resolves.
    """
    rows = (
        client.table("projects")
        .select("id")
        .eq("code", project_code)
        .limit(1)
        .execute()
        .data
        or []
    )
    if rows:
        return rows[0]["id"]

    # Fallback: <companyprefix>-<number> (the working-dir / cp.md Facts form).
    prefix, sep, tail = project_code.rpartition("-")
    if not sep or not tail.isdigit():
        return None
    number = int(tail)
    # companies.code is stored UPPERCASE (e.g. `IBX`) while the working-dir
    # prefix is lowercase (`ibx`); match case-insensitively. The prefix has no
    # %/_ (it's a slugified company code), so ilike treats it as a literal.
    companies = (
        client.table("companies")
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
        client.table("projects")
        .select("id")
        .eq("company_id", company_id)
        .eq("number", number)
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
            return []
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
    from cp_engine.project_sources import pull_source

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {
                "title": doc_title,
                "chunks": [],
                "note": f"project '{project_code}' not found",
            }
        client, pid, cid = resolved
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
def create_spine_element(project_code: str, label: str, type: str,
                         body: str = "", serves: list[str] | None = None) -> dict:
    """Create a new AUTHORED spine element (live v1) in MC-2.

    `type` is the element kind (email|note|source|brief|decision|stakeholder|
    agreement|synthesis|output|activity). `serves` optionally binds it to
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
        eid = rows[0]["est_item_id"]
        existing = (client.table("spine_substance").select("id")
                    .eq("project_code", project_code).eq("est_item_id", eid)
                    .limit(1).execute().data or [])
        if existing:
            return {"error": f"an element '{eid}' already exists; add a version instead"}
        client.table("spine_substance").upsert(rows, on_conflict="id").execute()
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
    (e.g. `_authored/latest-hypothesis`).
    """
    from datetime import datetime, timezone

    from cp_engine.authored_element import build_version_rows

    _SEL = ("id, est_item_id, est_item_kind, phase, binding, layer, placement, "
            "serves, version_label, version_date, status, framing, body, sources, origin")
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        prior = (client.table("spine_substance").select(_SEL)
                 .eq("project_code", project_code).eq("est_item_id", element_id)
                 .execute().data or [])
        if not prior:
            return {"error": f"no authored element {element_id!r} in {project_code!r}"}
        # Demote prior live row(s) via targeted update (no full-row rebuild —
        # mirrors the mc-2 endpoint; avoids clobbering prior sources/version_note).
        for v in prior:
            if v.get("status") == "live":
                client.table("spine_substance").update({"status": "superseded"}).eq("id", v["id"]).execute()
        rows = build_version_rows(
            project_id=pid, project_code=project_code, est_item_id=element_id,
            prior_versions=prior, body=body, version_note=version_note,
            now_iso=datetime.now(timezone.utc).isoformat(),
        )
        client.table("spine_substance").upsert(rows, on_conflict="id").execute()
        live = next(r for r in rows if r["status"] == "live")
        return {"element_id": live["est_item_id"], "version_label": live["version_label"]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to add version in {project_code!r}: {exc}"}


def run_stdio() -> None:
    """Run the server over stdio (what Claude Code launches via .mcp.json)."""
    mcp.run(transport="stdio")
