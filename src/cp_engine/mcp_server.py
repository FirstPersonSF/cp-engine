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

from cp_engine.mc2_db import (
    Tables,
    _resolve_initiative_id,
    _resolve_project_id,
)
from mcp.server import MCPServer

# mcp 2.x renamed FastMCP -> MCPServer (2026-07-28 spec release). The decorator
# API (`@mcp.tool()`) and `run(transport="stdio")` are unchanged, so the 37 tool
# definitions below needed no edits. See docs/2026-07-31-mcp-2x-migration.md.
mcp = MCPServer("cp-sources")

# ── Version stamping (#150) ───────────────────────────────────────────────
#
# `cp mcp` is a long-lived process: after a release upgrades the CLI on disk,
# this server keeps serving the OLD bytecode until the user restarts /mcp —
# and its failures then wear misleading masks (a missing-credential message
# for what is really a restart-needed condition). Two defenses:
#
#   * every error payload carries `server_version`, so a failure is always
#     attributable to the code that produced it;
#   * every tool result is checked against the version installed ON DISK
#     (re-read per call — the whole point is catching post-import drift), and
#     a mismatch injects `engine_version_warning` telling the caller to
#     restart /mcp.
#
# `__version__` is frozen at process import → it IS the server's version.
# The disk read deliberately uses importlib.metadata (NOT `__version__`,
# which release.py's docstring rules out as the canonical source): a broken
# or missing dist returns None and we stay silent rather than false-alarm.

_SERVER_VERSION: str | None = None  # populated lazily; import stays light


def _server_version() -> str | None:
    global _SERVER_VERSION
    if _SERVER_VERSION is None:
        try:
            from cp_engine import __version__

            _SERVER_VERSION = __version__
        except Exception:  # noqa: BLE001 — stamping must never break a tool
            pass
    return _SERVER_VERSION


def _installed_version() -> str | None:
    """The cp-engine version currently installed on disk (fresh read)."""
    try:
        import importlib.metadata as _md

        return _md.version("cp-engine")
    except Exception:  # noqa: BLE001 — unknown disk state = no warning
        return None


def _stamp_versions(result):
    """Annotate a tool result in place with version facts (#150).

    Error dicts (an `error` key, top-level or as a list item) gain
    `server_version`. On a server-vs-disk version mismatch, dict results and
    error/note list items also gain `engine_version_warning`. Non-dict
    results and clean rows pass through untouched.
    """
    server = _server_version()
    disk = _installed_version()
    warning = None
    if server and disk and server != disk:
        warning = (
            f"cp mcp server is v{server} but v{disk} is installed on disk — "
            "restart the MCP connection (/mcp) to pick up the new release"
        )

    def _annotate(d: dict) -> None:
        if "error" in d and server:
            d.setdefault("server_version", server)
        if warning:
            d.setdefault("engine_version_warning", warning)

    if isinstance(result, dict):
        _annotate(result)
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and ("error" in item or "note" in item):
                _annotate(item)
    return result


def _tool(fn):
    """`@mcp.tool()` plus the #150 version stamp on every result.

    Preserves the wrapped function's signature explicitly so MCPServer's
    schema introspection sees the tool's real parameters, not `*args`.
    """
    import functools
    import inspect

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return _stamp_versions(fn(*args, **kwargs))

    wrapper.__signature__ = inspect.signature(fn)
    return mcp.tool()(wrapper)


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


def _try_load_dropbox_creds() -> None:
    """Best-effort export of DROPBOX_* into `os.environ` (#111).

    The `DropboxConnector` self-configures from `os.getenv`, so a local MCP
    session — which has the mc-2 `.env` on disk but no DROPBOX_* in its own
    process environment — would otherwise authenticate as nobody on every
    Dropbox-hosted read.

    Deliberately swallows everything. Credential loading is an OPTIONAL
    enrichment of the environment: outside a tenant repo `load_config` raises
    NotATenantRepo, and in production (the ingest webhook) the creds already
    come from the real process env. Neither case should turn a working read
    into an error — if creds genuinely are missing, the connector raises its
    own precise "No Dropbox credentials found" downstream, which is the more
    actionable message.
    """
    try:
        from cp_engine.config import load as load_config
        from cp_engine.sync_mc2 import _load_dropbox_creds

        _load_dropbox_creds(load_config(_tenant_root()))
    except Exception:  # noqa: BLE001 - see docstring: never fail the caller
        pass


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


# Project / initiative id resolution lives in ``mc2_db`` — pure MC-2
# lookups with no MCP involvement. Re-exported here because this module's
# tools call them unqualified, and tests monkeypatch
# ``mcp_server._resolve_project_id``. Do NOT move the bodies back: importing
# this module pulls in FastMCP, and the webhook resolves ids without any MCP
# server (see mc2_db for the full note).


@_tool
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


@_tool
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


@_tool
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
        # A Dropbox-hosted source downloads through the DropboxConnector (#111).
        # No-ops for Drive-hosted sources and when no mc-2 clone is configured.
        _try_load_dropbox_creds()
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


@_tool
def compare_project_sources(project_code: str, doc_a: str, doc_b: str) -> dict:
    """Structural text diff between two versions of a document (#160).

    The read for feedback that arrives as a REVISED COPY of the artifact
    ("comments added" = comments resolved in): per-slide (pptx) or
    per-section (docx/md) text, aligned by best-match similarity — NOT by
    index, decks get reordered — reporting matched pairs (similarity +
    unchanged/edited/moved), cut units, new units, and thin placeholder
    units. `overall_similarity` also answers "are these duplicates?"
    (#158) without pulling either doc into context.

    `doc_a` / `doc_b` are each an ingested source title (fetched like
    `fetch_project_source`) or a local file path. Output is data — narrate
    it into a worklist.
    """
    import tempfile
    from pathlib import Path as _Path

    from cp_engine.project_sources import fetch_source
    from cp_engine.source_compare import compare_files

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        _try_load_dropbox_creds()

        def _local(doc: str, side: str) -> str | dict:
            if _Path(doc).is_file():
                return doc
            dest = tempfile.mkdtemp(prefix="cp-fetch-")
            fetched = fetch_source(client, pid, doc, dest)
            if fetched.get("error"):
                return {"error": f"{side}: {fetched['error']}"}
            return fetched["local_path"]

        path_a = _local(doc_a, "doc_a")
        if isinstance(path_a, dict):
            return path_a
        path_b = _local(doc_b, "doc_b")
        if isinstance(path_b, dict):
            return path_b
        return compare_files(path_a, path_b)
    except ValueError as exc:  # unsupported extension — actionable as-is
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — MCP boundary, never raise
        return {
            "error": f"compare failed for '{doc_a}' vs '{doc_b}': {exc}"
        }


