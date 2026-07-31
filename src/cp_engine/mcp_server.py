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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def list_spine_elements(project_code: str, layer: str = "",
                        scope: str = "", binding: str = "",
                        compact: bool = False) -> list[dict]:
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
    defaults for a first look; filter when the account dossiers and source
    stubs drown out the authored working set.
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
                          compact=compact)
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


@mcp.tool()
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
      new element (the rag_asset link `add_element_source` writes), so the card
      carries its own provenance. "Turn this ingested brief into a spine card."

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
        result = dict(created)
        if source_title:
            attached = add_element_source(
                project_code, created["element_id"], source_title,
            )
            result["source_attached"] = (
                attached.get("source") if "error" not in attached
                else f"(attach failed: {attached['error']})"
            )
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to add document to {project_code!r}: {exc}"}


@mcp.tool()
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


@mcp.tool()
def set_spine_element(project_code: str, key: str,
                      important: bool | None = None,
                      note: str | None = None,
                      layer: str | None = None,
                      framing: str | None = None,
                      serves: list[str] | None = None) -> dict:
    """Set `important`, `note`, `layer`, `framing` (title), and/or `serves`
    (work-item binding) on a spine element.

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
    normalized to the UI's canonical string. `framing` retitles the element (the
    est_item_id — the machine path — never changes, so existing keys keep
    working). `serves` rebinds the element to work-item ids (estimate
    activities/deliverables); pass `[]` to unbind — `binding` follows
    automatically ('live' when serves is non-empty, 'unbound' when empty).
    Layer, framing, and serves are element-level facts, so each is applied to
    EVERY version of the element — a partial write would scatter one element's
    history. Returns {est_item_id, important, note[, layer][, framing]
    [, serves][, promotion]}, or a structured {note}/{error} on miss.
    """
    from cp_engine.authored_element import canon_layer
    from cp_engine.project_sources import resolve_live_element
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, company_id = resolved
        row = resolve_live_element(client, pid, key, company_id)
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
        # layer / framing / serves describe the element as a whole (its kind,
        # its title, what it's bound to), so every version row moves together —
        # a partial move would scatter one element's history (#47).
        element_patch = {}
        canonical_layer = None
        if layer is not None:
            canonical_layer = canon_layer(layer)
            element_patch["layer"] = canonical_layer
        if framing is not None:
            element_patch["framing"] = framing
        if serves is not None:
            element_patch["serves"] = list(serves)
            # Same rule the authored-element builders use: bound ⇔ non-empty.
            element_patch["binding"] = "live" if serves else "unbound"
        if element_patch:
            (client.table(Tables.SPINE_SUBSTANCE).update(element_patch)
             .eq("project_id", row.get("project_id") or pid)
             .eq("est_item_id", row["est_item_id"]).execute())
        result = {
            "est_item_id": row["est_item_id"],
            "important": patch.get("important", row.get("important")),
            "note": patch.get("note", row.get("note")),
        }
        if canonical_layer is not None:
            result["layer"] = canonical_layer
        if framing is not None:
            result["framing"] = framing
        if serves is not None:
            result["serves"] = list(serves)
            result["binding"] = element_patch["binding"]
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
def add_element_source(project_code: str, key: str, source_title: str) -> dict:
    """Attach an ingested source document to a spine element.

    `key` resolves to ONE live element (est_item_id exact, or a unique
    case-insensitive title substring — same discipline as pull_spine_element);
    `source_title` resolves to ONE of the project's active ingested sources
    (see list_project_sources; exact title first, else a unique substring).
    Writes the typed link {"type": "rag_asset", id, title} into the element's
    `sources` on every version — the same write MC-2's dashboard performs —
    deduped by asset id (re-attaching is a no-op, `already: true`). Use it to
    close attach-as-source loops, e.g. an Agreement's signed SOW. Returns
    {est_item_id, source, attached, sources}, or a structured {note}/{error}.
    """
    from cp_engine.project_sources import modify_element_sources

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return modify_element_sources(client, pid, key, source_title,
                                      add=True, company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to attach '{source_title}' to '{key}' "
                         f"in {project_code!r}: {exc}"}


