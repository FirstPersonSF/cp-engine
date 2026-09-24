"""Session-capture route: land a session summary in the cp tenant.

WHY THIS EXISTS (cp-engine #247)
--------------------------------
A hosted-MCP user cannot leave a session record. `/cp-wrapup` and
`cxp capture-session` are LOCAL — they need a checkout — and the hosted
server deliberately holds a READ-ONLY deploy key, so it can never write to
the tenant. Measured 2026-09-14: Tony had 165 hosted calls and 134 writes
across 11 Google engagements, 1 git commit ever, and **0 session files**. Of
84 `sessions/` files tenant-wide, 83 were Drew's.

The durable CONTENT was never at risk — hosted writes land in MC-2 and
`cp-sync` commits them. What went missing was the session NARRATIVE, and
with it the `**Last session:**` line, which `sync._refresh_all_last_session_lines`
derives by globbing `**/sessions` **ON DISK**. That is the fact that decides
this design: a capture stored only as a DB row would never advance the line,
which is the whole point. **The session file has to reach the repo.**

Only this service clones the tenant with a WRITE key, so the write happens
here — exactly the division `/api/spine/promote` already states: "mc-2 has
the Supabase DB but NOT a checkout of the cp tenant filesystem — only this
service clones the tenant — so the markdown write (source of truth) + git
push happen HERE."

The full path, reusing the `promote_spine_transcript` precedent so no new
trust is minted:

    hosted capture_session  --caller's own JWT-->  mc-2
    mc-2                    --HMAC-------------->  HERE
    here                    --write deploy key-->  the repo

SYNCHRONOUS, unlike the promote routes. A capture is a file write plus a
commit — no LLM call, no ingest pipeline — so there is nothing to justify a
202 + run-row + polling. The caller gets the commit SHA or the failure.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import git_ops
import observability
import signatures
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()

# The tenant's top-level scope dirs. The clone is sparse — a session capture
# touches exactly one working dir, so there is no reason to materialize the
# whole tenant. Mirrors `spine._SCOPE_DIRS`.
_SCOPE_DIRS = ("1p", "firstpersonsf", "canonic")

# A summary must be real prose, not a placeholder. The floor is deliberately
# low (a terse but genuine capture is fine) and exists only to reject empty
# and whitespace-only bodies, which would otherwise produce a session file
# that advances the Last-session line while saying nothing.
_MIN_SUMMARY_CHARS = 20

# Guard against a runaway paste. Session summaries are narrative, not
# transcripts — the largest in the tenant today is well under this.
_MAX_SUMMARY_CHARS = 100_000


# The sparse clone materialises the scope dirs plus the engine's committed
# path index (`.cp-engine/paths.json`, #302) — the map every resolver reads
# before walking.
_SPARSE_PATHS = (*_SCOPE_DIRS, ".cp-engine")


def _resolve_working_dir(tenant_root, project_code: str):
    """The working dir for `project_code`, or None.

    Resolution is BY SEARCH, not by construction. `dir_slug()` would give the
    directory NAME, but not where in the tree it sits — engagement dirs are
    company-nested and, since #302, program-nested to any depth — and the
    tenant's own CLAUDE.md is explicit that the path must never be
    constructed. So: the engine's path index first, then a recursive walk of
    the scope dirs for a `cp.md` whose parent matches (a dir that has been
    re-homed is still found). The live tree is searched first, skipping
    `inactive/`; only when nothing live matches are the bins searched too,
    because capturing against a project that just went inactive is
    legitimate and refusing would lose the record.
    """
    from cp_engine.state import (
        SCOPE_DIRS,
        dir_slug,
        indexed_dir,
        iter_workstream_dirs,
    )

    tenant_root = Path(tenant_root)
    hit = indexed_dir(tenant_root, project_code)
    if hit is not None and (hit / "cp.md").is_file():
        return hit
    slug = dir_slug(project_code)
    for include_inactive in (False, True):
        for scope in SCOPE_DIRS:
            for candidate in iter_workstream_dirs(
                tenant_root / scope, include_inactive=include_inactive
            ):
                if candidate.name == slug and (candidate / "cp.md").is_file():
                    return candidate
    return None


@router.post("/api/sessions/capture")
async def sessions_capture(request: Request):
    """Write a session summary into a project's `sessions/`, commit, push.

    The server-side equivalent of `cxp capture-session --working-dir`, for
    callers with no checkout. Reuses the engine's own writers
    (`_write_session_file`, `refresh_last_session_line`) rather than
    reimplementing the filename convention or the forward-only date guard —
    two copies of those rules would drift, which is the #172/#178 lesson.

    Request body (JSON):
        {
          "project_code": "ggl-5151-grc-narrative",  # required
          "summary": "markdown body",                # required, real prose
          "user": "Tony",                            # required; names the file
          "when": "2026-09-14T16:39:00Z"             # optional, default now
        }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(...))
        X-Webhook-Timestamp: (optional, per the phased-rollout gate)

    Response (200):
        {"ok": true, "session_path": "<repo-relative>", "commit": "<sha>",
         "cp_md_updated": true|false}

    400 on a malformed or empty payload; 404 when no working dir resolves for
    the code. A capture that resolves but writes nothing is impossible — the
    session file is always new (the filename collision guard suffixes), so
    the empty-commit case that bit `/slack-action` (#237) cannot arise here.
    """
    from cp_engine.capture_session import (
        _write_session_file,
        refresh_last_session_line,
    )

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
    summary = (payload.get("summary") or "").strip()
    user = (payload.get("user") or "").strip()

    if not project_code:
        raise HTTPException(status_code=400, detail="project_code is required")
    if not user:
        raise HTTPException(status_code=400, detail="user is required")
    if len(summary) < _MIN_SUMMARY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"summary must be at least {_MIN_SUMMARY_CHARS} characters of "
                "real prose — an empty capture would advance the Last session "
                "line while saying nothing"
            ),
        )
    if len(summary) > _MAX_SUMMARY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"summary exceeds {_MAX_SUMMARY_CHARS} characters",
        )

    when_raw = payload.get("when")
    if when_raw:
        try:
            when = datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid `when`: {exc}"
            ) from exc
        # The filename carries local wall-clock, matching every existing
        # capture. An aware timestamp is normalized rather than rejected.
        if when.tzinfo is not None:
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        when = datetime.now()
    when = when.replace(microsecond=0)

    with git_ops._cloned_tenant(sparse_paths=list(_SPARSE_PATHS)) as tenant_root:
        working_dir = _resolve_working_dir(tenant_root, project_code)
        if working_dir is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no working dir for code {project_code!r} — it must name a "
                    "project, initiative, or standalone repo that exists in the "
                    "tenant tree"
                ),
            )

        summary_path = _write_session_file(
            working_dir=working_dir, summary_text=summary, user=user, when=when
        )
        # Derived, not authored (#81). Forward-only: a wrap-up-authored line
        # dated at or after this capture correctly survives.
        cp_md_updated = refresh_last_session_line(working_dir)

        rel = summary_path.relative_to(tenant_root).as_posix()
        message = (
            f"[session] {working_dir.name}: {user} "
            f"{when.strftime('%Y-%m-%d %H:%M')}"
        )
        try:
            sha = git_ops._commit_with_message_and_push(tenant_root, message)
        except Exception as exc:  # noqa: BLE001 — report, never 500 silently
            observability.capture(
                exc, area="sessions_capture", project_code=project_code
            )
            raise HTTPException(
                status_code=502,
                detail=f"session file written but the push failed: {exc}",
            ) from exc

    log.info(
        "session capture landed: %s (%s) by %s -> %s", project_code, rel, user, sha
    )
    return {
        "ok": True,
        "session_path": rel,
        "commit": sha,
        "cp_md_updated": cp_md_updated,
    }