@_tool
def archive_project_source(project_code: str, doc_title_or_id: str) -> dict:
    """Archive one ingested source doc — the RAG-store cleanup verb (#126).

    Soft delete (status → 'archived', same semantics as the dashboard's
    archive): the row, chunks, and spine provenance survive, but the doc
    leaves every active read (list, pull, retrieval ranking). Durable — the
    ingest dedup guard respects archived rows, so a file still sitting in
    Drive/Dropbox is NOT re-ingested unless its content changes.

    `doc_title_or_id` is an asset uuid or EXACT title; an ambiguous title
    returns the candidates (id + created_at) to pick from. Use for true
    duplicates and no-longer-relevant docs; for same-title DISTINCT docs,
    use `rename_project_source` instead — archiving would lose content.
    """
    from cp_engine.project_sources import archive_source

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"code '{project_code}' resolved to no project"}
        client, pid, _cid = resolved
        return archive_source(client, pid, doc_title_or_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"failed to archive source for '{project_code}': {exc}"
        }


@_tool
def rename_project_source(
    project_code: str, doc_title_or_id: str, new_title: str
) -> dict:
    """Retitle one ingested source doc (#126).

    The tool for same-title DISTINCT documents (recurring recordings always
    export under one title) that predate ingest-time date-suffixing —
    retitle instead of archiving real content. Readers resolve by title, so
    the new title is live immediately; `_sources.md` follows on next sync.

    `doc_title_or_id` resolves like `archive_project_source` (uuid or exact
    title; ambiguous titles return candidates to pick from).
    """
    from cp_engine.project_sources import rename_source

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"code '{project_code}' resolved to no project"}
        client, pid, _cid = resolved
        return rename_source(client, pid, doc_title_or_id, new_title)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"failed to rename source for '{project_code}': {exc}"
        }