@mcp.tool()
def remove_element_source(project_code: str, key: str, source_title: str) -> dict:
    """Detach an ingested source document from a spine element.

    The inverse of add_element_source: resolves the element and the source the
    same way, then removes the matching {"type": "rag_asset", ...} link (by
    asset id) from every version's `sources`. Removing a source that isn't
    attached returns a structured note, not an error. Returns
    {est_item_id, source, removed, sources}, or {note}/{error}.
    """
    from cp_engine.project_sources import modify_element_sources

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return modify_element_sources(client, pid, key, source_title,
                                      add=False, company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to detach '{source_title}' from '{key}' "
                         f"in {project_code!r}: {exc}"}


@mcp.tool()
def add_spine_step(
    project_code: str, key: str, title: str,
    status: str = "upcoming", step_date: str | None = None,
    note: str | None = None,
) -> dict:
    """Append an ordered STEP to a spine element's progress trail (#119).

    A step is a lightweight marker of one move toward finishing the element
    (drafted -> ratified -> rewriting -> booked) — NOT a version, source, or
    body. `key` resolves to ONE live element (est_item_id exact, or a unique
    framing substring — same discipline as pull_spine_element). The step is
    appended at the end (position = max+1). `status` ∈ done|active|upcoming
    (default upcoming); `step_date` is free-form ('7/16', optional); `note` is a
    sentence or two (optional, ≤8000 chars). A step NEVER completes the
    work-item on the schedule — that stays human-confirmed. Returns
    {est_item_id, position, steps} or {error}. Author steps as the work moves.
    """
    from cp_engine import spine_steps

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return spine_steps.add_step(client, pid, key, title, status=status,
                                    step_date=step_date, note=note,
                                    company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to add step to '{key}' in "
                         f"{project_code!r}: {exc}"}


@mcp.tool()
def propose_spine_step(
    project_code: str, key: str, title: str,
    status: str = "done", step_date: str | None = None,
    note: str | None = None,
) -> dict:
    """PROPOSE a machine-authored step on an element's trail (auto-journey-steps).

    Author a step as work moves DURING a session — but it lands PROPOSED, not
    live: a human confirms or dismisses it on the spine trail (the review-gate).
    Use this (not `add_spine_step`, which writes a live human step) when YOU are
    recording progress you just made on a work-item, e.g. at the end of a
    content/synthesis session on an engagement.

    Contract (design 2026-07-21 §2): one MOVE = one step (not one edit); bind to
    exactly ONE element (`key` resolves like pull_spine_element — skip rather
    than guess if you can't attribute the work to a single element); prefer
    `status='done'` (the move already happened); a terse past-tense `title`
    (≤~60 chars, "Ratified the pillars", not "worked on pillars"). Cap yourself
    at ≤2 proposed steps per session across all elements.

    Idempotent: re-proposing the same (element, title, step_date) is a no-op in
    ANY review state — a confirmed or already-dismissed twin is not re-proposed.
    Returns {est_item_id, proposed: bool, already?: bool, steps} or {error}.
    A step NEVER completes the work-item on the schedule — that stays human-only.
    """
    from cp_engine import spine_steps

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return spine_steps.propose_step(client, pid, key, title, status=status,
                                        step_date=step_date, note=note,
                                        company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to propose step on '{key}' in "
                         f"{project_code!r}: {exc}"}


