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
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cp-sources")


def _resolve(project_code: str):
    """Resolve a project CODE to `(client, project_id, company_id)`.

    Shared by both tools (DRY). Resolution is by `projects.id`, NOT a number
    parsed out of the code: slug codes like ``SAP-vision-update-2026`` carry no
    number, so we look the code up in `projects` to get its id, then use the
    authoritative `resolve_project_folders_by_id` for the company id.

    Returns ``None`` when the code matches no project (or its folders can't be
    resolved); callers degrade gracefully on ``None`` rather than crashing.
    """
    from cp_engine.asset_ingest import resolve_project_folders_by_id
    from cp_engine.cli import _load_config_or_die
    from cp_engine.sync_mc2 import MC2Backend

    config = _load_config_or_die()
    client = MC2Backend().connect(config)

    rows = (
        client.table("projects")
        .select("id")
        .eq("code", project_code)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return None
    project_id = rows[0]["id"]

    folders = resolve_project_folders_by_id(client, project_id)
    if folders is None:
        return None
    return client, folders.project_id, folders.company_id


@mcp.tool()
def list_project_sources(project_code: str) -> list[dict]:
    """List a project's ingested source documents (title, type) for this tenant."""
    from cp_engine.project_sources import list_sources

    resolved = _resolve(project_code)
    if resolved is None:
        return []
    client, pid, cid = resolved
    return list_sources(client, pid, cid)


@mcp.tool()
def pull_project_source(
    project_code: str, doc_title: str, query: str | None = None
) -> dict:
    """Pull a source document's content (chunked text + citation) by title.

    Optionally rank by a query for relevance instead of full-doc recency.
    """
    from cp_engine.project_sources import pull_source

    resolved = _resolve(project_code)
    if resolved is None:
        return {
            "title": doc_title,
            "chunks": [],
            "note": f"project '{project_code}' not found",
        }
    client, pid, cid = resolved
    return pull_source(client, pid, cid, doc_title, query=query)


def run_stdio() -> None:
    """Run the server over stdio (what Claude Code launches via .mcp.json)."""
    mcp.run(transport="stdio")
