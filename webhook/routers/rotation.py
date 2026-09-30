"""Word-count rotation route: roll aged Exec Summary Updates into the archive.

WHY THIS EXISTS (cp-engine #280, tier 3)
----------------------------------------
Word-count rotation was the last wrap-up step a hosted session could only
REPORT (`word_count_check`) and never perform: it moves text between two
files in one commit, and the hosted server holds no write key by
construction. This route performs it upstream, under the caller's name, on
the same path every other hosted write takes — so no new trust is minted:

    hosted rotate_word_count  --caller's own JWT-->  mc-2
    mc-2                      --HMAC------------->  HERE
    here                      --write deploy key->  the repo

What moves, and what deliberately does not, is `cp_engine.cp_rotation`'s
call: Updates entries older than 28 days leave `cp.md` verbatim for
`cp-archive-<YYYY-MM>.md` beside it. Hand-written sections never move.

NO-LOSS BEFORE PUSH. The engine checks its plan in memory before writing.
This route checks again against what is ON DISK, comparing the clone's
`HEAD` to the working tree the way `cxp merge-check` compares a ref to the
tree — and additionally refuses if anything other than the two files
changed. A single lost line is a 409 and nothing is committed; the clone is
temporary, so a refusal leaves the tenant exactly as it was.

ONE COMMIT covering both files, naming the caller. An honest no-op — nothing
past age — returns `ok: true, changed: false, commit: null` and commits
nothing (#249: a commit nobody's content required is manufactured activity).

SYNCHRONOUS, like `/api/project-state/capture`: a file move plus a commit.
"""

from __future__ import annotations

import json
import logging
import subprocess

import git_ops
import observability
import signatures
from fastapi import APIRouter, HTTPException, Request

from .sessions import _SPARSE_PATHS, _resolve_working_dir
from cp_engine.clock import tenant_today

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


def _changed_paths(tenant_root) -> set[str]:
    """Repo-relative paths `git status` reports as changed or new."""
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tenant_root, check=True, capture_output=True, text=True,
    ).stdout
    return {line[3:].strip().strip('"') for line in out.splitlines() if line.strip()}


@router.post("/api/word-count/rotate")
async def rotate_word_count(request: Request):
    """Rotate one project's aged Exec Summary Updates into its archive file.

    Request body (JSON):
        {"project_code": "1pi-9005-mission-control",   # required
         "user": "Tony"}                               # required; names the commit

    Headers: X-Webhook-Signature / X-Webhook-Timestamp.

    Response (200):
        {"ok": true, "changed": true, "commit": "<sha>",
         "cp_md_path": "...", "archive_path": "...",
         "moved": [{"date", "headline"}...], "older_than": "YYYY-MM-DD",
         "words_before": N, "words_after": N,
         "over_audit_threshold": bool, "over_rotation_threshold": bool}

    Nothing past age: the same shape with `changed: false`, `moved: []`,
    `commit: null`. 400 when the cp.md has no exec-summary region or Updates
    field; 404 when no working dir resolves; 409 when the no-loss check
    refuses (nothing is pushed); 502 when the push fails.
    """
    from cp_engine.cp_rotation import (
        RotationError,
        RotationLossError,
        check_rotation_loss,
        rotate_cp,
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
    if not project_code:
        raise HTTPException(status_code=400, detail="project_code is required")
    if not user:
        raise HTTPException(status_code=400, detail="user is required")

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

        try:
            plan = rotate_cp(working_dir, today=tenant_today())
        except RotationLossError as exc:
            # The in-memory check refused; nothing was written.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RotationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        rel_cp = (working_dir / "cp.md").relative_to(tenant_root).as_posix()
        rel_archive = (working_dir / plan.archive_name).relative_to(tenant_root).as_posix()
        report = {
            "cp_md_path": rel_cp,
            "archive_path": rel_archive,
            **plan.summary(),
        }

        if not plan.changed:
            log.info("rotation was a no-op: %s (%s) by %s", project_code, rel_cp, user)
            return {"ok": True, "commit": None, **report}

        # THE ON-DISK CHECK. Independent of the engine's in-memory one: this
        # reads what was actually written and compares it to HEAD.
        lost, unexpected = check_rotation_loss(
            tenant_root, [rel_cp, rel_archive], plan.scaffold_lines
        )
        stray = _changed_paths(tenant_root) - {rel_cp, rel_archive}
        if lost or unexpected or stray:
            detail = (
                f"rotation refused before push — lost {len(lost)} line(s), "
                f"{len(unexpected)} unexpected, {len(stray)} other file(s) touched"
            )
            if lost:
                detail += f"; first lost: {lost[0][:160]!r}"
            if stray:
                detail += f"; other files: {sorted(stray)[:5]}"
            log.error("%s: %s (%s) by %s", detail, project_code, rel_cp, user)
            raise HTTPException(status_code=409, detail=detail)

        n = len(plan.moved)
        message = (
            f"[rotate] {working_dir.name}: {n} Update{'s' if n != 1 else ''} "
            f"→ {plan.archive_name} ({user})\n\n"
            f"Rolled off Exec Summary Updates older than {plan.threshold.isoformat()}; "
            f"authored words {plan.words_before} → {plan.words_after}.\n\n"
            f"Requested-by: {user}"
        )
        try:
            sha = git_ops._commit_with_message_and_push(tenant_root, message)
        except Exception as exc:  # noqa: BLE001 — report, never 500 silently
            observability.capture(exc, area="word_count_rotate", project_code=project_code)
            raise HTTPException(
                status_code=502,
                detail=(
                    f"rotation written but the push failed: {exc} — retrying is "
                    "safe: a retry re-plans from the tenant's tip, and entries "
                    "already moved are not moved twice"
                ),
            ) from exc

    log.info(
        "rotation landed: %s (%s) moved=%d by %s -> %s",
        project_code, rel_cp, n, user, sha,
    )
    return {"ok": True, "commit": sha, **report}