@mcp.tool()
def set_spine_step(
    project_code: str, key: str, step_id: str,
    title: str | None = None, status: str | None = None,
    step_date: str | None = None, note: str | None = None,
) -> dict:
    """Update one step on a spine element's trail (#119).

    Advance a step (`status` ∈ done|active|upcoming) or edit its title/step_date/
    note. `key` resolves the parent element; `step_id` picks the step. Only the
    fields you pass change (None = untouched — this verb never nulls a field).
    Returns {est_item_id, step_id, steps} or {error}. The common move is
    advancing a step to `done` as the work lands.
    """
    from cp_engine import spine_steps

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return spine_steps.set_step(client, pid, key, step_id, title=title,
                                    status=status, step_date=step_date,
                                    note=note, company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to update step {step_id!r} on '{key}' in "
                         f"{project_code!r}: {exc}"}


@mcp.tool()
def reorder_spine_step(project_code: str, key: str, order: list[str]) -> dict:
    """Reorder a spine element's steps (#119).

    `order` is the FULL list of the element's step_ids in the desired order;
    positions are renumbered 1..N to match. `key` resolves the parent element.
    Returns {est_item_id, steps} or {error}.
    """
    from cp_engine import spine_steps

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return spine_steps.reorder_steps(client, pid, key, order,
                                         company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to reorder steps on '{key}' in "
                         f"{project_code!r}: {exc}"}


@mcp.tool()
def remove_spine_step(project_code: str, key: str, step_id: str) -> dict:
    """Delete one step from a spine element's trail (#119).

    `key` resolves the parent element; `step_id` picks the step. Remaining steps
    densify to stay 1..N contiguous. Returns {est_item_id, removed, steps} or
    {error}.
    """
    from cp_engine import spine_steps

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return spine_steps.remove_step(client, pid, key, step_id,
                                       company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to remove step {step_id!r} on '{key}' in "
                         f"{project_code!r}: {exc}"}


@mcp.tool()
def add_element_provenance(project_code: str, key: str, source_key: str) -> dict:
    """Attach ANOTHER spine element as provenance to a spine element (#104).

    The tiering-rule move for "this synthesis card absorbs these raw cards":
    where add_element_source attaches an ingested rag_asset, this attaches a
    spine ELEMENT as provenance. `key` resolves to ONE live target element (the
    survivor); `source_key` resolves to ONE element that MAY ALREADY BE RETIRED
    (the folded-in raw material — the usual cleanup case). Writes the typed link
    {"type": "spine_element", id, title, retired} into the target's `sources` on
    every version, deduped by (type, id). Because the link is a property of the
    surviving card, it SURVIVES the source element's retirement — closing the
    lineage hole where retire-and-lose-the-link was the only option. Returns
    {est_item_id, source, attached, sources}, or a structured {note}/{error}.
    """
    from cp_engine.project_sources import modify_element_provenance

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return modify_element_provenance(client, pid, key, source_key,
                                         add=True, company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to attach element '{source_key}' to '{key}' "
                         f"in {project_code!r}: {exc}"}


@mcp.tool()
def remove_element_provenance(project_code: str, key: str, source_key: str) -> dict:
    """Detach a spine-element provenance link from a spine element (#104).

    The inverse of add_element_provenance: resolves the target and source the
    same way (source may be retired), then removes the matching
    {"type": "spine_element", ...} link (by element id) from every version's
    `sources`. Detaching one that isn't attached returns a structured note, not
    an error. Returns {est_item_id, source, removed, sources}, or {note}/{error}.
    """
    from cp_engine.project_sources import modify_element_provenance

    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return modify_element_provenance(client, pid, key, source_key,
                                         add=False, company_id=cid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to detach element '{source_key}' from '{key}' "
                         f"in {project_code!r}: {exc}"}


