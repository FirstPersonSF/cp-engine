"""Append one entry to the tenant's `improvements.md` (cp-engine #282).

WHY A WEBHOOK ROUTE. `improvements.md` is a tenant FILE, not an MC-2 row, so
the hosted MCP server — which holds a read-only deploy key by construction —
cannot write it. This service holds the write key, and the same delegation the
session-capture and project-state routes use applies: the caller's identity
arrives from mc-2 (derived from a verified token, never read from the body) and
names the commit.

THE FILE'S OWN PROTOCOL IS THE CONTRACT. Log at the moment of friction; never
delete; `sweep improvements` clusters entries into issues later. This route
appends and does nothing else — there is deliberately no edit or remove path,
because the harvest marks entries IN PLACE and a route that could rewrite one
could quietly rewrite the record of a decision.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request

import git_ops
import observability
import signatures

log = logging.getLogger(__name__)

router = APIRouter()

_IMPROVEMENTS = "improvements.md"


@router.post("/api/improvements/append")
async def append_improvement(request: Request):
    """Append `- <today> · \\`area\\` — <observation>` to the tenant log.

    Response (200): `{"ok": true, "entry": "...", "commit": "<sha>",
    "path": "improvements.md"}`.

    A duplicate entry (same area, same observation) returns `ok: true` with
    `commit: null` and `changed: false` — a retry after a timeout must not
    double-log, and re-posting must not manufacture a commit.

    400 on a malformed payload or an entry the format rejects.
    """
    from cp_engine.improvements import ImprovementsError, append_entry

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

    area = (payload.get("area") or "").strip()
    observation = (payload.get("observation") or "").strip()
    user = (payload.get("user") or "").strip()

    if not user:
        raise HTTPException(status_code=400, detail="user is required")

    # NO sparse_paths. `git sparse-checkout set` takes DIRECTORIES — passing a
    # filename fails hard ("improvements.md is not a directory") and 500s the
    # route. It is also unnecessary: cone mode materializes every root-level
    # file automatically, and `improvements.md` is one. Verified against a real
    # clone; every sibling caller passes `_SCOPE_DIRS`, i.e. directories.
    with git_ops._cloned_tenant() as tenant_root:
        path = tenant_root / _IMPROVEMENTS
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"{_IMPROVEMENTS} is not in the tenant tree",
            )

        try:
            updated, changed = append_entry(
                path.read_text(), area, observation, today=date.today()
            )
        except ImprovementsError as exc:
            # Caller error — correctable by rewording, so 400 rather than 500.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        entry = updated.rstrip("\n").splitlines()[-1]

        if not changed:
            # Already logged. Reporting ok with no commit is the honest answer:
            # the observation is in the file, and committing would put an empty
            # diff in the history of a file people read chronologically.
            log.info("improvements append was a duplicate: %s by %s", area, user)
            return {
                "ok": True,
                "changed": False,
                "entry": entry,
                "commit": None,
                "path": _IMPROVEMENTS,
            }

        path.write_text(updated)
        message = f"[improvements] {area} ({user})"
        try:
            sha = git_ops._commit_with_message_and_push(tenant_root, message)
        except Exception as exc:  # noqa: BLE001 — report, never 500 silently
            observability.capture(exc, area="improvements_append")
            raise HTTPException(
                status_code=502,
                detail=f"entry written but the push failed: {exc}",
            ) from exc

    log.info("improvements append landed: %s by %s -> %s", area, user, sha)
    return {
        "ok": True,
        "changed": True,
        "entry": entry,
        "commit": sha,
        "path": _IMPROVEMENTS,
    }