@_tool
def push_to_dropbox(
    project_code: str, local_path: str, dest_name: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Upload a local file INTO a project's Dropbox folder (the write-back path).

    The inverse of `fetch_project_source`: use it to put a rich document you
    generated on this machine — a `.pptx` deck, a `.docx`, a PDF report — back
    into the project's Dropbox so the humans find it where they expect. Resolves
    the project's configured Dropbox folder, then uploads `local_path` there as
    `dest_name` (defaults to the local filename).

    Refuses to overwrite an existing file unless `overwrite=True` (so a second
    call with the same name is a safe no-clobber by default; pass overwrite=True
    to replace). Works for engagements AND initiatives — whichever has a Dropbox
    folder configured in MC-2. Returns {dropbox_path, name, size, overwrote}, or
    a structured {error} (no Dropbox folder configured, file not found, name
    collision, upload failure).
    """
    from cp_engine.asset_ingest import resolve_project_folders_by_id
    from cp_engine.config import load as load_config
    from cp_engine.project_sources import push_to_dropbox as _push
    from cp_engine.sync_mc2 import _load_dropbox_creds

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        folders = resolve_project_folders_by_id(client, pid)
        if folders is None or not folders.mc_dropbox_folder_id:
            return {
                "error": f"{project_code!r} has no Dropbox folder configured "
                "in MC-2 — nowhere to push to"
            }
        # The DropboxConnector reads DROPBOX_* from os.getenv; a local MCP
        # session resolves them from the mc-2 .env (mirrors pull_project_source's
        # ingest-creds load), not the bare process env.
        _load_dropbox_creds(load_config(_tenant_root()))
        from cloud_storage.dropbox_connector import DropboxConnector

        connector = DropboxConnector()
        return _push(
            connector, folders.mc_dropbox_folder_id, local_path,
            dest_name=dest_name, overwrite=overwrite,
        )
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return {
            "error": f"failed to push '{local_path}' to {project_code!r} "
            f"Dropbox: {exc}"
        }


@_tool
def pull_document_comments(project_code: str, doc_title: str) -> dict:
    """Read the reviewer COMMENTS on an ingested document (#108).

    Inline comments/annotations are the highest-signal stakeholder capture on a
    project — the client's own reviewers saying, in their words, what to change —
    but the ingest parsers drop them (they live in the file's comment layer, not
    its body). This reads them LIVE: a Google-Drive-hosted doc via the Drive API
    (the ONLY way to reach Google Docs comments, which exist nowhere in an
    export), otherwise by fetching the original Office binary (.docx/.pptx/.xlsx)
    and parsing its comment XML. `doc_title` resolves like pull_project_source
    (exact title, else a unique substring).

    Returns {title, provider, comment_count, comments} where each comment is
    {author, date, anchored_text, comment, replies[]} — grouped by author, it
    doubles as stakeholder intelligence ("what did each reviewer push on?").
    Returns {comment_count: 0} for a doc with no comments, or a {note}/{error}.
    """
    import tempfile

    from cp_engine.project_sources import pull_document_comments as _pull

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        # A Dropbox-hosted doc is read by downloading the original binary and
        # parsing its comment XML, which goes through the DropboxConnector's
        # os.getenv self-configuration. Without this the verb fails with
        # "No Dropbox credentials found" on every Dropbox source (#111).
        _try_load_dropbox_creds()
        dest = tempfile.mkdtemp(prefix="cp-comments-")
        return _pull(client, pid, doc_title, dest)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to read comments on '{doc_title}' "
                         f"in '{project_code}': {exc}"}


@_tool
def list_spine_elements(project_code: str, layer: str = "",
                        scope: str = "", binding: str = "",
                        compact: bool = False, tier: str = "") -> list[dict]:
    """List a project's LIVE spine elements (the distilled-memory index).

    Returns one row per element: est_item_id, framing (title), layer, binding,
    status, serves_count, body_len, version_label, version_date. Use this to
    see what's in a project's spine, then `pull_spine_element` to read one
    element's full body.

    `compact=true` returns a trimmed row — est_item_id, framing, layer,
    binding, body_len, version_label, scope, important, has_note (bool; the
    note text is dropped) — at a fraction of the token cost. Prefer it for
    orientation on a big spine; re-list without it when you need status,
    serves_count, done, version_date, or the note text.

    Optional filters narrow the listing, each a comma-list matched
    case-insensitively: `layer` (e.g. "Note,Decision"), `scope` ("project" or
    "account"), `binding` (e.g. "unbound"). The layer filter also folds
    singular/plural and matches substrings ("decision" matches "Decisions",
    "feedback" matches "Client feedback"); a layer term that matches nothing
    on the spine returns a note row (even under compact) with a `hint` list
    of the layer values that actually exist — when the layer matched but
    scope/binding emptied the combination, you get a plain empty list
    instead. Empty filters return everything — useful
    defaults for a first look.

    `tier` is the signal/noise facet (#158): "working" (or "authored")
    drops the per-doc source stubs so orientation reads the authored
    working set; "stubs" shows only them; ""/"all" shows everything.
    Prefer `tier="working", compact=true` as the first call on a big spine.
    """
    from cp_engine.project_sources import list_spine

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            # A structured note, NOT a bare [] — an unresolvable code must never
            # masquerade as a genuinely empty spine (the v0.39.0 false-negative).
            return [{"note": f"code '{project_code}' resolved to no project"}]
        client, pid, cid = resolved
        return list_spine(client, pid, cid, layer=layer or None,
                          scope=scope or None, binding=binding or None,
                          compact=compact, tier=tier or None)
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return [{"error": f"failed to list spine for '{project_code}': {exc}"}]


@_tool
def pull_spine_element(project_code: str, key: str) -> dict:
    """Pull ONE live spine element's full body by est_item_id or title.

    `key` is an est_item_id (e.g. `_authored/email-from-olivia...`, exact, the
    machine path) or a case-insensitive substring of the element's title
    (`framing`). Returns the full body + context (layer, binding, serves,
    sources, version_label). Returns an `error` key when nothing/ambiguously
    matches; a successful element may carry its own importance `note`.

    AGREEMENT elements are projections: the stored body carries only the
    human-authored terms, and this pull composes the live engagement shape
    (phases, deliverables, dates, done-marks) from the estimator into the
    returned body (`derived_block: true`). Deliverables/dates edited in the
    estimate are instantly true here — never retype them into the element.
    """
    from cp_engine.project_sources import pull_spine

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {
                "body": "",
                "error": f"project '{project_code}' not found",
            }
        client, pid, cid = resolved
        result = pull_spine(client, pid, key, cid)
        if (result.get("layer") or "").lower() == "agreement" and not result.get("error"):
            # Compose the read-side projection. Fail-soft: a missing estimate
            # (initiatives, pre-estimate deals) or a fetch error just means no
            # block — never break the pull.
            try:
                from datetime import date as _date

                from cp_engine.agreement_projection import (
                    drift_warnings, render_engagement_block, sow_attach_nudge,
                )
                from cp_engine.estimate import fetch_estimate, fetch_schedule
                est = fetch_estimate(client, pid)
                if est is not None:
                    bars = fetch_schedule(client, est.id)
                    # Drift is best-effort within the best-effort block: a
                    # meetings fetch failure just means no divergence rule.
                    try:
                        from cp_engine.project_sources import (
                            list_project_meetings,
                        )
                        meetings = list_project_meetings(client, pid)
                    except Exception:  # noqa: BLE001
                        meetings = []
                    drift = drift_warnings(
                        est, bars, meetings, today=_date.today(),
                    )
                    result["body"] = (result.get("body") or "") + "\n\n" + \
                        render_engagement_block(est, bars, drift=drift)
                    result["derived_block"] = True
                    if drift:
                        result["drift_warnings"] = drift
                if not result.get("sources") and cid is not None:
                    from cp_engine.project_sources import list_sources
                    nudge = sow_attach_nudge(list_sources(client, pid, cid))
                    if nudge:
                        result["attach_nudge"] = nudge
            except Exception:  # noqa: BLE001 — projection is best-effort
                pass
        return result
    except Exception as exc:  # noqa: BLE001
        # An MCP tool must never throw to the client: return a structured,
        # actionable error note instead of propagating a protocol error.
        return {
            "body": "",
            "error": f"failed to pull spine element '{key}' from '{project_code}': {exc}",
        }


@_tool
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


@_tool
def create_spine_element(project_code: str, label: str, type: str,
                         body: str = "", serves: list[str] | None = None,
                         step_title: str | None = None) -> dict:
    """Create a new AUTHORED spine element (live v1) in MC-2.

    `type` is the element kind (email|note|source|brief|decision|stakeholder|
    agreement|synthesis|output|activity|retrospective|research|deliverable|
    timeline|clientfeedback). It is normalized to the spine UI's
    canonical `layer` (e.g. email→Email, source→Source material), so an element
    you author here groups under the same layer the dashboard shows and its
    by-layer filters match. `serves` optionally binds it to
    work-item ids. Returns {element_id, version_label[, step]}. The element is
    live immediately and mirrors to the repo on the next cp sync.

    Auto-journals creation as a review-gated `source='auto'` step for today
    (design 2026-07-23-auto-step-on-version-write), so authoring a card always
    leaves an activity record. `step_title` overrides the derived "Created
    <label>" title. Non-fatal — a journal miss surfaces under `step`, never
    fails the create.
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
        result = {"element_id": live["est_item_id"],
                  "version_label": live["version_label"]}
        # Auto-journal creation (non-fatal — never fails the create).
        try:
            from cp_engine import spine_steps
            result["step"] = spine_steps.upsert_auto_step(
                client, pid, live["est_item_id"],
                step_title or f"Created {label}",
                step_date=datetime.now(timezone.utc).isoformat()[:10],
                company_id=_cid,
            )
        except Exception as exc:  # noqa: BLE001 — journaling is non-fatal
            result["step"] = {"error": f"auto-step failed: {exc}"}
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to create element in {project_code!r}: {exc}"}


@_tool
def add_spine_document(
    project_code: str, label: str, type: str = "synthesis",
    file_path: str | None = None, source_title: str | None = None,
    serves: list[str] | None = None,
) -> dict:
    """Add a whole DOCUMENT to the spine as a new element — file OR ingested source.

    A document-oriented wrapper over `create_spine_element` for the two shapes
    that otherwise force a manual copy-paste of the body:

    - `file_path` — read a local file (a synthesis `.md` you generated, a
      reference doc on disk) and author its content as the element body. UTF-8
      text files (`.md`/`.txt`/etc.) only; for a binary you want in the spine,
      push it to Dropbox / ingest it as a source instead, then use
      `source_title` here.
    - `source_title` — pull an ALREADY-INGESTED source's text (by title, resolved
      like `pull_project_source`) as the body AND auto-attach that source to the
      new element (the same rag_asset link the hosted `add_element_source`
      writes), so the card carries its own provenance. "Turn this ingested
      brief into a spine card."

    Provide exactly ONE of `file_path` / `source_title`. `type` is the element
    kind (default `synthesis`; use `source` for reference material, or any kind
    `create_spine_element` accepts). `serves` optionally binds work-item ids.
    Returns {element_id, version_label[, source_attached]}, or a structured
    {error}. To UPDATE an existing element from a document instead, read the file
    and pass its text to `add_spine_version`.
    """
    from cp_engine.config import load as load_config
    from cp_engine.sync_mc2 import _load_ingest_creds

    try:
        if bool(file_path) == bool(source_title):
            return {"error": "provide exactly one of file_path or source_title"}

        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved

        # ── Assemble the body from the chosen document source ──
        if file_path:
            from pathlib import Path

            src = Path(file_path)
            if not src.exists() or not src.is_file():
                return {"error": f"file not found: {file_path}"}
            try:
                body = src.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return {
                    "error": f"{file_path!r} is not UTF-8 text — ingest it as a "
                    "source (or push to Dropbox) and pass source_title instead"
                }
            if not body.strip():
                return {"error": f"file is empty: {file_path}"}
        else:
            from cp_engine.project_sources import pull_source

            if cid is None:
                return {
                    "error": "source_title works for engagements (initiatives "
                    "have no ingested sources) — use file_path for an initiative"
                }
            # No query: full-doc text in returned order (see pull_source). Load
            # ingest creds defensively though no embedding happens on query=None.
            _load_ingest_creds(load_config(_tenant_root()))
            pulled = pull_source(client, pid, cid, source_title)
            chunks = pulled.get("chunks") or []
            if not chunks:
                note = pulled.get("note") or "no chunks"
                return {"error": f"could not read source {source_title!r}: {note}"}
            body = "\n\n".join(chunks)

        # ── Author the element (reuse create_spine_element's validated path) ──
        created = create_spine_element(
            project_code, label, type, body=body, serves=serves,
        )
        if "error" in created:
            return created

        # ── For the ingested-source path, attach the source for provenance ──
        # Calls `modify_element_sources` in-process: the `add_element_source`
        # TOOL moved to the hosted server (#143), but this attach is an internal
        # step of one stdio verb, not a second write door.
        result = dict(created)
        if source_title:
            from cp_engine.project_sources import modify_element_sources

            try:
                attached = modify_element_sources(
                    client, pid, created["element_id"], source_title,
                    add=True, company_id=cid,
                )
            except Exception as exc:  # noqa: BLE001
                attached = {"error": str(exc)}
            result["source_attached"] = (
                attached.get("source") if "error" not in attached
                else f"(attach failed: {attached['error']})"
            )
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to add document to {project_code!r}: {exc}"}


@_tool
def add_spine_version(project_code: str, element_id: str, body: str,
                      version_note: str | None = None,
                      step_title: str | None = None) -> dict:
    """Add a new version to an existing AUTHORED spine element.

    Supersedes the prior live version (a targeted status update) and creates a
    new live version carrying the `version_note` ("what changed"). Returns
    {element_id, version_label}. `element_id` is the element's est_item_id
    (e.g. `_authored/latest-hypothesis`) OR a distinct `framing` (title)
    substring — the same key forms `pull_spine_element` accepts.

    Auto-journals the move: on success it proposes ONE `source='auto'` step for
    today on this element's trail (review-gated — a human confirms/dismisses it),
    so a content-write always leaves an activity record without a manual
    wrap-up proposal (design 2026-07-23-auto-step-on-version-write). Pass
    `step_title` to give that step the real move's words ("Built Mehul's cube
    framing into deck v09"); omitted, it falls back to `version_note`, else
    "Updated <framing> (v<N>)". A second bump of the same element the same day
    UPDATES that step's title rather than stacking a row. The auto-step is
    NON-FATAL: any failure surfaces under `step` in the return, never as a tool
    {error} — a journal miss never fails the version write. Returns
    {element_id, version_label[, step]}.
    """
    from datetime import datetime, timezone

    from cp_engine.authored_element import build_version_rows
    from cp_engine.project_sources import resolve_element_versions

    _SEL = ("id, est_item_id, est_item_kind, phase, binding, layer, placement, "
            "serves, version_label, version_date, status, framing, body, sources, "
            "origin, important, note, project_code, archived, scope, project_id")
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
        canonical_id, prior = resolve_element_versions(client, pid, element_id, columns=_SEL, company_id=_cid)
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
        row_project_id = next(
            (v.get("project_id") for v in prior if v.get("project_id")), pid)
        rows = build_version_rows(
            project_id=row_project_id, project_code=row_project_code, est_item_id=canonical_id,
            prior_versions=prior, body=body, version_note=version_note,
            now_iso=datetime.now(timezone.utc).isoformat(),
        )
        client.table(Tables.SPINE_SUBSTANCE).upsert(rows, on_conflict="id").execute()
        live = next(r for r in rows if r["status"] == "live")
        result = {"element_id": live["est_item_id"],
                  "version_label": live["version_label"]}
        # Auto-journal the move as a review-gated step (non-fatal — a journal
        # miss must never fail the version write). Title priority:
        # explicit step_title > version_note > derived "Updated <framing> (v<N>)".
        try:
            from cp_engine import spine_steps
            title = (step_title or version_note
                     or f"Updated {live.get('framing') or canonical_id} "
                        f"({live['version_label']})")
            result["step"] = spine_steps.upsert_auto_step(
                client, row_project_id, canonical_id, title,
                step_date=datetime.now(timezone.utc).isoformat()[:10],
                company_id=_cid,
            )
        except Exception as exc:  # noqa: BLE001 — journaling is non-fatal
            result["step"] = {"error": f"auto-step failed: {exc}"}
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to add version in {project_code!r}: {exc}"}


# NOTE (cp-engine #143): `set_spine_element` now lives on the hosted MCP server
# (`cp-hosted` connector, same verb name), where the write carries the caller's
# identity and lands in the audit log. CAVEAT: the stdio wrapper also fired a
# RAG transcript promotion on an `important` false→true flip; the hosted copy
# does NOT mirror that side effect yet (tracked on #143 — it lands when
# `promote_spine_transcript` ports). The promotion path itself is INTACT here:
# `cp_engine.spine_promote.promote_transcript` is still called by the
# `promote_spine_transcript` tool below, which is the standalone run/retry door
# for exactly this promotion. Do not delete that helper.


# NOTE (cp-engine #143): the relation/step verbs — `create_spine_relation`,
# `add_spine_step`, `propose_spine_step` (batch 1) and `set_spine_step`,
# `reorder_spine_step`, `remove_spine_step` (batch 2) — now live on the hosted
# MCP server (`cp-hosted` connector, same verb names), where writes carry the
# caller's identity and land in the audit log. They were removed from this stdio
# server so the surface never exists twice; the underlying `cp_engine.spine_steps`
# module stays (close_out.py and add_spine_version's auto-step still call it).
#
# Batch 3 followed the same way: `add_element_source`, `remove_element_source`,
# `add_element_provenance` and `remove_element_provenance` are hosted verbs now.
# Their implementations — `project_sources.modify_element_sources` /
# `modify_element_provenance` / `_resolve_source_element` — STAY: that module is
# shared read/write machinery (pull_source, resolve_element_versions, …) that the
# surviving stdio tools depend on, and `add_spine_document` calls
# `modify_element_sources` in-process for its source_title attach.
#
# Batch 4 took the retire/scope guarded verbs: `retire_spine_element`,
# `retire_spine_elements`, `retire_spine_relation`, `promote_stakeholder`,
# `demote_stakeholder` and `set_element_account_scope` are hosted verbs now,
# where the retire is one atomic guarded function (the stdio copy was four
# service-key statements with a mid-failure corruption window) and the
# engagements-only + sibling-twin scope guards live in the DB.
# `project_sources.resolve_live_element` STAYS — it is the shared element
# matcher behind `pull_spine_element`, `pull_element_from_project` and the
# framework verbs, not retire-specific machinery. And the account-scope MOVE
# stays in-process as `_set_account_scope` below, because
# `pull_element_from_project(account=True)` needs it as an internal step of a
# surviving verb (the same shape as batch 3's `add_spine_document` attach) —
# an internal helper, not a second write door.
# See docs/hosted-mcp-team-setup.md.


def _promote_stakeholder(project_code: str, key: str) -> dict:
    """Promote a project's stakeholder element to ACCOUNT scope.

    In-process helper only — the `promote_stakeholder` TOOL moved to the hosted
    server (#143). This body survives as the implementation behind
    `_set_account_scope`, which `pull_element_from_project(account=True)` calls
    as an internal step.

    Stakeholders are account-level people wearing project clothes: promotion
    makes the element readable from EVERY project of the company (it appears
    in their list/pull with scope='account'), while `project_id` stays as
    provenance. Opt-in and human-triggered — engagement-specific reads that
    shouldn't travel belong in a separate project-scoped element. Every
    version of the element moves together. Engagements only (initiatives have
    no company). Returns {est_item_id, scope, company_id, layer[, warning]},
    or a structured {note}/{error} on miss/collision.
    """
    from cp_engine.project_sources import resolve_live_element

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        if cid is None:
            return {"note": "initiatives have no company — account promotion "
                            "applies to engagements only"}
        row = resolve_live_element(client, pid, key, cid)
        if row is None:
            return {"note": f"no single live element matching '{key}' in {project_code!r}"}
        if (row.get("scope") or "project") == "account":
            return {"note": f"'{row['est_item_id']}' is already account-scoped"}
        eid = row["est_item_id"]
        # Collision guard: the same slug already promoted from a SIBLING
        # project means this person exists at account scope — version that
        # element instead of creating a twin.
        twins = (client.table(Tables.SPINE_SUBSTANCE)
                 .select("est_item_id, project_id")
                 .eq("company_id", cid).eq("scope", "account")
                 .eq("est_item_id", eid).limit(1).execute().data or [])
        if twins and str(twins[0].get("project_id")) != str(pid):
            return {"error": f"'{eid}' already exists at account scope "
                             "(promoted from another project) — add_spine_version "
                             "on the account element instead"}
        # Element-level move: every version row travels together (same
        # discipline as layer/framing/serves).
        (client.table(Tables.SPINE_SUBSTANCE)
         .update({"scope": "account", "company_id": cid})
         .eq("project_id", pid).eq("est_item_id", eid).execute())
        result = {"est_item_id": eid, "scope": "account", "company_id": cid,
                  "layer": row.get("layer")}
        if (row.get("layer") or "").lower() not in ("stakeholders", "stakeholder"):
            result["warning"] = (f"layer is {row.get('layer')!r}, not Stakeholders — "
                                 "promotion applied, but check this is really an "
                                 "account-level element")
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to promote '{key}' in {project_code!r}: {exc}"}


def _demote_stakeholder(project_code: str, key: str) -> dict:
    """Remove an element from ACCOUNT scope — the inverse of _promote_stakeholder.

    In-process helper only (see `_promote_stakeholder`); the tool is hosted.

    The element returns to its PROVENANCE project (scope='project',
    company_id cleared; project_id never changed, so there is exactly one
    home for it to land). It disappears from sibling projects' spines and
    the account roster; nothing is deleted, and re-promoting restores
    account visibility. Every version moves together. `key` resolves the
    account element from ANY of the company's projects. Returns
    {est_item_id, scope, returned_to_project_id}, or a structured
    {note}/{error} on miss.
    """
    from cp_engine.project_sources import resolve_live_element

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        row = resolve_live_element(client, pid, key, cid)
        if row is None:
            return {"note": f"no single live element matching '{key}' in {project_code!r}"}
        if (row.get("scope") or "project") != "account":
            return {"note": f"'{row['est_item_id']}' is not account-scoped — nothing to demote"}
        provenance_pid = row.get("project_id") or pid
        (client.table(Tables.SPINE_SUBSTANCE)
         .update({"scope": "project", "company_id": None})
         .eq("project_id", provenance_pid)
         .eq("est_item_id", row["est_item_id"]).execute())
        return {"est_item_id": row["est_item_id"], "scope": "project",
                "returned_to_project_id": provenance_pid}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to demote '{key}' in {project_code!r}: {exc}"}


def _set_account_scope(
    project_code: str, key: str, account: bool = True,
) -> dict:
    """Tag ANY spine element account-level (or return it to project scope).

    In-process helper only — the `set_element_account_scope` TOOL, like
    `promote_stakeholder`/`demote_stakeholder`, moved to the hosted server
    (#143). It stays here because `pull_element_from_project(account=True)`
    account-tags the copy it just authored as an internal step of that
    surviving verb.

    The type-agnostic generalization of promote/demote stakeholder:
    use it to make a synthesis, a source, a decision — any element, not just a
    stakeholder — readable from EVERY project of the same company (`account=True`,
    scope='account', company_id set, provenance project unchanged), or to pull it
    back to its home project (`account=False`). Every version of the element
    moves together. Engagements only (initiatives have no company). Opt-in and
    human-triggered — engagement-specific content that shouldn't travel belongs
    in a project-scoped element. Returns
    {est_item_id, scope, company_id?, returned_to_project_id?}, or {note}/{error}.

    (For a stakeholder specifically, the hosted `promote_stakeholder` is the same
    move with a layer sanity-check — prefer it there; this is for everything else.)
    """
    if account:
        return _promote_stakeholder(project_code, key)
    return _demote_stakeholder(project_code, key)


@_tool
def pull_element_from_project(
    from_code: str, to_code: str, key: str, type: str = "synthesis",
    account: bool = False,
) -> dict:
    """Copy a spine element FROM another project INTO this one, with lineage.

    The "pull content from another project" move: resolve `key` to one live
    element in `from_code` (est_item_id or a distinct title substring, exactly
    like `pull_spine_element`), then author a COPY of its body as a new element
    in `to_code`. The copy's body is prefixed with an origin provenance line
    (`from <from_code> · <est_item_id>`) and its version note records the source,
    so the lineage is legible in the element itself. `type` sets the copy's kind
    (default `synthesis` — a cross-project pull is usually re-synthesis; pass
    `source`/`reference` to carry it verbatim). With `account=True` the copy
    lands account-scoped immediately (readable from every sibling project — the
    account-tagging ask), so a body pulled once serves the whole company.

    Lineage note: spine relation edges are within-project (they key on
    est_item_id in one project's space), so cross-project lineage is recorded as
    provenance IN the copied element — the origin line + version note — rather
    than a dangling edge. Within `to_code`, wire a `derives_from` edge from the
    copy to any local element it builds on with `create_spine_relation` (hosted
    server verb — cp-engine #143).

    Returns {element_id, version_label, origin, account_scoped}, or a structured
    {error}. Does NOT move the original — the source project keeps its element.
    """
    from cp_engine.project_sources import pull_spine

    try:
        src = _resolve(from_code)
        if src is None:
            return {"error": f"source project {from_code!r} not found"}
        s_client, s_pid, s_cid = src
        # Read the source element (its own scope ladder, incl. account elements).
        pulled = pull_spine(s_client, s_pid, key, s_cid)
        if pulled.get("error"):
            return {"error": f"in {from_code!r}: {pulled['error']}"}
        origin_id = pulled.get("est_item_id") or key
        origin_framing = pulled.get("framing") or origin_id
        body = pulled.get("body") or ""
        if not body.strip():
            return {"error": f"source element {origin_id!r} in {from_code!r} has "
                             "an empty body — nothing to pull"}

        # Stamp legible cross-project provenance into the copy's body head.
        origin_line = f"> _Pulled from **{from_code}** · `{origin_id}` ({origin_framing})_"
        copied_body = f"{origin_line}\n\n{body}"
        label = f"{origin_framing} (from {from_code})"

        created = create_spine_element(to_code, label, type, body=copied_body)
        if "error" in created:
            return created

        # Record the source in the new element's first version note via a
        # targeted add_spine_version? No — create already made v1. Instead carry
        # provenance in the body (above) + the return payload. Account-tag if asked.
        account_scoped = False
        if account:
            promoted = _set_account_scope(to_code, created["element_id"], True)
            account_scoped = promoted.get("scope") == "account"

        return {
            "element_id": created["element_id"],
            "version_label": created["version_label"],
            "origin": {"project": from_code, "est_item_id": origin_id,
                       "framing": origin_framing},
            "account_scoped": account_scoped,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to pull '{key}' from {from_code!r} into "
                         f"{to_code!r}: {exc}"}


@_tool
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
        row = resolve_live_element(client, pid, key, company_id)
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


@_tool
def framework_readiness(layer: str | None = None) -> dict:
    """List the curated inbound frameworks (the synthesis menu) + snapshot identity.

    Returns ONLY frameworks with a curated decompose/compose template — the
    ones worth offering (everything else discards by design). `layer` filters
    by UNF layer (category|vision|audience|messaging|offering|proof|culture|
    competitive). No LLM call, no project needed. INTERNAL: framework
    names/ids never go into client-facing material.
    """
    from cp_engine.frameworks import readiness_menu

    try:
        return readiness_menu(layer)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to list frameworks: {exc}"}


@_tool
def framework_decompose(project_code: str, framework: str,
                        source_keys: list[str],
                        baseline: dict | None = None) -> dict:
    """Decompose project material through one curated framework (extraction).

    `framework` is an id or slug (see framework_readiness). `source_keys`
    scope the corpus — REQUIRED and per-framework deliberate (decompose
    follows the corpus's dominant subject): each key resolves as a
    repo-relative file path under the tenant root, a spine element key, or a
    source-doc title, in that order. Returns {field_values, field_confidence,
    outcome, sources, usage}. Treat 'uncertain' fields as human-review items
    (they are usually the open decisions). A 'discarded' outcome means no
    curated template — that's a no-op, never write a substitute prompt.
    Pass `baseline` (a prior result's {field_values, field_confidence}) to
    get a `diff` — the pre/post decision record (changed/new/dropped fields
    plus confidence moves; a value that held while confidence hardened is a
    decision RATIFIED). Persists nothing. INTERNAL: results carry framework
    identity — keep them out of client-facing material.
    """
    from cp_engine.config import load as load_config
    from cp_engine.frameworks import assemble_corpus, get_catalog, make_llm
    from cp_engine.sync_mc2 import _load_ingest_creds
    from inbound_frameworks import decompose

    try:
        fw = get_catalog().get(framework)
        if fw is None:
            return {"error": f"no framework {framework!r} in the catalog"}
        if not fw.has_decompose_template:
            return {"note": f"{fw.id} has no curated decompose template yet — "
                            "snapshot refreshes are the only channel (anti-graveyard)"}
        if not source_keys:
            return {"error": "source_keys is required — scope the corpus per framework"}
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        root = _tenant_root()
        corpus, manifest = assemble_corpus(client, pid, cid, root, source_keys)
        if not corpus:
            return {"error": "no source_keys resolved to any text", "sources": manifest}
        _load_ingest_creds(load_config(root))  # ANTHROPIC_API_KEY into env
        d = decompose(fw, corpus, make_llm("decompose"))
        result = {
            "framework_id": fw.id,
            "outcome": d.get("outcome"),
            "field_values": d.get("field_values"),
            "field_confidence": d.get("field_confidence"),
            "sources": manifest,
            "usage": d.get("usage"),
        }
        if baseline is not None:
            from cp_engine.frameworks import diff_decompositions
            result["diff"] = diff_decompositions(baseline, result)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to decompose {framework!r} in {project_code!r}: {exc}"}


@_tool
def framework_compose(framework: str, field_values: dict,
                      target_element_type: str | None = None) -> dict:
    """Compose draft element content from human-confirmed framework field values.

    The generation half of the loop: run AFTER a human has reviewed the
    decompose output (especially 'uncertain' fields — they are usually
    undecided questions, not extraction noise). Returns {content, element_type,
    body, outcome, usage}; `content` is the package's `{sections:[...]}` and
    `body` is that content adapted to spine-element markdown, ready to pass
    straight to create_spine_element. Author it as a DRAFT — never
    auto-canonical, and the framework id goes in the version note only, never
    the element body or title. Composed text is already Element-language (the
    invariant is baked into the engine prompt).
    """
    from cp_engine.config import load as load_config
    from cp_engine.frameworks import get_catalog, make_llm
    from cp_engine.sync_mc2 import _load_ingest_creds
    from inbound_frameworks import compose

    try:
        fw = get_catalog().get(framework)
        if fw is None:
            return {"error": f"no framework {framework!r} in the catalog"}
        if not fw.has_compose_template:
            return {"note": f"{fw.id} has no curated compose template yet — "
                            "snapshot refreshes are the only channel (anti-graveyard)"}
        if not field_values:
            return {"error": "field_values is required — decompose (and review) first"}
        _load_ingest_creds(load_config(_tenant_root()))
        c = compose(fw, field_values, make_llm("compose"),
                    target_element_type=target_element_type)
        from cp_engine.frameworks import content_to_body
        return {
            "framework_id": fw.id,
            "outcome": c.get("outcome"),
            "content": c.get("content"),
            "body": content_to_body(c.get("content") or {}),
            "element_type": c.get("element_type"),
            "usage": c.get("usage"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to compose {framework!r}: {exc}"}


def _commitment_scope(client, project_code: str) -> dict | None:
    """Resolve a code to the commitments owner dict ``{"id", "code", "kind"}``.

    The commitments table needs to know WHICH owner column to use
    (``project_id`` vs ``initiative_id`` under the num_nonnulls==1 CHECK), so
    unlike ``_resolve`` this keeps the kind. Initiatives are checked FIRST:
    their bare slugs are unambiguous, and ``_resolve_project_id``'s own
    fallback can return initiative ids — after an initiatives miss, any
    project-branch hit is genuinely a project. Standalone repos have no owner
    column in mig-097 and resolve to None (a commitment must belong to an
    engagement or an initiative).
    """
    iid = _resolve_initiative_id(client, project_code)
    if iid is not None:
        return {"id": iid, "code": project_code, "kind": "initiative"}
    pid = _resolve_project_id(client, project_code)
    if pid is None:
        return None
    return {"id": pid, "code": project_code, "kind": "project"}


def _resolve_commitments(project_code: str):
    """Resolve a code for the commitment tools: ``(client, scope-or-None)``."""
    from cp_engine.config import load as load_config
    from cp_engine import mc2_db

    config = load_config(_tenant_root())
    client = mc2_db.get_client(config)
    return client, _commitment_scope(client, project_code)


@_tool
def create_commitment(project_code: str, description: str, owner: str = "",
                      due_date: str = "", direction: str = "internal") -> dict:
    """Register a dated commitment (who owes what by when) in MC-2's commitments store.

    Lands as a PROPOSAL (`date_status='proposed'`) with `source_kind='session'`
    — the same review gate the meeting auto-ingest path uses: the weekly dates
    loop ratifies the date and the Monday partners digest picks it up; nothing
    is auto-confirmed. `owner` is the person who owes it — an email address
    (stored as owner_email) or a display name. `direction` is one of
    us_to_them | them_to_us | internal. `due_date` must be ISO YYYY-MM-DD or
    empty (undated rows get flagged "needs a date" downstream — don't guess a
    date the humans never agreed). Idempotent on identical description text:
    re-creating returns "duplicate", including rows a human already dropped.
    """
    from cp_engine.commitments import (
        DIRECTIONS, INTERNAL, _valid_due_date, write_commitment,
    )
    from cp_engine.ingest import _content_hash

    try:
        direction = (direction or "").strip() or INTERNAL
        if direction not in DIRECTIONS:
            return {"error": f"direction must be one of {sorted(DIRECTIONS)}"}
        text = (description or "").strip()
        if not text:
            return {"error": "description is required"}
        due_iso = None
        if (due_date or "").strip():
            due_iso = _valid_due_date(due_date)
            if due_iso is None:
                return {"error": f"due_date {due_date!r} is not an ISO date "
                                 "(YYYY-MM-DD); omit it if no date was agreed"}
        client, scope = _resolve_commitments(project_code)
        if scope is None:
            return {"error": f"code {project_code!r} resolved to no engagement "
                             "or initiative (standalone repos can't own commitments)"}
        who = (owner or "").strip()
        owner_email = who if "@" in who else None
        owner_name = None if owner_email else (who or None)
        # Hash on the resolved owner id, not the caller's code string — the
        # same project reached via `ibx-5153` and `ibx-5153-ai-campaign` must
        # dedupe to one row.
        cp_hash = _content_hash(scope["id"], "create-commitment", text)
        outcome = write_commitment(
            client, owner=scope, description=text, cp_hash=cp_hash,
            source_kind="session", direction=direction,
            owner_email=owner_email, owner_name=owner_name, due_date=due_iso,
        )
        return {"result": outcome, "kind": scope["kind"], "cp_hash": cp_hash}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to create commitment in {project_code!r}: {exc}"}


@_tool
def list_commitments(project_code: str, status: str = "open") -> list[dict]:
    """List a project's or initiative's commitments (due-date ascending, undated last).

    `status` is one of open | done | dropped | all. The read side of the
    commitments store — use at wrap up to reconcile promised vs. delivered.
    `date_status` shows ratification state (proposed → agreed after two
    unchanged weekly posts; slipped = past due while still open) and
    `source_kind` tells session-authored rows from meeting-ingested ones.
    """
    from cp_engine import commitments as cm

    try:
        if status not in ("open", "done", "dropped", "all"):
            return [{"error": "status must be one of open|done|dropped|all"}]
        client, scope = _resolve_commitments(project_code)
        if scope is None:
            return [{"note": f"code {project_code!r} resolved to no engagement "
                             "or initiative"}]
        return cm.list_commitments(client, scope, status=status)
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"failed to list commitments for {project_code!r}: {exc}"}]


# NOTE (cp-engine #143): `resolve_commitment` — the wrap-up-sweep verb that
# closes an open commitment as `done` or `dropped` — now lives on the hosted MCP
# server (`cp-hosted` connector, same verb name), where the write carries the
# caller's identity and lands in the audit log. The `cp_engine.commitments`
# module stays: `close_commitment`/`find_open_commitment` remain the shared
# implementation, and close_out.py's checklist still points humans at the verb.


@_tool
def create_note(project_code: str, recipient: str, body: str,
                author: str = "") -> dict:
    """Leave a partner a Note against a project (in-app unread + Slack DM).

    The agent-session bridge to MC-2's Notes feature — use it to hand a partner
    a progress note / handoff at the end of session work (the same surface the
    dashboard's "leave a note" uses). `recipient` resolves to ONE person by
    display name (a distinct substring, e.g. "Marcello") or email; `author`
    defaults to the tenant's acting partner (Drew) and likewise resolves by
    name/email. `body` is markdown, document-sized (renders in-app; the Slack DM
    copy is truncated with a link to the full note). `project_code` is any
    engagement/initiative/repo code.

    The note lands `status='unread'` and is delivered as a Slack DM to the
    recipient; delivery is best-effort and never blocks the note (the return's
    `slack_delivery` is 'sent' | 'failed' | 'skipped' — 'skipped' means the note
    is in-app only, e.g. no Slack user for the recipient). Returns
    {note_id, recipient, author, slack_delivery[, slack_ts]}, or a {note}/{error}
    on a resolution miss. Author it when the human asks to notify/ping a partner
    — don't send unprompted.
    """
    from cp_engine.config import load as load_config
    from cp_engine.notes import write_note

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        config = load_config(_tenant_root())
        return write_note(
            client, config, project_code=project_code, project_id=pid,
            recipient=recipient, body=body, author=(author or None),
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to create note in {project_code!r}: {exc}"}


def run_stdio() -> None:
    """Run the server over stdio (what Claude Code launches via .mcp.json)."""
    mcp.run(transport="stdio")