@mcp.tool()
def retire_spine_element(project_code: str, key: str) -> dict:
    """Retire a spine element — remove it from the live spine, keeping history.

    Use for duplicates and elements that no longer belong (e.g. the same source
    doc ingested twice). `key` is an est_item_id (exact) or a case-insensitive
    `framing` (title) substring resolved to ONE live element (same discipline
    as pull_spine_element). Every version of the element is marked
    `archived=true` and its live version is superseded, so it disappears from
    list/pull/resolve immediately and reaps from the repo mirror on next sync —
    the element itself is recoverable via a dashboard un-archive. Its typed
    edges (spine_relations) ARE deleted, not archived (#96) — a retired element
    must not leave `active` edges dangling from a dead endpoint. Returns
    {est_item_id, retired: true, edges_removed: int}, or {note}/{error} on miss.
    """
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        return _retire_one(client, pid, cid, key)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to retire '{key}' in {project_code!r}: {exc}"}


def _retire_one(client, pid: str, cid: str | None, key: str) -> dict:
    """Retire a single live element (the shared body of retire_spine_element and
    retire_spine_elements). Resolves `key`, archives every version, demotes the
    live row, and cascades the element's typed edges (#96). Returns
    {est_item_id, retired: true, edges_removed} or a {note} on a resolution miss.
    Raises are left to the caller's try/except boundary."""
    from cp_engine.project_sources import resolve_live_element

    row = resolve_live_element(client, pid, key, cid)
    if row is None:
        return {"note": f"no single live element matching '{key}'"}
    eid = row["est_item_id"]
    row_pid = row.get("project_id") or pid
    # Archive EVERY version (element-level retire), then demote the live
    # row(s). Two targeted updates, ordered so a failure between them leaves
    # the element archived (hidden from reads, #47's filter) rather than
    # superseded-but-unarchived with no live version.
    (client.table(Tables.SPINE_SUBSTANCE).update({"archived": True})
     .eq("project_id", row_pid).eq("est_item_id", eid).execute())
    (client.table(Tables.SPINE_SUBSTANCE).update({"status": "superseded"})
     .eq("project_id", row_pid).eq("est_item_id", eid)
     .eq("status", "live").execute())
    # Cascade to the element's typed edges (#96): archiving substance alone
    # left spine_relations rows `active` but dangling from a now-dead
    # endpoint — a silent graph corruption an agent would still walk. Delete
    # every edge on either side of this element. Edges key on est_item_id
    # (mig 117), so this is a straight two-sided delete; PostgREST has no OR
    # across columns, so run one delete per direction.
    edges_removed = 0
    for side in ("from_item_id", "to_item_id"):
        res = (client.table(Tables.SPINE_RELATIONS).delete()
               .eq("project_id", row_pid).eq(side, eid).execute())
        edges_removed += len(res.data or [])
    return {"est_item_id": eid, "retired": True, "edges_removed": edges_removed}


@mcp.tool()
def retire_spine_elements(project_code: str, keys: list[str]) -> dict:
    """Retire several spine elements in one call (#105) — batch cleanup.

    Each entry of `keys` resolves and retires exactly as retire_spine_element
    (archive every version, supersede the live row, cascade typed edges #96).
    A slot cleanup that collapses many raw cards is one operation instead of
    N. Per-key results are returned so a partial resolution miss doesn't fail
    the batch: `results` is a list of {key, est_item_id, retired, edges_removed}
    for hits and {key, note} for a key that resolved to no single live element.
    Returns {retired: int, edges_removed: int, results: [...]}, or {error}.
    """
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        results: list[dict] = []
        retired = 0
        edges_removed = 0
        for key in keys:
            try:
                r = _retire_one(client, pid, cid, key)
            except Exception as exc:  # noqa: BLE001 — one bad key must not abort the batch
                results.append({"key": key, "error": str(exc)})
                continue
            if r.get("retired"):
                retired += 1
                edges_removed += r.get("edges_removed", 0)
            results.append({"key": key, **r})
        return {"retired": retired, "edges_removed": edges_removed,
                "results": results}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to batch-retire in {project_code!r}: {exc}"}


# The closed relation vocabulary (mig 117's CHECK). Kept here as the MCP's own
# source of truth so a bad kind is rejected with a readable error before it ever
# reaches the DB CHECK (which would surface as an opaque 500).
_RELATION_KINDS = frozenset(
    {"responds_to", "supersedes", "derives_from", "informs", "contradicts"}
)


