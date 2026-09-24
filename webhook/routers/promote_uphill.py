"""Promote-uphill route: copy a DECISION bullet one level up the tree.

WHY THIS EXISTS (cp-engine #304 follow-up, v0.124.1)
----------------------------------------------------
`promote_uphill` (#304) moves an item from a job to its program or account
— an explicit call, never inferred from content. A **commitment** is a row,
so the hosted server serves that path itself. A **decision** is a sprint-file
BULLET: reading the child's sprint files and writing the parent's needs a
checkout, and the hosted server holds a read-only deploy key by
construction. Until now it answered `unsupported_here` and named the CLI.

Only this service clones the tenant with a WRITE key, so the write happens
here — the same division `/api/sessions/capture` and
`/api/project-state/capture` state, and the same path, so no new trust is
minted:

    hosted promote_uphill  --caller's own JWT-->  mc-2
    mc-2                   --HMAC-------------->  HERE
    here                   --write deploy key-->  the repo

The bullet write reuses `cp_engine.promote_uphill.promote_decision` — the
CLI's own function — so the copy carries the standard `cp:hash` trailer,
lands under `### Decisions` in the parent's current calendar-week file, and
a second call is a no-op. The spine step on the parent's `Promoted uphill`
element is a Supabase write, not a file, and goes through the same code with
this service's client; with no client configured the bullet still lands and
the missing step is REPORTED in `step`, exactly as the CLI does.

SYNCHRONOUS, like the two capture routes: a file write plus a commit, no
LLM call, nothing to justify a 202 + run row + polling.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

import git_ops
import observability
import signatures
from fastapi import APIRouter, HTTPException, Request

from cp_engine import mc2_db

from .sessions import _SPARSE_PATHS as _CAPTURE_SPARSE_PATHS

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()

# The capture routes materialise the scope dirs + `.cp-engine` (the path
# index). A decision promotion also READS the child's sprint files and
# WRITES the parent's, so `sprints/` joins the cone.
_SPARSE_PATHS = (*_CAPTURE_SPARSE_PATHS, "sprints")

_ITEM_KINDS_SERVED = ("decision",)
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_COMMIT_EXCERPT_CHARS = 60
_MAX_REF_CHARS = 2_000
_MAX_NOTE_CHARS = 2_000

# Engine error prefixes that are the CALLER's to fix (400). Anything else
# the engine reports is a 500-class surprise and is raised as such, so it
# reaches Sentry instead of hiding behind a "bad request".
_CLIENT_ERROR_MARKERS = (
    "no parent",
    "no decision on",
    "is not in",  # `<code>` is not in .cp-engine/paths.json
    "item_ref is required",
    "no sprint file for parent",
    "item_kind must be",
)


def _commit_excerpt(text: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > _COMMIT_EXCERPT_CHARS:
        return flat[:_COMMIT_EXCERPT_CHARS].rstrip() + "…"
    return flat


@router.post("/api/promote-uphill")
async def promote_uphill(request: Request):
    """Copy a decision bullet into the parent's sprint file, commit, push.

    The server-side equivalent of `cxp promote-uphill <code> --decision
    <ref>`, for callers with no checkout.

    Request body (JSON):
        {
          "project_code": "ggl-5136-go-safety-website",  # required: the CHILD
          "item_kind": "decision",                        # required; only kind served
          "item_ref": "<cp:hash or exact bullet text>",   # required
          "note": "why it belongs one level up",          # optional; kept on the step
          "week": "2026-W39",                             # optional; parent file week
          "actor": "tony"                                 # optional; commit trailer
        }

    Headers:
        X-Webhook-Signature / X-Webhook-Timestamp (per the phased-rollout gate)

    Response (200):
        {"ok": true, "promoted": true, "already": false,
         "parent_code": "<code>", "sprint_path": "<repo-relative>",
         "commit": "<sha>", "level": {...}, "from": {...}, "to": {...},
         "cp_hash": "…", "source": "<child sprint path>", "step": {...}}

    An item already promoted returns 200 with `already: true`, no write and
    `commit: null` — matching the CLI, which prints "Already promoted" and
    exits 0. 400 when the code has no parent, is not in the index, or names
    no decision; 502 when the bullet was written but the push failed.
    """
    from cp_engine.promote_uphill import find_decision, promote_decision

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

    project_code = (payload.get("project_code") or "").strip()
    item_kind = (payload.get("item_kind") or "").strip().lower()
    item_ref = (payload.get("item_ref") or "").strip()
    note = (payload.get("note") or "").strip() or None
    week = (payload.get("week") or "").strip() or None
    actor = (payload.get("actor") or "").strip()

    if not project_code:
        raise HTTPException(status_code=400, detail="project_code is required")
    if item_kind not in _ITEM_KINDS_SERVED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"item_kind must be one of {list(_ITEM_KINDS_SERVED)} — a "
                "commitment is a row and is promoted where the row lives, not here"
            ),
        )
    if not item_ref:
        raise HTTPException(
            status_code=400, detail="item_ref is required: the bullet's cp:hash or exact text"
        )
    if len(item_ref) > _MAX_REF_CHARS:
        raise HTTPException(status_code=400, detail=f"item_ref exceeds {_MAX_REF_CHARS} characters")
    if note and len(note) > _MAX_NOTE_CHARS:
        raise HTTPException(status_code=400, detail=f"note exceeds {_MAX_NOTE_CHARS} characters")
    if week and not _WEEK_RE.match(week):
        raise HTTPException(status_code=400, detail="week must look like 2026-W39")

    # The step is a Supabase write; the bullet is not. No client → the bullet
    # still lands and `step` says the trail entry was not written (the CLI's
    # own contract), rather than refusing the promotion outright.
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning("promote-uphill: no MC-2 client — the parent's spine step will be skipped")

    with git_ops._cloned_tenant(sparse_paths=list(_SPARSE_PATHS)) as tenant_root:
        result = promote_decision(
            client,
            tenant_root=tenant_root,
            code=project_code,
            item_ref=item_ref,
            note=note,
            today=date.today(),
            week_iso=week,
        )

        if "error" in result:
            detail = str(result["error"])
            if any(marker in detail for marker in _CLIENT_ERROR_MARKERS):
                raise HTTPException(status_code=400, detail=detail)
            raise HTTPException(status_code=500, detail=detail)

        parent_code = result["to"]["code"]
        response = {**result, "parent_code": parent_code, "commit": None}

        if result.get("already"):
            log.info(
                "promote-uphill was a no-op: %s → %s (%s) already promoted",
                project_code, parent_code, result.get("sprint_path"),
            )
            return response

        found = find_decision(tenant_root, result["from"]["code"], result["item_ref"])
        excerpt = _commit_excerpt(found.text if found else result["item_ref"])
        message = f"[promote-uphill] {result['from']['code']} → {parent_code}: {excerpt}"
        if actor:
            message += f" ({actor})"

        try:
            sha = git_ops._commit_with_message_and_push(tenant_root, message)
        except Exception as exc:  # noqa: BLE001 — report, never 500 silently
            observability.capture(
                exc, area="promote_uphill", project_code=project_code
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"decision written to {result.get('sprint_path')} but the "
                    f"push failed: {exc} — re-sending this exact call is safe "
                    "(the copy is content-addressed and a second write is a "
                    "no-op); retry it"
                ),
            ) from exc

    response["commit"] = sha
    log.info(
        "promote-uphill landed: %s → %s (%s) by %s -> %s",
        project_code, parent_code, result.get("sprint_path"), actor or "?", sha,
    )
    return response
