"""Project-state route: merge Exec Summary fields into a project's cp.md.

WHY THIS EXISTS (cp-engine #251)
--------------------------------
The Exec Summary is the most-read surface in the system and the least-written
one. Six independent consumers treat it as durable project truth — the
master-CP one-liner (`summary.derive_from_project_cp`), the agenda, the
planning bundle, `cxp brief`, the lint, and hosted MCP's own
`get_project_state`. It has three writers, and **none of them author prose**:
a one-time migration, the `**Last session:**` line, and a manual escape hatch.

Measured 2026-09-14: of 140 exec-summary rewrites in 90 days, 131 were one
person. Thirteen of twenty-three engagements carried summaries 30–62 days
stale while ~7MB of meeting, sprint and spine content accumulated in surfaces
no index reads.

That is not a discipline problem. `docs/plans/2026-06-30-exec-summary.md` drew
a deliberate seam — the engine owns *scaffold + read + render*, the **model**
owns *all prose* — and it assumed the model would be invoked at wrap-up, per
project, reliably. The move to hosted MCP then put the model somewhere that
**cannot write a tenant file**: the hosted server holds a read-only deploy key
by construction. The model owns the prose, works from hosted, and cannot put
the prose where it belongs. This route is the missing half.

It is NOT a reversal of the cutover. Nothing here generates text — every value
written arrives from the caller. The engine still only decides where it lands.

PER-FIELD, NOT WHOLE-REGION. Measured over 60 days, of 45 exec-summary
rewrites, 26 touched exactly ONE field. A whole-region verb would make that
58% case the destructive one — a caller sending only Status silently blanks
the other seven fields. See `cp_engine.exec_summary_merge` for the full
argument and the concurrency consequence.

The path mirrors `/api/sessions/capture` exactly, so no new trust is minted:

    hosted capture_project_state  --caller's own JWT-->  mc-2
    mc-2                          --HMAC------------->  HERE
    here                          --write deploy key->  the repo

SYNCHRONOUS. A merge is a file write plus a commit — no LLM call, no ingest
pipeline — so there is nothing to justify a 202 + run-row + polling.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import git_ops
import observability
import signatures
from fastapi import APIRouter, HTTPException, Request

from .sessions import _SCOPE_DIRS, _resolve_working_dir

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()

# A field value must be real prose. The floor is deliberately low — a terse
# but genuine Status is fine — and exists only to reject empty and
# whitespace-only values, which would blank a field while reporting success.
_MIN_FIELD_CHARS = 3

# Guard against a runaway paste. The Exec Summary is a summary; the lint
# already warns well below this.
_MAX_FIELD_CHARS = 20_000


@router.post("/api/project-state/capture")
async def project_state_capture(request: Request):
    """Merge named Exec Summary fields into a project's cp.md, commit, push.

    The server-side equivalent of a human editing between the exec-summary
    markers at wrap-up, for callers with no checkout. Reuses the engine's own
    `merge_exec_summary_fields` rather than reimplementing the field grammar —
    two copies of that rule would drift, which is the #172/#178 lesson.

    Request body (JSON):
        {
          "project_code": "ggl-5136-go-safety-website",   # required
          "fields": {                                      # required, >=1
            "Status": "Medium regional migrations next.",
            "Where it stands": ["Migrations underway.", "Launch at risk."]
          },
          "user": "Tony"                                   # required; commit trailer
        }

    A field value is a string (inline field) or a list of strings (bulleted).
    **Fields not named are left exactly as they were** — that is the point.

    Headers:
        X-Webhook-Signature / X-Webhook-Timestamp (per the phased-rollout gate)

    Response (200):
        {"ok": true, "changed": ["Status"], "commit": "<sha>",
         "cp_md_path": "<repo-relative>"}

    A merge that changes nothing returns `ok: true` with `changed: []` and
    **no commit** — re-sending identical content must not manufacture
    freshness, since the `· updated` stamp is what the staleness check reads.

    400 on a malformed payload or an unknown field label; 404 when no working
    dir resolves for the code.
    """
    from cp_engine.exec_summary_merge import (
        ExecSummaryMergeError,
        merge_exec_summary_fields,
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
    user = (payload.get("user") or "").strip()
    fields = payload.get("fields")

    if not project_code:
        raise HTTPException(status_code=400, detail="project_code is required")
    if not user:
        raise HTTPException(status_code=400, detail="user is required")
    if not isinstance(fields, dict) or not fields:
        raise HTTPException(
            status_code=400,
            detail="fields must be a non-empty object of {label: value}",
        )

    cleaned = _validate_fields(fields)

    with git_ops._cloned_tenant(sparse_paths=list(_SCOPE_DIRS)) as tenant_root:
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

        cp_md = working_dir / "cp.md"
        try:
            merged, changed = merge_exec_summary_fields(
                cp_md.read_text(), cleaned, today=date.today()
            )
        except ExecSummaryMergeError as exc:
            # Caller error (unknown label, or a region sync has not scaffolded
            # yet). Correctable by the caller, so 400 rather than 500.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        rel = cp_md.relative_to(tenant_root).as_posix()

        if not changed:
            # Nothing to commit. Reporting ok:true with an empty `changed` is
            # the honest answer: the caller's content is already what the file
            # says. Committing here would advance the freshness stamp on a
            # summary nobody actually refreshed — the failure #249 measures.
            log.info(
                "project-state capture was a no-op: %s (%s) by %s",
                project_code, rel, user,
            )
            return {"ok": True, "changed": [], "commit": None, "cp_md_path": rel}

        cp_md.write_text(merged)

        message = f"[project-state] {working_dir.name}: {', '.join(changed)} ({user})"
        try:
            sha = git_ops._commit_with_message_and_push(tenant_root, message)
        except Exception as exc:  # noqa: BLE001 — report, never 500 silently
            observability.capture(
                exc, area="project_state_capture", project_code=project_code
            )
            raise HTTPException(
                status_code=502,
                detail=f"cp.md written but the push failed: {exc}",
            ) from exc

    log.info(
        "project-state capture landed: %s (%s) fields=%s by %s -> %s",
        project_code, rel, list(changed), user, sha,
    )
    return {
        "ok": True,
        "changed": list(changed),
        "commit": sha,
        "cp_md_path": rel,
    }


def _validate_fields(fields: dict) -> dict[str, str | list[str]]:
    """Normalize and bounds-check the caller's field mapping.

    Label VALIDITY is not checked here — `merge_exec_summary_fields` owns the
    set of real field names and raises on an unknown one. Duplicating that
    list would be a second source of truth for what a field is.
    """
    cleaned: dict[str, str | list[str]] = {}
    for label, value in fields.items():
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(status_code=400, detail="field labels must be strings")
        label = label.strip()

        if isinstance(value, str):
            text = value.strip()
            if len(text) < _MIN_FIELD_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"field {label!r} must carry real prose — an empty value "
                        "would blank the field while reporting success"
                    ),
                )
            if len(text) > _MAX_FIELD_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail=f"field {label!r} exceeds {_MAX_FIELD_CHARS} characters",
                )
            cleaned[label] = text
            continue

        if isinstance(value, list):
            # An EMPTY list is legitimate — it clears a bulleted field, which
            # is how "Blockers: none any more" is expressed. Only the items
            # themselves are bounds-checked.
            items = []
            for item in value:
                if not isinstance(item, str):
                    raise HTTPException(
                        status_code=400,
                        detail=f"field {label!r} bullets must be strings",
                    )
                text = item.strip()
                if not text:
                    continue
                if len(text) > _MAX_FIELD_CHARS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"a bullet in {label!r} exceeds {_MAX_FIELD_CHARS}",
                    )
                items.append(text)
            cleaned[label] = items
            continue

        raise HTTPException(
            status_code=400,
            detail=(
                f"field {label!r} must be a string (inline) or a list of "
                "strings (bulleted)"
            ),
        )
    return cleaned