@mcp.tool()
def create_spine_relation(
    project_code: str, kind: str, from_key: str, to_key: str,
    note: str | None = None,
) -> dict:
    """Create a typed directed edge between two live spine elements (#97).

    `kind` is one of the closed vocabulary: responds_to | supersedes |
    derives_from | informs | contradicts. `from_key`/`to_key` each resolve to
    ONE live element the same way `pull_spine_element` resolves — an exact
    est_item_id or a distinct `framing` (title) substring. The edge is written
    live (`status='active'`, `source='manual'`) into spine_relations and keys on
    est_item_id (stable across version bumps), so the live version resolves at
    read time. Idempotent on the mig-117 unique constraint
    (project_id, kind, from, to) — a re-create of the same edge is a no-op.
    Returns {kind, from_item_id, to_item_id, created: bool}, or {note}/{error}.

    Authoring vocab (which edge for which change) lives in the synthesis-session
    protocol: responds_to = their voice reacting to ours; derives_from = built
    from named inputs; supersedes = a genuine fork (rare); informs = shaped but
    didn't generate; contradicts = conflicting claim.
    """
    from cp_engine.project_sources import resolve_live_element

    try:
        kind_n = (kind or "").strip().lower()
        if kind_n not in _RELATION_KINDS:
            return {"error": f"unknown relation kind {kind!r}; "
                             f"use one of {sorted(_RELATION_KINDS)}"}
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        src = resolve_live_element(client, pid, from_key, cid)
        if src is None:
            return {"note": f"no single live element matching from_key '{from_key}'"}
        dst = resolve_live_element(client, pid, to_key, cid)
        if dst is None:
            return {"note": f"no single live element matching to_key '{to_key}'"}
        from_eid, to_eid = src["est_item_id"], dst["est_item_id"]
        if from_eid == to_eid:
            return {"error": "an element cannot relate to itself"}
        # Idempotent: the mig-117 unique constraint means a duplicate edge is a
        # no-op, but check first so we return created=False rather than swallow a
        # constraint error.
        existing = (client.table(Tables.SPINE_RELATIONS).select("id")
                    .eq("project_id", pid).eq("kind", kind_n)
                    .eq("from_item_id", from_eid).eq("to_item_id", to_eid)
                    .limit(1).execute().data or [])
        if existing:
            return {"kind": kind_n, "from_item_id": from_eid,
                    "to_item_id": to_eid, "created": False}
        client.table(Tables.SPINE_RELATIONS).insert({
            "project_id": pid, "project_code": project_code, "kind": kind_n,
            "from_item_id": from_eid, "to_item_id": to_eid,
            "status": "active", "source": "manual",
            "note": note, "created_by": "cp-sources",
        }).execute()
        return {"kind": kind_n, "from_item_id": from_eid,
                "to_item_id": to_eid, "created": True}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to create relation in {project_code!r}: {exc}"}


