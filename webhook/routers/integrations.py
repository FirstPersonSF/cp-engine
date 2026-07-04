"""Integration routes: fathom tag resolution; ClickUp close round-trip.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import git_ops
import pipeline
import signatures
from fastapi import APIRouter, HTTPException, Request, Response

from cp_engine import mc2_db
from cp_engine.ingest import IngestPlanError, execute_plan
from cp_engine.mc2_db import Tables

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


@router.post("/api/resolve-tags")
async def resolve_tags_endpoint(request: Request) -> dict:
    """Resolve fathom display tags to canonical cp project codes.

    THE resolution authority (arch-phase-2): fathom-meeting-sync calls this
    at dispatch time instead of maintaining its own ``projectTagToCode``
    parse. Wraps :func:`cp_engine.tag_resolve.resolve_tags` — the parse
    heuristic plus a DB-backed verification against MC-2
    ``projects``/``initiatives``.

    Request body (JSON):
        { "tags": ["GGL 5136 go/safety website", "mission-control", ...] }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(body, WEBHOOK_HMAC_SECRET))
        X-Webhook-Timestamp: optional, same scheme as the ingest routes

    Response:
        { "resolutions": [ {"tag", "code", "kind", "matched"}, ... ] }

    ``code`` is None for unresolvable tags; ``matched=False`` means the
    code came from the string parse without a live MC-2 row (historical /
    archived projects — still routable, same as fathom's old local parse).
    """
    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    tags = payload.get("tags")
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list")
    if len(tags) > 200:
        raise HTTPException(status_code=400, detail="too many tags (max 200)")

    from cp_engine.tag_resolve import resolve_tags

    client = pipeline._create_supabase_client()
    return {"resolutions": resolve_tags(client, tags)}


@router.post("/clickup-task-closed")
async def clickup_task_closed(request: Request):
    """ClickUp webhook: a task's status changed. If the new status is in
    the `closed` family AND the task was created from a cp ask, flip the
    matching cp ask to `[closed]`.

    Routing: webhook payload carries `task_id`. The dashboard stored
    `clickup_task_id` on the proposal row at task-creation time, so we
    look up the cp_ask_hash + project code from clickup_task_proposals.
    No need to fetch the task description from ClickUp's API.

    ClickUp's `taskStatusUpdated` event fires on ANY status change.
    The payload's ``history_items`` can carry MULTIPLE changes per
    delivery (e.g. an assignee change + a status flip in the same
    update). Pre-fix, we only inspected ``history_items[0]``; if the
    assignee change happened to land first, the close transition in
    ``[1]`` was silently ignored. Now we iterate and look for the
    first ``field=='status'`` item with ``after.type=='closed'``.

    Returns:
      200 success: {matched_hash, code, commit_sha, ingested}
      204 not-closed: no item in history transitions to a closed status
      200 orphan: {ingested: false, reason: "no_proposal_row"}
      401: bad HMAC
      500: missing secret env var
    """
    raw_body = await request.body()
    signatures._verify_clickup_signature(raw_body, request.headers.get("x-signature", ""))

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    task_id = payload.get("task_id")
    if not task_id:
        log.info("clickup-task-closed: no task_id in payload; ignoring")
        return Response(status_code=204)

    # Filter for actual close events. taskStatusUpdated fires on any
    # status change AND can pack multiple changes (assignee, priority,
    # status) into one event. Walk every item and look for a status
    # transition INTO the `closed` family.
    history = payload.get("history_items") or []
    if not history:
        log.info(
            "clickup-task-closed: no history_items for task=%s; ignoring",
            task_id,
        )
        return Response(status_code=204)

    close_item = None
    for item in history:
        if not isinstance(item, dict):
            continue
        # The `field` key tells us which attribute changed. We only
        # care about status. Old payloads sometimes omit field entirely
        # and only ever carry status changes — in that case we fall
        # back to the after.type test alone.
        field = item.get("field")
        if field is not None and field != "status":
            continue
        after = item.get("after") or {}
        if isinstance(after, dict) and after.get("type") == "closed":
            close_item = item
            break

    if close_item is None:
        # Diagnostic: which fields did we see, so we can tell a no-op
        # (only assignee changed) from a schema drift (status item
        # present but type label changed).
        fields_seen = [
            (i.get("field"), (i.get("after") or {}).get("type"))
            for i in history if isinstance(i, dict)
        ]
        log.info(
            "clickup-task-closed: task=%s no closed transition in history (%s); ignoring",
            task_id, fields_seen,
        )
        return Response(status_code=204)

    lookup = _lookup_proposal_by_clickup_task_id(task_id)
    if lookup is None:
        log.warning("clickup-task-closed: no proposal row for clickup_task_id=%s", task_id)
        return {"task_id": task_id, "ingested": False, "reason": "no_proposal_row"}

    cp_hash, code = lookup

    # Build a close-ask plan keyed on the hash marker. _write_close_ask
    # does a substring search inside the open bullet, so the hash comment
    # is a unique, durable anchor — won't false-match against any other
    # bullet in the file.
    plan = {
        "projects": {
            code: {
                "close-ask": [
                    {"match": f"<!-- cp:hash={cp_hash} -->", "closed_by": "clickup"}
                ]
            }
        },
    }

    with git_ops._cloned_tenant() as tenant_root:
        config = pipeline._load_tenant_config(tenant_root)
        try:
            # close-ask plan — no ClickUp-proposal verbs in scope, but pass
            # the client for parity (cheap, and future-safe).
            result = execute_plan(
                plan,
                tenant_root=config.root,
                today=datetime.now().date(),
                supabase=pipeline._create_supabase_client(),
                meeting_id=None,
            )
        except IngestPlanError as exc:
            log.warning("clickup-task-closed: execute_plan failed: %s", exc)
            return {
                "task_id": task_id,
                "matched_hash": cp_hash,
                "code": code,
                "ingested": False,
                "reason": f"execute_plan_failed: {exc}",
            }

        if not result.files_written:
            # No-op OR errors-without-writes: the ask was already closed,
            # or the hash marker isn't present in any open bullet. Either
            # way, nothing to commit — and we must NOT return ingested=True
            # because the ClickUp end may use that signal to stop retrying.
            log.info(
                "clickup-task-closed: no files written for code=%s hash=%s errors=%s",
                code, cp_hash, result.errors,
            )
            response: dict = {
                "task_id": task_id,
                "matched_hash": cp_hash,
                "code": code,
                "ingested": False,
                "reason": "no_change" if not result.errors else "no_files_written",
            }
            if result.errors:
                response["errors"] = result.errors
            return response

        commit_sha = git_ops._commit_clickup_close(
            tenant_root=tenant_root, code=code, cp_hash=cp_hash,
        )
        log.info(
            "clickup-task-closed: ingested code=%s hash=%s commit=%s",
            code, cp_hash, commit_sha,
        )
        return {
            "task_id": task_id,
            "matched_hash": cp_hash,
            "code": code,
            "commit_sha": commit_sha,
            "ingested": commit_sha is not None,
        }


def _lookup_proposal_by_clickup_task_id(task_id: str) -> tuple[str, str] | None:
    """Return (cp_ask_hash, code) for a given ClickUp task_id.

    Resolves the owning cp code from whichever owner column is set on the
    ``clickup_task_proposals`` row (post-migration 081 the table carries
    BOTH ``project_id`` and ``initiative_id`` with a num_nonnulls == 1
    CHECK — exactly one owner):

      - ``project_id`` set → ``<company>-<number>`` via projects → companies.
      - ``initiative_id`` set → the initiative's slug ``code`` directly.

    Returns None if no row matches, neither owner resolves, or Supabase is
    unavailable. Best-effort: any exception is swallowed and treated as
    'not found' (the webhook returns `ingested: false`).
    """
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning(
            "clickup-task-closed: Supabase unavailable (env not set or "
            "supabase package not installed); can't look up task_id"
        )
        return None
    try:
        resp = (
            client.table(Tables.CLICKUP_TASK_PROPOSALS)
            .select("cp_ask_hash, project_id, initiative_id")
            .eq("clickup_task_id", task_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        row = rows[0]
        cp_hash = row.get("cp_ask_hash")
        if not cp_hash:
            log.warning(
                "clickup-task-closed: proposal for task=%s has no cp_ask_hash",
                task_id,
            )
            return None

        initiative_id = row.get("initiative_id")
        if initiative_id:
            code = _resolve_initiative_code(client, initiative_id)
            if code:
                return cp_hash, code
            log.warning(
                "clickup-task-closed: no initiative code for id=%s (task=%s)",
                initiative_id, task_id,
            )
            return None

        project_id = row.get("project_id")
        if project_id:
            code = _resolve_engagement_code(client, project_id)
            if code:
                return cp_hash, code
            log.warning(
                "clickup-task-closed: no engagement code for project_id=%s (task=%s)",
                project_id, task_id,
            )
            return None

        log.warning(
            "clickup-task-closed: proposal for task=%s has no owner", task_id
        )
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "clickup-task-closed: task lookup failed for %s: %s", task_id, exc
        )
        return None


def _resolve_engagement_code(client, project_id: str) -> str | None:
    """Resolve a projects.id to its ``<company>-<number>`` engagement code."""
    resp = (
        client.table(Tables.PROJECTS)
        .select("number, company_id")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    proj = rows[0]
    number = proj.get("number")
    company_id = proj.get("company_id")
    if number is None or not company_id:
        return None
    cresp = (
        client.table(Tables.COMPANIES)
        .select("code")
        .eq("id", company_id)
        .limit(1)
        .execute()
    )
    crows = cresp.data or []
    if not crows:
        return None
    company_code = (crows[0].get("code") or "").lower()
    if not company_code:
        return None
    return f"{company_code}-{number}"


def _resolve_initiative_code(client, initiative_id: str) -> str | None:
    """Resolve an initiatives.id to its slug ``code``."""
    resp = (
        client.table(Tables.INITIATIVES)
        .select("code")
        .eq("id", initiative_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    return rows[0].get("code") or None