@mcp.tool()
def retire_spine_relation(
    project_code: str, kind: str, from_key: str, to_key: str,
) -> dict:
    """Delete a typed edge between two spine elements (#97).

    The inverse of `create_spine_relation`: resolves `from_key`/`to_key` to live
    elements (or accepts raw est_item_ids for edges whose endpoint is already
    retired), then deletes the matching `kind` edge from spine_relations. Use to
    fix a mis-recorded edge (e.g. a wrong `supersedes` that should be
    `responds_to`). Returns {kind, from_item_id, to_item_id, removed: int}, or
    {note}/{error}.

    Resolution tolerates a dead endpoint: if `from_key`/`to_key` doesn't resolve
    to a LIVE element it is used verbatim as an est_item_id, so an orphaned edge
    left by an older retire can still be cleaned by passing the raw ids.
    """
    from cp_engine.project_sources import resolve_live_element

    try:
        kind_n = (kind or "").strip().lower()
        if kind_n not in _RELATION_KINDS:
            return {"error": f"unknown relation kind {kind!r}; "
                             f"use one of {sorted(_RELATION_KINDS)}"}
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, cid = resolved
        src = resolve_live_element(client, pid, from_key, cid)
        dst = resolve_live_element(client, pid, to_key, cid)
        # Fall back to the raw key as an est_item_id so orphaned edges (endpoint
        # already retired) remain cleanable.
        from_eid = src["est_item_id"] if src else from_key
        to_eid = dst["est_item_id"] if dst else to_key
        res = (client.table(Tables.SPINE_RELATIONS).delete()
               .eq("project_id", pid).eq("kind", kind_n)
               .eq("from_item_id", from_eid).eq("to_item_id", to_eid)
               .execute())
        removed = len(res.data or [])
        if removed == 0:
            return {"note": f"no {kind_n} edge {from_eid} -> {to_eid} to remove"}
        return {"kind": kind_n, "from_item_id": from_eid,
                "to_item_id": to_eid, "removed": removed}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to retire relation in {project_code!r}: {exc}"}


@mcp.tool()
def promote_stakeholder(project_code: str, key: str) -> dict:
    """Promote a project's stakeholder element to ACCOUNT scope.

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


@mcp.tool()
def demote_stakeholder(project_code: str, key: str) -> dict:
    """Remove an element from ACCOUNT scope — the inverse of promote_stakeholder.

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


@mcp.tool()
def set_element_account_scope(
    project_code: str, key: str, account: bool = True,
) -> dict:
    """Tag ANY spine element account-level (or return it to project scope).

    The type-agnostic generalization of `promote_stakeholder`/`demote_stakeholder`:
    use it to make a synthesis, a source, a decision — any element, not just a
    stakeholder — readable from EVERY project of the same company (`account=True`,
    scope='account', company_id set, provenance project unchanged), or to pull it
    back to its home project (`account=False`). Every version of the element
    moves together. Engagements only (initiatives have no company). Opt-in and
    human-triggered — engagement-specific content that shouldn't travel belongs
    in a project-scoped element. Returns
    {est_item_id, scope, company_id?, returned_to_project_id?}, or {note}/{error}.

    (For a stakeholder specifically, `promote_stakeholder` is the same move with a
    layer sanity-check — prefer it there; this is for everything else.)
    """
    if account:
        return promote_stakeholder(project_code, key)
    return demote_stakeholder(project_code, key)


@mcp.tool()
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
    copy to any local element it builds on with `create_spine_relation`.

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
            promoted = set_element_account_scope(to_code, created["element_id"], True)
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def resolve_commitment(project_code: str, key: str, outcome: str = "done") -> dict:
    """Close an OPEN commitment: `outcome` 'done' (delivered) or 'dropped' (no longer owed).

    `key` is a commitment id from list_commitments or a distinct substring of
    its description; an ambiguous key returns the candidates instead of
    guessing. Commitments are never deleted — a dropped row stays as the
    archive and keeps re-ingests of the same meeting from resurrecting it.
    The wrap-up-sweep verb, mirroring weekly-cp.md's `[resolved: ...]` markers.
    """
    from cp_engine.commitments import close_commitment, find_open_commitment

    try:
        if outcome not in ("done", "dropped"):
            return {"error": "outcome must be 'done' or 'dropped'"}
        client, scope = _resolve_commitments(project_code)
        if scope is None:
            return {"error": f"code {project_code!r} resolved to no engagement "
                             "or initiative"}
        row, err = find_open_commitment(client, scope, key)
        if err:
            return {"error": err}
        close_commitment(client, row["id"], outcome)
        return {"resolved": row["id"], "description": row.get("description"),
                "outcome": outcome}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to resolve commitment in {project_code!r}: {exc}"}


@mcp.tool()
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
