"""cp-engine-webhook — Phase C.2

FastAPI service that fathom-meeting-sync calls when a high-confidence
project-status meeting arrives. Generates an ingest plan via Claude,
applies it to a fresh clone of the cp tenant, commits + pushes.

Deployment model: Railway service co-located with fathom-meeting-sync
under the same Railway project (shares env vars + metrics). SSH-based
git push using a deploy key on the cp tenant repo with write access.

Endpoints:
  POST /api/auto-ingest   — main entry; HMAC-signed by caller
  POST /clickup-task-closed — ClickUp → cp bullet-flip (v0.12+); HMAC-signed
  POST /slack-action      — Slack interactive button + modal (v0.14+); HMAC-signed
  GET  /health            — liveness check; reports cp-engine version

Required env vars:
  WEBHOOK_HMAC_SECRET     — shared secret with fathom-meeting-sync
  ANTHROPIC_API_KEY       — for plan generation
  SUPABASE_URL            — for fetching transcripts
  SUPABASE_SERVICE_KEY    — service-role key (server-side only)
  CP_TENANT_REPO_URL      — git@github.com:FirstPersonSF/cp.git
  GIT_SSH_KEY             — private SSH key matching the deploy key
  GIT_AUTHOR_NAME         — "cp-engine-webhook"
  GIT_AUTHOR_EMAIL        — "webhook@firstperson.is" or similar
  CLICKUP_WEBHOOK_SECRET  — (v0.12+) HMAC for /clickup-task-closed; separate
                            from WEBHOOK_HMAC_SECRET so they rotate independently
  SLACK_SIGNING_SECRET    — (v0.14+) HMAC for /slack-action; from Slack app's
                            Basic Information page
  SLACK_BOT_TOKEN         — (v0.14+) `xoxb-...`; used by views.open to launch
                            the "Snooze until…" date-picker modal

Required BUILD-TIME token (NOT a runtime env var):
  gh_token                — read-only GitHub token used to clone the private
                            git+https deps (Canonic-OS/canonic-component-
                            library). Accepted by webhook/Dockerfile via
                            EITHER a BuildKit secret (id=gh_token, preferred,
                            never in image history) OR a build ARG `gh_token`.
                            On Railway: add a service VARIABLE named `gh_token`
                            (lowercase) under Service → Variables — Railway
                            passes service variables to the build as ARGs
                            (Railway does NOT support BuildKit secret mounts).
                            Scope: Contents:read on canonic-component-library.
                            Consumed only inside the install RUN; never in ENV.
                            Local: docker build --secret id=gh_token,env=GH_TOKEN …
                            (or --build-arg gh_token=$GH_TOKEN to mirror Railway)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response

try:
    from supabase import create_client
except ImportError:
    create_client = None  # type: ignore[assignment]

import cp_engine
from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, execute_plan
from cp_engine.plan_from_account_meeting import (
    AccountPlanError,
    generate_account_plan,
    generate_sprint_planning_plan,
    list_active_for_company,
    list_active_for_scope,
)
from cp_engine.plan_from_transcript import (
    PlanGenerationError,
    generate_plan,
)
from cp_engine.retrospective import append_entry, build_entry
from cp_engine.shell import ShellDirNotFound, find_shell_dir

from clickup_propose import propose_clickup_tasks
from meeting_artifact import write_meeting_artifacts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cp-engine-webhook")

# Strong-reference set for background tasks spawned from Slack-action
# handlers. Python's asyncio keeps only WEAK references to tasks created
# by asyncio.create_task — without a strong reference, the GC can collect
# a still-running task mid-execution. The set + done-callback pattern
# (see _spawn_background) keeps a strong reference until the task finishes.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """asyncio.create_task with strong-reference retention.

    Adds the task to the module-level `_background_tasks` set and adds a
    done-callback that discards it once complete. Required for the Slack
    interactive flow where the request returns 200 before the work runs.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _create_supabase_client():
    """Build a Supabase client for execute_plan's ClickUp-proposal verbs.

    Returns None when env vars are missing OR the supabase package isn't
    importable. Callers MUST treat None as "ClickUp routing degraded" and
    keep going — the primary sprint-file ingest contract is that we never
    break it on best-effort Supabase work (see clickup_propose._supabase_client
    for the original of this pattern; this helper exists so every
    `execute_plan(...)` callsite can thread a real client through to the
    v0.15 ``set-milestone`` / ``set-client-ask-task`` verbs instead of
    falling into ingest.py's "no client → silent skip" branch, which is
    what made the v0.15.0 headline feature dead code in prod).
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key or create_client is None:
        log.warning(
            "execute_plan supabase client unavailable: "
            "SUPABASE_URL/SUPABASE_SERVICE_KEY env missing OR supabase pkg not installed"
        )
        return None
    try:
        return create_client(url, key)
    except Exception as exc:  # noqa: BLE001 — never block primary ingest
        log.warning("execute_plan supabase client failed to build: %s", exc)
        return None


app = FastAPI(title="cp-engine-webhook", version=cp_engine.__version__)


@app.get("/health")
def health() -> dict:
    """Liveness probe. Reports the cp-engine version we're running against."""
    return {
        "status": "healthy",
        "cp_engine_version": cp_engine.__version__,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/api/auto-ingest")
async def auto_ingest(request: Request) -> dict:
    """Generate + apply an ingest plan for one meeting against one or more projects.

    Request body (JSON):
        {
          "meeting_id": "<uuid from fathom_meetings.id>",
          "project_codes": ["ggl-5168", "ggl-5136"],
          "transcript_text": "<full transcript>"  # optional; fetched from
                                                    # Supabase if absent
        }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(body, WEBHOOK_HMAC_SECRET))

    Response:
        { "ingested": [{"code": ..., "files_written": [...], "errors": [...]}],
          "commit_sha": "...", "skipped_no_op": false }
    """
    raw_body = await request.body()
    _verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    meeting_id = payload.get("meeting_id")
    project_codes = payload.get("project_codes") or []
    transcript_text = payload.get("transcript_text")
    if not meeting_id or not project_codes:
        raise HTTPException(
            status_code=400, detail="meeting_id and project_codes are required"
        )

    # Fathom retries on timeout. If the same (meeting_id, project_codes)
    # tuple already has a successful run, short-circuit and return early
    # — no fresh clone, no LLM call, no second auto_ingest_runs row.
    # Reruns from the dashboard go through a different endpoint
    # (/api/auto-ingest/runs/{run_id}/rerun) and intentionally skip
    # this check.
    dup = _find_successful_duplicate_run(meeting_id, project_codes)
    if dup is not None:
        log.info(
            "auto-ingest duplicate delivery: meeting=%s codes=%s existing_run=%s",
            meeting_id, project_codes, dup,
        )
        return {
            "status": "duplicate_delivery_skipped",
            "existing_run_id": dup,
            "meeting_id": meeting_id,
            "project_codes": project_codes,
        }

    return _perform_auto_ingest(
        meeting_id=meeting_id,
        project_codes=project_codes,
        transcript_text=transcript_text,
    )


@app.post("/api/auto-ingest/runs/{run_id}/rerun")
async def rerun_auto_ingest(run_id: str, request: Request) -> dict:
    """Rerun a previously-failed auto-ingest run.

    Loads the row from auto_ingest_runs by id, extracts meeting_id +
    project_codes, and re-fires the pipeline via _perform_auto_ingest.
    Writes a NEW auto_ingest_runs row (don't mutate the original — keep
    the failure as history).

    Body SHOULD be JSON ``{"run_id": "<uuid>"}`` matching the URL path.
    Body+timestamp are folded into the HMAC base (see
    ``_verify_signature``), so this binds the signed request to a
    specific target — a captured signature can no longer be replayed
    against an arbitrary ``run_id`` by swapping just the path segment.
    During the phased rollout window we still accept empty bodies for
    backwards compatibility with the v0.13 dashboard; once
    ``WEBHOOK_REQUIRE_TIMESTAMP`` is enforced, the body becomes
    mandatory and its ``run_id`` must equal the URL ``run_id``.

    Only ``status='failed'`` rows may be rerun. Successful reruns are
    risky because sprint files may have been hand-edited since the
    original run.
    """
    raw_body = await request.body()
    _verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    # Body-binds-to-URL guard. If a body is present, its ``run_id`` must
    # match the URL — a captured signed body for run-A can no longer
    # be replayed against run-B via path manipulation. Empty body is
    # allowed for backwards compatibility with the v0.13 dashboard
    # *only* while the legacy-timestamp gate is off; once enforced, the
    # body becomes mandatory (the HMAC must bind to the run_id).
    require_ts = _truthy_env("WEBHOOK_REQUIRE_TIMESTAMP")
    if raw_body:
        try:
            body_payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid JSON: {exc}"
            ) from exc
        body_run_id = (
            body_payload.get("run_id") if isinstance(body_payload, dict) else None
        )
        if require_ts:
            # Enforcement on: body_run_id is mandatory AND must match the
            # URL. A missing/empty key would otherwise silently bypass the
            # equality check, breaking the spec invariant "body run_id ==
            # URL run_id under enforcement".
            if not body_run_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "rerun requires JSON body {'run_id': '<id>'} "
                        "when timestamps are enforced"
                    ),
                )
            if body_run_id != run_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"body run_id {body_run_id!r} does not match "
                        f"URL run_id {run_id!r}"
                    ),
                )
        elif body_run_id and body_run_id != run_id:
            # Enforcement off (legacy): only check when key is present,
            # backwards-compatible with v0.13 dashboard.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"body run_id {body_run_id!r} does not match "
                    f"URL run_id {run_id!r}"
                ),
            )
    elif require_ts:
        raise HTTPException(
            status_code=400,
            detail=(
                "rerun requires JSON body {'run_id': '<id>'} "
                "when timestamps are enforced"
            ),
        )

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key or create_client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for rerun"
        )

    client = create_client(url, key)
    resp = (
        client.table("auto_ingest_runs")
        .select("meeting_id, project_codes, status")
        .eq("id", run_id)
        .single()
        .execute()
    )
    row = resp.data
    if not row:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if row.get("status") != "failed":
        raise HTTPException(
            status_code=400,
            detail=(
                f"only failed runs may be rerun "
                f"(status={row.get('status')!r})"
            ),
        )

    meeting_id = row.get("meeting_id")
    project_codes = row.get("project_codes") or []
    if not meeting_id or not project_codes:
        raise HTTPException(
            status_code=400,
            detail="failed run is missing meeting_id or project_codes",
        )

    log.info(
        "auto-ingest rerun: run_id=%s meeting=%s projects=%s",
        run_id, meeting_id, project_codes,
    )
    return _perform_auto_ingest(
        meeting_id=meeting_id,
        project_codes=project_codes,
    )


def _perform_auto_ingest(
    *,
    meeting_id: str,
    project_codes: list[str],
    transcript_text: str | None = None,
) -> dict:
    """Body of /api/auto-ingest, factored out so the rerun endpoint can
    call it directly without re-marshalling through a fastapi Request.

    Does the transcript fetch (if absent), tenant clone, per-project
    ingest+commit loop, ClickUp proposals, meeting artifacts, and
    auto_ingest_runs row insert. Same return shape as /api/auto-ingest.
    """
    if transcript_text is None:
        transcript_text = _fetch_transcript(meeting_id)

    log.info(
        "auto-ingest start: meeting=%s projects=%s", meeting_id, project_codes
    )

    with _cloned_tenant() as tenant_root:
        config = _load_tenant_config(tenant_root)
        transcript_path = _stage_transcript(tenant_root, meeting_id, transcript_text)

        # Fetch the meeting row once per request. We need `action_items`
        # for the plan merge (each item becomes a deterministic
        # record-ask) AND for the per-meeting artifact write later in
        # this same function. Pre-Lever-1 the artifact path did its own
        # fetch; we now share the result to avoid a redundant Supabase
        # round-trip per auto-ingest call. Best-effort: a None here
        # means we'll fall back to LLM-only ingest (no action_items
        # merged) and artifact generation will skip cleanly.
        meeting = _fetch_meeting(meeting_id)
        action_items = (meeting or {}).get("action_items") or []

        # Per-project ingest + per-project commit. A multi-project
        # auto-ingest call produces N commits (one per project that
        # actually wrote files), not one combined commit. This makes
        # the git log readable (each commit's diff scopes to one
        # project's sprint file) and lets revert target one project
        # without disturbing the others.
        ingested: list[dict] = []
        commits: list[str] = []
        for code in project_codes:
            entry = _ingest_one_project(
                config=config,
                code=code,
                transcript_path=transcript_path,
                action_items=action_items,
                meeting_id=meeting_id,
                meeting=meeting,
            )
            ingested.append(entry)
            if entry["files_written"]:
                commit_sha = _commit_and_push(
                    tenant_root=tenant_root,
                    meeting_id=meeting_id,
                    ingested=[entry],
                )
                entry["commit_sha"] = commit_sha
                commits.append(commit_sha)
                log.info(
                    "auto-ingest commit: meeting=%s project=%s commit=%s",
                    meeting_id, code, commit_sha,
                )

        # Stage A — propose ClickUp tasks from the meeting's Fathom action
        # items. Independent of whether the transcript produced cp bullets;
        # best-effort, never raises.
        clickup_summary = propose_clickup_tasks(meeting_id, project_codes)

        # Per-meeting artifacts — synthesis + transcript into each
        # project's meetings/ dir. Runs after the per-project bullet
        # commits so their `git add -A` doesn't sweep these in. Reuses
        # the meeting row already fetched above; passes it via
        # `meeting=` to skip the redundant Supabase round-trip inside
        # _generate_meeting_artifacts.
        artifact_summary = _generate_meeting_artifacts(
            tenant_root=tenant_root,
            meeting_id=meeting_id,
            transcript_text=transcript_text,
            project_codes=project_codes,
            meeting=meeting,
        )

        if not commits:
            log.info("auto-ingest no-op: no files changed for meeting=%s", meeting_id)
            response = {
                "ingested": ingested,
                "commit_sha": None,
                "skipped_no_op": True,
                "clickup_proposals": clickup_summary,
                "meeting_artifacts": artifact_summary,
            }
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=project_codes,
                status=_status_from_ingested(ingested, anything_wrote=False),
                ingested=ingested,
                commit_sha=None,
            )
            return response

        # observability: log one row per multi-project auto-ingest run
        # against the last commit SHA. Per-project commit shas are
        # surfaced in the `ingested` payload field.
        last_commit = commits[-1]
        log.info(
            "auto-ingest done: meeting=%s commits=%d last=%s",
            meeting_id, len(commits), last_commit,
        )
        response = {
            "ingested": ingested,
            "commit_sha": last_commit,
            "commit_shas": commits,
            "skipped_no_op": False,
            "clickup_proposals": clickup_summary,
            "meeting_artifacts": artifact_summary,
        }
        _log_run_to_supabase(
            meeting_id=meeting_id,
            project_codes=project_codes,
            status="success",
            ingested=ingested,
            commit_sha=last_commit,
        )
        return response


# ─────────────────────────────────────────────────────────────
#  Phase D.4: account-meeting endpoint
# ─────────────────────────────────────────────────────────────


@app.post("/api/auto-ingest-account")
async def auto_ingest_account(request: Request) -> dict:
    """Generate + apply an account-meeting plan.

    Called by fathom-meeting-sync when the user assigns a meeting as an
    account meeting via the dashboard. Different from /api/auto-ingest:
    the project list is NOT in the request — we fetch all currently-
    active projects for the company at ingest time.

    Request body (JSON):
        {
          "meeting_id": "<uuid from fathom_meetings.id>",
          "company_code": "GGL",                # canonical company code
          "transcript_text": "<full transcript>"  # optional; fetched
                                                    # from Supabase if absent
        }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(body, WEBHOOK_HMAC_SECRET))

    Response shape mirrors /api/auto-ingest plus an `account_summary`
    field with the weekly-cp.md commit, and `commit_shas` listing all
    per-project commits.
    """
    raw_body = await request.body()
    _verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    meeting_id = payload.get("meeting_id")
    company_code = payload.get("company_code")
    transcript_text = payload.get("transcript_text")
    if not meeting_id or not company_code:
        raise HTTPException(
            status_code=400,
            detail="meeting_id and company_code are required",
        )

    if transcript_text is None:
        transcript_text = _fetch_transcript(meeting_id)

    log.info(
        "auto-ingest-account start: meeting=%s company=%s",
        meeting_id, company_code,
    )

    with _cloned_tenant() as tenant_root:
        config = _load_tenant_config(tenant_root)

        # Fetch the active project list at ingest time — not at
        # assignment time. A project added today is in scope; a project
        # closed today drops out.
        active = list_active_for_company(config, company_code)
        if not active:
            log.warning(
                "auto-ingest-account: no active projects for company=%s",
                company_code,
            )
            response = {
                "ingested": [],
                "commit_sha": None,
                "skipped_no_op": True,
                "reason": f"no active projects for company '{company_code}'",
            }
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=[],
                status="skipped_no_op",
                ingested=[],
                commit_sha=None,
            )
            return response

        log.info(
            "auto-ingest-account: %d active project(s) for company=%s: %s",
            len(active), company_code, [p.code for p in active],
        )

        # Stage transcript for the prompt + audit log.
        transcript_path = _stage_transcript(tenant_root, meeting_id, transcript_text)

        # Generate the multi-project plan in ONE Claude call.
        try:
            generated = generate_account_plan(
                config=config,
                company_code=company_code,
                meeting_id=meeting_id,
                transcript_text=transcript_text,
                active_projects=list(active),
            )
        except AccountPlanError as exc:
            log.error("account plan generation failed: %s", exc)
            response = {
                "ingested": [],
                "commit_sha": None,
                "skipped_no_op": False,
                "errors": [str(exc)],
            }
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=[p.code for p in active],
                status="failed",
                ingested=[],
                commit_sha=None,
                top_level_errors=[f"account plan generation failed: {exc}"],
            )
            return response

        # Execute the plan. Per-project entries fan out into per-project
        # commits via the same pattern as /api/auto-ingest. The
        # account_summary lands as a separate weekly-cp.md commit.
        plan = generated.plan
        projects_block = plan.get("projects") or {}

        ingested: list[dict] = []
        commits: list[str] = []

        # Step 1: per-project ingest + commit (as before)
        #
        # NOTE: Fathom action_items are intentionally NOT threaded through
        # this multi-project endpoint (unlike /api/auto-ingest, which
        # passes them to _ingest_one_project). The action_items JSONB on
        # fathom_meetings has no per-project attribution, so for an
        # account meeting covering N projects we can't route a given item
        # to the right one. See clickup_propose.py:148-151 for the same
        # constraint downstream. If we ever change this, decide first how
        # to handle the routing.
        for code, entries in projects_block.items():
            single_project_plan = {
                "transcript": plan.get("transcript", {"source": "fathom"}),
                "projects": {code: entries},
            }
            entry = {
                "code": code,
                "plan_summary": {v: len(items) for v, items in entries.items()},
                "files_written": [],
                "skipped_duplicate": 0,
                "errors": [],
            }
            try:
                exec_result = execute_plan(
                    single_project_plan,
                    tenant_root=tenant_root,
                    today=datetime.now().date(),
                    supabase=_create_supabase_client(),
                    meeting_id=meeting_id,
                )
            except IngestPlanError as exc:
                entry["errors"].append(f"plan execution failed: {exc}")
                ingested.append(entry)
                continue
            entry["files_written"] = [str(p) for p in exec_result.files_written]
            entry["skipped_duplicate"] = exec_result.skipped_duplicate
            entry["errors"].extend(exec_result.errors)
            ingested.append(entry)
            if exec_result.files_written:
                commit_sha = _commit_and_push(
                    tenant_root=tenant_root,
                    meeting_id=meeting_id,
                    ingested=[entry],
                )
                entry["commit_sha"] = commit_sha
                commits.append(commit_sha)
                log.info(
                    "auto-ingest-account commit: meeting=%s project=%s commit=%s",
                    meeting_id, code, commit_sha,
                )

        # Step 2: account_summary (+ account_decisions if any) → one
        # additional commit against weekly-cp.md.
        summary_plan = {
            "transcript": plan.get("transcript", {"source": "fathom"}),
        }
        if plan.get("account_summary"):
            summary_plan["account_summary"] = plan["account_summary"]
        if plan.get("account_decisions"):
            summary_plan["account_decisions"] = plan["account_decisions"]

        if "account_summary" in summary_plan or "account_decisions" in summary_plan:
            summary_entry = {
                "code": f"account:{company_code.lower()}",
                "plan_summary": {
                    "account_summary": 1 if "account_summary" in summary_plan else 0,
                    "account_decisions": (
                        len(summary_plan.get("account_decisions") or [])
                    ),
                },
                "files_written": [],
                "skipped_duplicate": 0,
                "errors": [],
            }
            try:
                summary_exec = execute_plan(
                    summary_plan,
                    tenant_root=tenant_root,
                    today=datetime.now().date(),
                    supabase=_create_supabase_client(),
                    meeting_id=meeting_id,
                )
            except IngestPlanError as exc:
                summary_entry["errors"].append(f"plan execution failed: {exc}")
                ingested.append(summary_entry)
            else:
                summary_entry["files_written"] = [
                    str(p) for p in summary_exec.files_written
                ]
                summary_entry["skipped_duplicate"] = summary_exec.skipped_duplicate
                summary_entry["errors"].extend(summary_exec.errors)
                if summary_exec.files_written:
                    summary_commit = _commit_and_push(
                        tenant_root=tenant_root,
                        meeting_id=meeting_id,
                        ingested=[summary_entry],
                    )
                    summary_entry["commit_sha"] = summary_commit
                    commits.append(summary_commit)
                    log.info(
                        "auto-ingest-account summary commit: meeting=%s commit=%s",
                        meeting_id, summary_commit,
                    )
                ingested.append(summary_entry)

        # Per-meeting artifacts into every active project's meetings/ dir.
        artifact_summary = _generate_meeting_artifacts(
            tenant_root=tenant_root,
            meeting_id=meeting_id,
            transcript_text=transcript_text,
            project_codes=[p.code for p in active],
        )

        if not commits:
            log.info(
                "auto-ingest-account no-op: nothing changed for meeting=%s",
                meeting_id,
            )
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=[p.code for p in active],
                status="skipped_no_op",
                ingested=ingested,
                commit_sha=None,
            )
            return {
                "ingested": ingested,
                "commit_sha": None,
                "commit_shas": [],
                "skipped_no_op": True,
                "meeting_artifacts": artifact_summary,
            }

        last_commit = commits[-1]
        _log_run_to_supabase(
            meeting_id=meeting_id,
            project_codes=[p.code for p in active],
            status="success",
            ingested=ingested,
            commit_sha=last_commit,
        )
        return {
            "ingested": ingested,
            "commit_sha": last_commit,
            "commit_shas": commits,
            "skipped_no_op": False,
            "meeting_artifacts": artifact_summary,
        }


# ─────────────────────────────────────────────────────────────
#  Phase D.5: tenant-scope sprint planning endpoint
# ─────────────────────────────────────────────────────────────


@app.post("/api/auto-ingest-sprint-planning")
async def auto_ingest_sprint_planning(request: Request) -> dict:
    """Generate + apply a tenant-scope sprint-planning plan.

    Called by fathom-meeting-sync when the user assigns a meeting as
    sprint planning for a specific scope ('1p', 'fpsf', 'canonic').
    Like /api/auto-ingest-account but the project list spans multiple
    companies (1p = all client engagements; fpsf/canonic = all
    initiatives under that self-company).

    Request body (JSON):
        {
          "meeting_id": "<uuid from fathom_meetings.id>",
          "scope": "1p" | "fpsf" | "canonic",
          "transcript_text": "<full transcript>"  # optional
        }

    Response shape mirrors /api/auto-ingest-account.
    """
    raw_body = await request.body()
    _verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    meeting_id = payload.get("meeting_id")
    scope = payload.get("scope")
    transcript_text = payload.get("transcript_text")
    if not meeting_id or not scope:
        raise HTTPException(
            status_code=400,
            detail="meeting_id and scope are required",
        )

    if transcript_text is None:
        transcript_text = _fetch_transcript(meeting_id)

    log.info(
        "auto-ingest-sprint-planning start: meeting=%s scope=%s",
        meeting_id, scope,
    )

    with _cloned_tenant() as tenant_root:
        config = _load_tenant_config(tenant_root)

        try:
            active = list_active_for_scope(config, scope)
        except AccountPlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not active:
            log.warning(
                "auto-ingest-sprint-planning: no active projects for scope=%s",
                scope,
            )
            response = {
                "ingested": [],
                "commit_sha": None,
                "skipped_no_op": True,
                "reason": f"no active projects for scope '{scope}'",
            }
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=[],
                status="skipped_no_op",
                ingested=[],
                commit_sha=None,
            )
            return response

        log.info(
            "auto-ingest-sprint-planning: %d active project(s) for scope=%s: %s",
            len(active), scope, [p.code for p in active],
        )

        transcript_path = _stage_transcript(tenant_root, meeting_id, transcript_text)

        try:
            generated = generate_sprint_planning_plan(
                config=config,
                scope=scope,
                meeting_id=meeting_id,
                transcript_text=transcript_text,
                active_projects=list(active),
            )
        except AccountPlanError as exc:
            log.error("sprint-planning plan generation failed: %s", exc)
            response = {
                "ingested": [],
                "commit_sha": None,
                "skipped_no_op": False,
                "errors": [str(exc)],
            }
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=[p.code for p in active],
                status="failed",
                ingested=[],
                commit_sha=None,
                top_level_errors=[f"sprint-planning plan generation failed: {exc}"],
            )
            return response

        plan = generated.plan
        projects_block = plan.get("projects") or {}

        ingested: list[dict] = []
        commits: list[str] = []

        # Per-project ingest + commit (same pattern as /api/auto-ingest-account).
        #
        # NOTE: Fathom action_items are intentionally NOT threaded through
        # this multi-project endpoint (unlike /api/auto-ingest, which
        # passes them to _ingest_one_project). The action_items JSONB on
        # fathom_meetings has no per-project attribution, so for a
        # sprint-planning meeting covering N projects we can't route a
        # given item to the right one. See clickup_propose.py:148-151 for
        # the same constraint downstream. If we ever change this, decide
        # first how to handle the routing.
        for code, entries in projects_block.items():
            single_project_plan = {
                "transcript": plan.get("transcript", {"source": "fathom"}),
                "projects": {code: entries},
            }
            entry = {
                "code": code,
                "plan_summary": {v: len(items) for v, items in entries.items()},
                "files_written": [],
                "skipped_duplicate": 0,
                "errors": [],
            }
            try:
                exec_result = execute_plan(
                    single_project_plan,
                    tenant_root=tenant_root,
                    today=datetime.now().date(),
                    supabase=_create_supabase_client(),
                    meeting_id=meeting_id,
                )
            except IngestPlanError as exc:
                entry["errors"].append(f"plan execution failed: {exc}")
                ingested.append(entry)
                continue
            entry["files_written"] = [str(p) for p in exec_result.files_written]
            entry["skipped_duplicate"] = exec_result.skipped_duplicate
            entry["errors"].extend(exec_result.errors)
            ingested.append(entry)
            if exec_result.files_written:
                commit_sha = _commit_and_push(
                    tenant_root=tenant_root,
                    meeting_id=meeting_id,
                    ingested=[entry],
                )
                entry["commit_sha"] = commit_sha
                commits.append(commit_sha)
                log.info(
                    "auto-ingest-sprint-planning commit: meeting=%s project=%s commit=%s",
                    meeting_id, code, commit_sha,
                )

        # account_summary + account_decisions → one weekly-cp.md commit.
        summary_plan = {
            "transcript": plan.get("transcript", {"source": "fathom"}),
        }
        if plan.get("account_summary"):
            summary_plan["account_summary"] = plan["account_summary"]
        if plan.get("account_decisions"):
            summary_plan["account_decisions"] = plan["account_decisions"]

        if "account_summary" in summary_plan or "account_decisions" in summary_plan:
            summary_entry = {
                "code": f"sprint-planning:{scope}",
                "plan_summary": {
                    "account_summary": 1 if "account_summary" in summary_plan else 0,
                    "account_decisions": (
                        len(summary_plan.get("account_decisions") or [])
                    ),
                },
                "files_written": [],
                "skipped_duplicate": 0,
                "errors": [],
            }
            try:
                summary_exec = execute_plan(
                    summary_plan,
                    tenant_root=tenant_root,
                    today=datetime.now().date(),
                    supabase=_create_supabase_client(),
                    meeting_id=meeting_id,
                )
            except IngestPlanError as exc:
                summary_entry["errors"].append(f"plan execution failed: {exc}")
                ingested.append(summary_entry)
            else:
                summary_entry["files_written"] = [
                    str(p) for p in summary_exec.files_written
                ]
                summary_entry["skipped_duplicate"] = summary_exec.skipped_duplicate
                summary_entry["errors"].extend(summary_exec.errors)
                if summary_exec.files_written:
                    summary_commit = _commit_and_push(
                        tenant_root=tenant_root,
                        meeting_id=meeting_id,
                        ingested=[summary_entry],
                    )
                    summary_entry["commit_sha"] = summary_commit
                    commits.append(summary_commit)
                    log.info(
                        "auto-ingest-sprint-planning summary commit: meeting=%s commit=%s",
                        meeting_id, summary_commit,
                    )
                ingested.append(summary_entry)

        # Per-meeting artifacts into every active project's meetings/ dir.
        artifact_summary = _generate_meeting_artifacts(
            tenant_root=tenant_root,
            meeting_id=meeting_id,
            transcript_text=transcript_text,
            project_codes=[p.code for p in active],
        )

        if not commits:
            log.info(
                "auto-ingest-sprint-planning no-op: nothing changed for meeting=%s",
                meeting_id,
            )
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=[p.code for p in active],
                status="skipped_no_op",
                ingested=ingested,
                commit_sha=None,
            )
            return {
                "ingested": ingested,
                "commit_sha": None,
                "commit_shas": [],
                "skipped_no_op": True,
                "meeting_artifacts": artifact_summary,
            }

        last_commit = commits[-1]
        _log_run_to_supabase(
            meeting_id=meeting_id,
            project_codes=[p.code for p in active],
            status="success",
            ingested=ingested,
            commit_sha=last_commit,
        )
        return {
            "ingested": ingested,
            "commit_sha": last_commit,
            "commit_shas": commits,
            "skipped_no_op": False,
            "meeting_artifacts": artifact_summary,
        }


# ─────────────────────────────────────────────────────────────
#  Lever 1 / Task 1.7 — ClickUp close round-trip
# ─────────────────────────────────────────────────────────────


@app.post("/clickup-task-closed")
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
    _verify_clickup_signature(raw_body, request.headers.get("x-signature", ""))

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

    with _cloned_tenant() as tenant_root:
        config = _load_tenant_config(tenant_root)
        try:
            # close-ask plan — no ClickUp-proposal verbs in scope, but pass
            # the client for parity (cheap, and future-safe).
            result = execute_plan(
                plan,
                tenant_root=config.root,
                today=datetime.now().date(),
                supabase=_create_supabase_client(),
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

        commit_sha = _commit_clickup_close(
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


# ─────────────────────────────────────────────────────────────
#  Lever 3 / Task 3.2 — Slack interactive component endpoint
# ─────────────────────────────────────────────────────────────


@app.post("/slack-action")
async def slack_action(request: Request) -> dict:
    """Handle a Slack interactive-component click.

    Slack POSTs `application/x-www-form-urlencoded` with a single `payload`
    field containing the JSON. We verify the signature, parse the payload,
    route by `value` prefix, and IMMEDIATELY return 200 (Slack's 3-second
    ack window). Actual work happens in a background asyncio task.

    `value` format: `<verb>|<code>|<hash>` for fixed-action buttons.
    For `snooze-{ask,risk}-pick`, the click opens a modal via views.open
    inline (the trigger_id expires after 3s, so this can't be backgrounded);
    the actual snooze happens on the subsequent `view_submission`.

    Reference: https://api.slack.com/interactivity/handling
    """
    raw_body = await request.body()
    _verify_slack_signature(
        raw_body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
    )

    import urllib.parse as _up
    form = _up.parse_qs(raw_body.decode("utf-8"))
    payload_raw = form.get("payload", [""])[0]
    if not payload_raw:
        raise HTTPException(400, "missing payload field")
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"invalid JSON in payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload must be a JSON object")

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        return await _handle_block_action(payload)
    if payload_type == "view_submission":
        return await _handle_view_submission(payload)
    return {"ok": True, "ignored": payload_type}


async def _handle_block_action(payload: dict) -> dict:
    """Acknowledge IMMEDIATELY; do the work in a background task."""
    actions = payload.get("actions") or []
    if not actions:
        raise HTTPException(400, "no actions in payload")
    action = actions[0]
    value = action.get("value") or ""
    # Slack guarantees action_id is unique per block-element. v0.14's
    # button emitters namespace it as `<verb>_<code>_<hash>` so even
    # duplicate-hash items still produce unique ids. _post_response_url_update
    # uses this to find and surgically replace ONLY the clicked item's
    # actions block (Bug 1 in v0.14.0/1: walking ALL actions blocks made
    # one click visually close every item in the digest).
    action_id = action.get("action_id") or ""
    user_id = (payload.get("user") or {}).get("id", "")
    response_url = payload.get("response_url") or ""
    trigger_id = payload.get("trigger_id", "")
    original_message = payload.get("message", {})

    parts = value.split("|")
    if len(parts) != 3:
        raise HTTPException(400, f"malformed value: {value!r}")
    verb, code, cp_hash = parts

    # Snooze-pick: modal must open NOW (trigger_id expires in 3s). views.open
    # is one API call (~200-400ms); within budget. Run via asyncio.to_thread
    # so the sync slack_sdk call doesn't block the loop, but AWAIT before
    # returning (the modal must be open before we ack).
    if verb.endswith("-pick"):
        underlying_verb = verb.replace("-pick", "")
        await asyncio.to_thread(
            _open_snooze_modal,
            trigger_id=trigger_id,
            verb=underlying_verb,
            code=code,
            cp_hash=cp_hash,
            response_url=response_url,
        )
        return {"ok": True}

    extras: dict = {"closed_by": "slack", "user": user_id}

    if verb in ("snooze-ask-7d", "snooze-risk-7d"):
        from datetime import timedelta
        underlying_verb = verb.replace("-7d", "")
        extras["until"] = (date.today() + timedelta(days=7)).isoformat()
        verb = underlying_verb

    # Dispatch the slow path (clone + plan + push + Slack update) to a
    # background task via _spawn_background (strong-ref retention).
    #
    # Structured-log every spawn so a Railway restart that interrupts the
    # background task is recoverable from logs (postmortem: grep for
    # `slack_action_spawn` and replay any missing `slack_action_complete`
    # in the same time window).
    # TODO(v0.16): persist to slack_action_intents for automatic recovery
    # sweep on restart — until we see real drops in prod, structured
    # logs are good enough.
    log.info(
        "slack_action_spawn code=%s verb=%s hash=%s action_id=%s user=%s",
        code, verb, cp_hash, action_id, user_id,
    )
    _spawn_background(_run_action_in_background(
        verb=verb, code=code, cp_hash=cp_hash, extras=extras,
        response_url=response_url, original_message=original_message,
        clicked_action_id=action_id,
    ))
    return {"ok": True, "queued": True}


async def _handle_view_submission(payload: dict) -> dict:
    """Date-picker modal submission. Validates synchronously (so we can
    return inline field errors), then backgrounds the plan run + response
    update so we ack Slack inside the 3-second window."""
    view = payload.get("view") or {}
    if view.get("callback_id") != "snooze_until_modal":
        # Unknown modal — DO NOT return `response_action: clear` (that would
        # close a modal we don't own). Return `{"ok": True, "ignored": ...}`.
        return {"ok": True, "ignored": view.get("callback_id")}
    # Defensive: Slack echoes private_metadata verbatim, but version skew or
    # test traffic could feed garbage. Bare json.loads → 500 → Slack retries 3x.
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except json.JSONDecodeError:
        log.warning("view_submission has malformed private_metadata; ignoring")
        return {"ok": True, "ignored": "malformed metadata"}
    verb = meta.get("verb")
    code = meta.get("code")
    cp_hash = meta.get("hash")
    response_url = meta.get("response_url", "")
    # Schema-drift / replay guard: missing fields → inline modal error rather
    # than silently dispatching a nonsense plan (verb=None, code=None, …).
    if not (verb and code and cp_hash):
        log.warning("view_submission missing required fields: %s", meta)
        return {
            "response_action": "errors",
            "errors": {"date_block": "Snooze request expired; please click the button again."},
        }

    state = view.get("state", {}).get("values", {})
    until = (
        state.get("date_block", {})
             .get("until_date", {})
             .get("selected_date", "")
    )
    if not until:
        return {
            "response_action": "errors",
            "errors": {"date_block": "Pick a date"},
        }

    # Same structured-log shape as _handle_block_action so a single
    # logfilter catches both spawn paths.
    log.info(
        "slack_action_spawn code=%s verb=%s hash=%s action_id=%s "
        "source=view_submission until=%s",
        code, verb, cp_hash, "", until,
    )
    _spawn_background(_run_action_in_background(
        verb=verb, code=code, cp_hash=cp_hash,
        extras={"until": until},  # No closed_by — only meaningful for close/resolve verbs
        response_url=response_url,
        original_message={},  # modal submission has no original to splice into
    ))
    return {"response_action": "clear"}


async def _run_action_in_background(
    *,
    verb: str, code: str, cp_hash: str, extras: dict,
    response_url: str, original_message: dict,
    clicked_action_id: str = "",
) -> None:
    """Background coroutine: run the cp plan via to_thread, update Slack.

    Wraps sync work (git subprocess, slack_sdk sync calls, requests.post)
    via asyncio.to_thread so the event loop stays free.

    `clicked_action_id` is threaded through to _post_response_url_update so
    that only the actions block carrying that action_id is replaced with
    a confirmation (Bug 1 fix in v0.14.2: previously the loop replaced
    EVERY actions block in the message, so one click visually closed all
    digest items at once).

    Logs but never raises — exceptions here would be lost. Surface failures
    via the in-place Slack message instead.
    """
    try:
        result = await asyncio.to_thread(
            _run_plan_for_one_item,
            verb=verb, code=code, cp_hash=cp_hash, **extras,
        )
    except Exception as exc:  # noqa: BLE001 — background must not crash
        log.exception(
            "slack-action background failed: %s/%s/%s", verb, code, cp_hash
        )
        result = {"committed": False, "commit_sha": None, "errors": [str(exc)]}

    # Pairs with `slack_action_spawn` so postmortem can correlate spawns
    # with completions and identify clicks that never wrapped up (Railway
    # restart mid-task). action_id isn't in scope here; correlation key
    # is (code, verb, hash) which is unique per displayed digest item.
    log.info(
        "slack_action_complete code=%s verb=%s hash=%s committed=%s "
        "commit_sha=%s errors=%d",
        code, verb, cp_hash,
        result.get("committed"),
        (result.get("commit_sha") or "")[:8],
        len(result.get("errors") or []),
    )

    confirmation = _confirmation_text(verb=verb, extras=extras, result=result)
    try:
        await asyncio.to_thread(
            _post_response_url_update,
            response_url=response_url,
            original_message=original_message,
            confirmation=confirmation,
            clicked_action_id=clicked_action_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "slack-action response_url update failed: %s/%s", code, cp_hash
        )


def _run_plan_for_one_item(
    *, verb: str, code: str, cp_hash: str, **extras
) -> dict:
    """Clone tenant, run a 1-item plan for the given verb, commit, push.

    Mirrors `_perform_auto_ingest`'s shape but for the tiny resolve/snooze/
    close plans triggered by Slack button clicks.
    """
    item: dict = {"hash": cp_hash}
    if "until" in extras:
        item["until"] = extras["until"]
    if "closed_by" in extras:
        item["closed_by"] = extras["closed_by"]

    plan = {"projects": {code: {verb: [item]}}}

    with _cloned_tenant() as tenant_root:
        # Slack-button plans are close-ask / snooze-ask / resolve-risk — no
        # ClickUp-proposal verbs in scope here. Pass the client for parity
        # (cheap) and meeting_id=None (this is a Slack click, not a meeting).
        result = execute_plan(
            plan,
            tenant_root=tenant_root,
            today=date.today(),
            supabase=_create_supabase_client(),
            meeting_id=None,
        )

        ingested_entry = {
            "code": code,
            "files_written": [str(p) for p in result.files_written],
            "errors": result.errors,
            "plan_summary": {verb: 1},
        }
        if result.files_written:
            commit_sha = _commit_and_push(
                tenant_root=tenant_root,
                meeting_id=f"slack-{verb}-{cp_hash}",
                ingested=[ingested_entry],
            )
            return {
                "committed": True,
                "commit_sha": commit_sha,
                "errors": result.errors,
            }
        return {
            "committed": False,
            "commit_sha": None,
            "errors": result.errors,
        }


def _post_response_url_update(
    *,
    response_url: str,
    original_message: dict,
    confirmation: str,
    clicked_action_id: str = "",
) -> None:
    """POST to Slack's `response_url` to replace ONLY the clicked item's
    actions block with the confirmation context.

    The original digest message has N items, each with its own actions
    block carrying 3 buttons. When a user clicks one button, we want
    the corresponding actions block (and ONLY that one) replaced with
    "✅ Resolved · 10:42 AM UTC · `abc12345`" — every OTHER item must
    keep its action buttons intact.

    `clicked_action_id` is the `action_id` of the button the user
    clicked. We walk the message blocks and only replace the actions
    block that contains an element with that action_id.

    Fallback: if `clicked_action_id` is empty (older view_submission
    code path that doesn't have a single source block), fall through
    to the old replace-all behavior — modal submissions pass
    `original_message={}` anyway, so no blocks are touched.
    """
    import requests as _req

    def _block_contains_action_id(block: dict, target_id: str) -> bool:
        if not target_id or block.get("type") != "actions":
            return False
        for el in block.get("elements", []) or []:
            if el.get("action_id") == target_id:
                return True
        return False

    new_blocks: list[dict] = []
    replaced = False
    for block in original_message.get("blocks", []):
        if _block_contains_action_id(block, clicked_action_id):
            new_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": confirmation}],
            })
            replaced = True
        elif block.get("type") == "actions" and not clicked_action_id:
            # Backward-compat fallback: no clicked_action_id provided,
            # collapse all actions blocks (legacy view_submission path).
            new_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": confirmation}],
            })
            replaced = True
        else:
            new_blocks.append(block)

    if not replaced and clicked_action_id:
        # The action_id we were told to replace wasn't found in the
        # message — e.g. message was edited mid-click, or Slack
        # delivered a stale message blob. Log it; don't silently
        # disappear the confirmation.
        log.warning(
            "response_url update: action_id %r not found in message blocks; "
            "no in-place update applied",
            clicked_action_id,
        )

    resp = _req.post(response_url, json={
        "replace_original": True,
        "blocks": new_blocks,
        "text": confirmation,
    }, timeout=5)
    if not resp.ok:
        log.warning(
            "response_url update returned %s: %s",
            resp.status_code, resp.text[:200],
        )


def _confirmation_text(*, verb: str, extras: dict, result: dict) -> str:
    """Human-readable confirmation rendered in the post-click message.

    Uses `%I` (zero-padded) rather than `%-I` (unpadded extension) for
    cross-platform consistency.
    """
    from datetime import datetime as _datetime, timezone as _tz
    now_str = _datetime.now(_tz.utc).strftime("%I:%M %p UTC").lstrip("0")
    label = {
        "resolve-risk": "✅ Resolved",
        "close-ask": "✅ Closed",
        "snooze-ask": f"💤 Snoozed until {extras.get('until', '?')}",
        "snooze-risk": f"💤 Snoozed until {extras.get('until', '?')}",
    }.get(verb, "✓ Done")
    sha = result.get("commit_sha")
    sha_str = f" · `{sha[:8]}`" if sha else ""
    errors = result.get("errors") or []
    if errors and not sha:
        return f"⚠️ Action failed: {errors[0][:120]}"
    # Silent dedupe: hash not in current sprint file (item already resolved
    # on a previous click, or rolled forward to a different sprint). The
    # resolve-risk / snooze-* writers treat this as a no-op (per Task 1.1
    # / 1.2 patterns). Surface to the user so the message isn't misleading.
    if not result.get("committed"):
        return f"ℹ️ No matching item (already resolved or moved sprint) · {now_str}"
    return f"{label} · {now_str}{sha_str}"


def _open_snooze_modal(
    *, trigger_id: str, verb: str, code: str, cp_hash: str, response_url: str,
) -> None:
    """Open a Slack modal with a date picker; the actual snooze happens
    on the subsequent view_submission callback.

    `verb` is the underlying verb without the -pick suffix: 'snooze-ask' or
    'snooze-risk'. Packed into private_metadata so _handle_view_submission
    can route the submission to the right plan.
    """
    from datetime import date, timedelta
    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise HTTPException(500, "SLACK_BOT_TOKEN not configured")
    client = WebClient(token=token)
    private_metadata = json.dumps({
        "verb": verb, "code": code, "hash": cp_hash, "response_url": response_url,
    })
    client.views_open(trigger_id=trigger_id, view={
        "type": "modal",
        "callback_id": "snooze_until_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Snooze until"},
        "submit": {"type": "plain_text", "text": "Snooze"},
        "blocks": [{
            "type": "input",
            "block_id": "date_block",
            "label": {"type": "plain_text", "text": f"Snooze {code} until:"},
            "element": {
                "type": "datepicker",
                "action_id": "until_date",
                "initial_date": (date.today() + timedelta(days=7)).isoformat(),
            },
        }],
    })


# ─────────────────────────────────────────────────────────────
#  Signature verification
# ─────────────────────────────────────────────────────────────


# Replay-window (seconds). Matches the Slack signature freshness budget.
_TIMESTAMP_REPLAY_WINDOW_SEC = 300


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _verify_signature(
    raw_body: bytes, provided: str, timestamp: str | None = None
) -> None:
    """HMAC-SHA256 verify the fathom-meeting-sync -> cp-engine-webhook body.

    Two HMAC shapes are accepted to allow a phased rollout:

    1. Legacy (no timestamp): ``hmac(secret, body)`` — accepted when
       ``timestamp`` is empty AND the env var ``WEBHOOK_REQUIRE_TIMESTAMP``
       is unset/false. A warning is logged so we can monitor when the
       caller side finishes rolling out.

    2. Replay-protected: ``hmac(secret, f"{timestamp}.{body}")`` —
       always accepted. The timestamp is rejected (401) if it's older
       or further in the future than 5 minutes (``abs(now - ts) > 300``).

    When ``WEBHOOK_REQUIRE_TIMESTAMP`` is true, missing timestamps and
    legacy-shape signatures both 401. This is the gate we flip after
    fathom-meeting-sync ships its own update.

    ``timestamp`` is Unix epoch seconds as a string (matches the Slack
    pattern at ``_verify_slack_signature``).
    """
    secret = os.environ.get("WEBHOOK_HMAC_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="WEBHOOK_HMAC_SECRET not configured")
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Webhook-Signature header")

    require_ts = _truthy_env("WEBHOOK_REQUIRE_TIMESTAMP")

    if not timestamp:
        if require_ts:
            raise HTTPException(
                status_code=401, detail="missing X-Webhook-Timestamp header"
            )
        # Phased rollout: accept the legacy body-only shape but log it
        # so we know when the caller side has cut over.
        log.warning(
            "webhook-verify: legacy unsigned-timestamp request accepted "
            "(set WEBHOOK_REQUIRE_TIMESTAMP=true to enforce)"
        )
        expected_legacy = hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_legacy, provided):
            raise HTTPException(status_code=401, detail="invalid signature")
        return

    try:
        ts_int = int(timestamp)
    except ValueError:
        raise HTTPException(
            status_code=401, detail="X-Webhook-Timestamp not an integer"
        ) from None

    skew = time.time() - ts_int
    if abs(skew) > _TIMESTAMP_REPLAY_WINDOW_SEC:
        log.warning(
            "webhook-verify rejected: timestamp outside %ds window (skew=%.1fs)",
            _TIMESTAMP_REPLAY_WINDOW_SEC, skew,
        )
        raise HTTPException(
            status_code=401, detail="X-Webhook-Timestamp outside freshness window"
        )

    base = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")


def _verify_clickup_signature(raw_body: bytes, provided: str) -> None:
    """HMAC-SHA256 validate a ClickUp webhook payload.

    ClickUp signs each webhook with HMAC-SHA256 (hex) under the
    `X-Signature` header, using a secret returned when the webhook is
    registered via their API. See:
    https://developer.clickup.com/docs/webhooksignature

    Uses CLICKUP_WEBHOOK_SECRET — kept distinct from the Fathom webhook's
    WEBHOOK_HMAC_SECRET so they can be rotated independently.
    """
    secret = os.environ.get("CLICKUP_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(
            status_code=500, detail="CLICKUP_WEBHOOK_SECRET not configured"
        )
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Signature header")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")


def _verify_slack_signature(raw_body: bytes, timestamp: str, provided: str) -> None:
    """Verify Slack's `X-Slack-Signature` header.

    Slack signs `v0:<timestamp>:<body>` with HMAC-SHA256 against
    SLACK_SIGNING_SECRET. The header value is `v0=<hex digest>`. Also
    enforces the 5-minute timestamp freshness window to prevent replays.

    Reference: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not secret:
        log.warning("slack-verify rejected: SLACK_SIGNING_SECRET not configured")
        raise HTTPException(500, "SLACK_SIGNING_SECRET not configured")
    if not provided or not provided.startswith("v0="):
        log.warning(
            "slack-verify rejected: missing or malformed X-Slack-Signature "
            "(provided=%r)", (provided or "")[:16],
        )
        raise HTTPException(401, "missing or malformed X-Slack-Signature")
    if not timestamp:
        log.warning("slack-verify rejected: missing X-Slack-Request-Timestamp")
        raise HTTPException(401, "missing X-Slack-Request-Timestamp")
    try:
        ts_int = int(timestamp)
    except ValueError:
        log.warning("slack-verify rejected: timestamp not an int: %r", timestamp[:32])
        raise HTTPException(401, "X-Slack-Request-Timestamp not an int") from None
    import time as _time
    skew = _time.time() - ts_int
    if abs(skew) > 300:
        log.warning(
            "slack-verify rejected: timestamp outside 5-min window (skew=%.1fs)",
            skew,
        )
        raise HTTPException(401, "Slack timestamp outside 5-minute freshness window")
    base = f"v0:{timestamp}:".encode() + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        # Diagnostic logging: never log the full secret OR the full signatures
        # (would leak HMAC state). Log first 6 hex chars of expected vs provided
        # so we can tell if it's a wrong-secret-entirely case (totally different
        # prefixes) vs body-encoding case (close prefixes but not exact match).
        log.warning(
            "slack-verify rejected: HMAC mismatch (expected_prefix=%s provided_prefix=%s "
            "body_len=%d secret_len=%d)",
            expected[:9],  # "v0=" + 6 hex chars
            provided[:9],
            len(raw_body),
            len(secret),
        )
        raise HTTPException(401, "invalid Slack signature")


def _lookup_proposal_by_clickup_task_id(task_id: str) -> tuple[str, str] | None:
    """Return (cp_ask_hash, project_code) for a given ClickUp task_id.

    Joins clickup_task_proposals → projects → companies to reconstruct
    the `<company>-<number>` code. Returns None if no row matches or
    Supabase is unavailable. Best-effort: any exception is swallowed
    and treated as 'not found' (the webhook returns `ingested: false`).

    Note: today, this only resolves engagement codes (``<company>-<number>``)
    because the ``clickup_task_proposals.project_id`` FK targets
    ``projects.id``, not ``initiatives.id``. The initiative ClickUp path in
    ``clickup_propose._resolve_project`` is latently broken (would fail FK
    insert) — when that's fixed in a separate task, this lookup will need
    a parallel ``initiatives`` fallback.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("clickup-task-closed: Supabase env not set; can't look up task_id")
        return None
    try:
        from supabase import create_client

        client = create_client(url, key)
        resp = (
            client.table("clickup_task_proposals")
            .select("cp_ask_hash, projects!inner(number, companies!inner(code))")
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
        proj = row.get("projects") or {}
        # Supabase nested selects can return list-or-dict depending on
        # how the relationship is declared; normalize both shapes.
        if isinstance(proj, list):
            proj = proj[0] if proj else {}
        companies = proj.get("companies") or {}
        if isinstance(companies, list):
            companies = companies[0] if companies else {}
        company_code = (companies.get("code") or "").lower()
        number = proj.get("number")
        if company_code and number is not None:
            return cp_hash, f"{company_code}-{number}"
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "clickup-task-closed: task lookup failed for %s: %s", task_id, exc
        )
        return None


def _commit_clickup_close(
    *, tenant_root: Path, code: str, cp_hash: str
) -> str | None:
    """Commit + push a ClickUp-close round-trip. Returns the new HEAD sha,
    or None if the working tree was clean (e.g., execute_plan already
    flipped the bullet on a previous webhook run)."""
    env = _ssh_env()

    # Short-circuit if execute_plan made no on-disk change. Without this,
    # `git commit` would fail with "nothing to commit" and 500 the request.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        log.info(
            "clickup-task-closed: no changes for code=%s hash=%s", code, cp_hash
        )
        return None

    subprocess.run(["git", "add", "-A"], cwd=tenant_root, check=True)
    message = (
        f"[clickup-close] {code}: hash {cp_hash}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=tenant_root,
        check=True,
        env=env,
    )
    target_branch = os.environ.get("CP_TENANT_BRANCH", "main")
    if target_branch != "main":
        subprocess.run(
            ["git", "branch", "-M", target_branch], cwd=tenant_root, check=True
        )
    _push_with_retry(tenant_root, target_branch=target_branch, env=env)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# ─────────────────────────────────────────────────────────────
#  Tenant repo lifecycle (clone-on-each-request)
# ─────────────────────────────────────────────────────────────


@contextmanager
def _cloned_tenant():
    """Clone cp tenant into temp dir; yield path; always clean up."""
    repo_url = os.environ.get("CP_TENANT_REPO_URL")
    if not repo_url:
        raise HTTPException(status_code=500, detail="CP_TENANT_REPO_URL not configured")

    tmp = Path(tempfile.mkdtemp(prefix="cp-webhook-"))
    try:
        env = _ssh_env()
        subprocess.run(
            ["git", "clone", "--depth=10", repo_url, str(tmp / "cp")],
            check=True,
            env=env,
            capture_output=True,
        )
        # Configure committer once per clone so every commit picks it up.
        subprocess.run(
            ["git", "config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "cp-engine-webhook")],
            cwd=tmp / "cp",
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "webhook@firstperson.is")],
            cwd=tmp / "cp",
            check=True,
        )
        yield tmp / "cp"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ssh_env() -> dict:
    """Build a subprocess env that uses GIT_SSH_KEY for the clone/push."""
    env = os.environ.copy()
    key_material = os.environ.get("GIT_SSH_KEY")
    if not key_material:
        return env

    # Materialize the key once per request to a tempfile that we'll point
    # GIT_SSH_COMMAND at. Container has tmpfs at /tmp, fine for ephemeral keys.
    key_path = Path(tempfile.mkdtemp(prefix="cp-webhook-key-")) / "id_ed25519"
    key_path.write_text(key_material if key_material.endswith("\n") else key_material + "\n")
    key_path.chmod(0o600)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    )
    return env


# ─────────────────────────────────────────────────────────────
#  Per-project ingest
# ─────────────────────────────────────────────────────────────


def _speaker_names(participants) -> list[str]:
    """Extract participant display names defensively.

    Fathom's `participants` is a list whose items may be dicts (with a
    `name` field) or bare strings. Anything else is ignored. Mirrors the
    parsing already done in meeting_artifact._build_markdown.
    """
    names: list[str] = []
    for p in participants or []:
        if isinstance(p, dict) and p.get("name"):
            names.append(p["name"])
        elif isinstance(p, str) and p:
            names.append(p)
    return names


def _plan_decisions(plan: dict | None, code: str) -> list[str]:
    """Pull decision texts out of the generated plan's project block, if any.

    The plan's `record-decision` verb (when present) carries one dict per
    decision; we extract a text/description field defensively. Returns []
    if the plan has no decisions for this project.
    """
    if not plan:
        return []
    block = (plan.get("projects") or {}).get(code) or {}
    out: list[str] = []
    for item in block.get("record-decision") or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("decision") or item.get("description")
            if text:
                out.append(str(text).strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _action_item_texts(action_items: list[dict] | None) -> list[str]:
    """Extract a text/description field from each Fathom action item."""
    out: list[str] = []
    for item in action_items or []:
        if isinstance(item, dict):
            text = item.get("description") or item.get("text") or item.get("title")
            if text:
                out.append(str(text).strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _append_retrospective(
    *,
    config: TenantConfig,
    code: str,
    meeting: dict | None,
    action_items: list[dict] | None,
    plan: dict | None,
) -> str:
    """Append the meeting's WHOLE Fathom summary to the project's Retrospective.

    Best-effort, never raises. Returns one of:
      - "skipped"   — no meeting, or no summary to preserve
      - "appended"  — a new entry was written
      - "duplicate" — meeting already present (idempotency no-op)
      - "error"     — an exception was caught (logged); ingest continues

    The Fathom `summary` is embedded WHOLE (anti-compression); decisions /
    action items / links are ADDED as structured pointers, never a
    replacement. The history file is committed by the caller's normal
    `git add -A` commit.
    """
    if meeting is None:
        return "skipped"
    summary = (meeting.get("summary") or "").strip()
    if not summary:
        return "skipped"

    try:
        meeting_id = str(meeting.get("id") or "")
        entry_md = build_entry(
            date=str(meeting.get("meeting_date") or ""),
            title=str(meeting.get("title") or "Untitled meeting"),
            speakers=_speaker_names(meeting.get("participants")),
            summary=summary,
            decisions=_plan_decisions(plan, code),
            action_items=_action_item_texts(action_items),
            recording_url=meeting.get("fathom_url"),
            transcript_link=None,
            meeting_id=meeting_id or None,
        )
        history_path = (
            find_shell_dir(config.root, code)
            / "shell"
            / "Retrospective"
            / "meeting-history.md"
        )
        wrote = append_entry(
            history_path,
            meeting_id,
            entry_md,
            code=code,
            project=code,
            today=datetime.now().date(),
        )
        return "appended" if wrote else "duplicate"
    except ShellDirNotFound as exc:
        log.warning("retrospective: no shell dir for %s: %s", code, exc)
        return "error"
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("retrospective: append failed for %s: %s", code, exc)
        return "error"


def _ingest_one_project(
    *,
    config: TenantConfig,
    code: str,
    transcript_path: Path,
    action_items: list[dict] | None = None,
    meeting_id: str | None = None,
    meeting: dict | None = None,
) -> dict:
    """Generate plan + execute for a single project. Returns a summary dict.

    `action_items` (the Fathom meeting's structured action_items JSONB)
    is threaded into `generate_plan` where each item becomes a
    deterministic `record-ask` appended to the plan. Default-None keeps
    the function callable from any other site that hasn't fetched the
    meeting row yet.

    `meeting_id` is threaded into ``execute_plan`` so the v0.15+
    ClickUp-proposal verbs (``set-milestone`` / ``set-client-ask-task``)
    can stamp the originating meeting onto each proposal row.
    """
    entry = {
        "code": code,
        "plan_summary": None,
        "files_written": [],
        "skipped_duplicate": 0,
        "errors": [],
    }
    try:
        gen = generate_plan(
            config=config,
            project_code=code,
            transcript_path=transcript_path,
            action_items=action_items,
        )
    except PlanGenerationError as exc:
        entry["errors"].append(f"plan generation failed: {exc}")
        log.warning("plan generation failed for %s: %s", code, exc)
        return entry

    # Summarize what's in the plan for logging + observability.
    projects = gen.plan.get("projects") or {}
    project_block = projects.get(code) or {}
    entry["plan_summary"] = {verb: len(items) for verb, items in project_block.items()}

    try:
        exec_result = execute_plan(
            gen.plan,
            tenant_root=config.root,
            today=datetime.now().date(),
            supabase=_create_supabase_client(),
            meeting_id=meeting_id,
        )
    except IngestPlanError as exc:
        entry["errors"].append(f"plan execution failed: {exc}")
        log.warning("plan execution failed for %s: %s", code, exc)
        return entry

    entry["files_written"] = [str(p) for p in exec_result.files_written]
    entry["skipped_duplicate"] = exec_result.skipped_duplicate
    entry["errors"].extend(exec_result.errors)

    # Retrospective append (spine-inversion Part B). Best-effort: never
    # raises, never appends to entry["errors"], so a retrospective failure
    # can't flip the project's ingest status to "failed". The new file is
    # picked up by the caller's normal `git add -A` commit.
    entry["retrospective"] = _append_retrospective(
        config=config,
        code=code,
        meeting=meeting,
        action_items=action_items,
        plan=gen.plan,
    )
    return entry


# ─────────────────────────────────────────────────────────────
#  Transcript handling
# ─────────────────────────────────────────────────────────────


def _normalize_transcript(transcript_field) -> str:
    """Normalize Fathom's transcript field into a single readable string.

    Four shapes show up in production:
      1. None / empty → ""
      2. plain string → returned as-is (older webhook payload format)
      3. JSONB object with "text"/"plain_text" key → that value
      4. JSONB list of segments `[{text, speaker:{display_name}, timestamp}, ...]`
         → rendered as `<ts> - <speaker>\\n  <text>` blocks matching the
         staged-transcript file format that plan_from_transcript was
         tuned against.

    Shape 4 is the standard across all current Fathom-delivered meetings;
    the earlier `str(...)` fallback turned a Python list into its repr
    (which silently passed validation but produced garbage prompts).
    """
    if transcript_field is None:
        return ""
    if isinstance(transcript_field, str):
        return transcript_field
    if isinstance(transcript_field, list):
        return _segments_to_text(transcript_field)
    if isinstance(transcript_field, dict):
        if transcript_field.get("text"):
            return transcript_field["text"]
        if transcript_field.get("plain_text"):
            return transcript_field["plain_text"]
        segments = transcript_field.get("segments")
        if isinstance(segments, list):
            return _segments_to_text(segments)
    return ""


def _segments_to_text(segments: list) -> str:
    """Render Fathom segments as `<ts> - <speaker>\\n  <text>` blocks."""
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker_obj = seg.get("speaker") or {}
        speaker = (
            (speaker_obj.get("display_name") if isinstance(speaker_obj, dict) else None)
            or seg.get("display_name")
            or seg.get("speaker_name")
            or "Unknown"
        )
        ts = seg.get("timestamp") or seg.get("ts") or ""
        header = f"{ts} - {speaker}" if ts else speaker
        lines.append(f"{header}\n  {text}")
    return "\n\n".join(lines)


def _fetch_transcript(meeting_id: str) -> str:
    """Pull a transcript from Supabase fathom_meetings table."""
    try:
        from supabase import create_client
    except ImportError as exc:
        raise HTTPException(
            status_code=500, detail="supabase package not installed"
        ) from exc

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise HTTPException(
            status_code=500, detail="SUPABASE_URL or SUPABASE_SERVICE_KEY not set"
        )

    client = create_client(url, key)
    resp = (
        client.table("fathom_meetings")
        .select("id, title, meeting_date, transcript, participants")
        .eq("id", meeting_id)
        .single()
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")

    text = _normalize_transcript(resp.data.get("transcript"))
    if not text:
        raise HTTPException(
            status_code=422, detail=f"meeting {meeting_id} has no transcript text"
        )

    header = (
        f"# {resp.data.get('title', 'Meeting')}\n"
        f"# Date: {resp.data.get('meeting_date', 'unknown')}\n"
        f"# Meeting ID: {meeting_id}\n\n"
    )
    return header + text


def _fetch_meeting(meeting_id: str) -> dict | None:
    """Pull the full fathom_meetings row needed for per-meeting artifacts.

    Includes `summary` and `action_items` — fields the plan-generation
    path drops but the meeting-artifact path needs. Best-effort: returns
    None on any failure so artifact generation degrades gracefully
    without breaking auto-ingest.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("meeting-artifact: Supabase env not set; skipping fetch")
        return None
    try:
        from supabase import create_client

        client = create_client(url, key)
        resp = (
            client.table("fathom_meetings")
            .select(
                "id, title, meeting_date, summary, action_items, "
                "participants, duration_minutes, fathom_url"
            )
            .eq("id", meeting_id)
            .single()
            .execute()
        )
        return resp.data or None
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("meeting-artifact: meeting fetch failed for %s: %s", meeting_id, exc)
        return None


def _stage_transcript(tenant_root: Path, meeting_id: str, text: str) -> Path:
    """Write transcript to tenant's transcripts/incoming/ for the prompt + audit."""
    incoming = tenant_root / "transcripts" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    filename = f"auto-ingest-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{meeting_id[:8]}.txt"
    path = incoming / filename
    path.write_text(text, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
#  Commit + push
# ─────────────────────────────────────────────────────────────


# Markers that git emits on a non-fast-forward push reject (the case
# we can recover from with a pull --rebase). Anything else (auth, hook
# rejection, network) is raised straight through — we don't want to
# loop on those.
#
# NB: a bare "rejected" marker was previously here too but matched every
# kind of push reject (auth failures, pre-receive hooks, branch
# protection), wasting two pull-rebase round-trips on each before
# bottoming out. The two markers below are what git actually emits for
# the non-ff race condition; auth/hook rejects raise immediately.
_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "(non-fast-forward)",
    "fetch first",
)


def _push_with_retry(
    tenant_root: Path,
    *,
    target_branch: str,
    env: dict,
    max_attempts: int = 3,
) -> None:
    """``git push origin <branch>`` with rebase-on-reject recovery.

    Concurrent auto-ingest webhooks each clone independently and race on
    push. The loser of the race gets a non-fast-forward rejection. This
    helper recovers by ``git pull --rebase origin <branch>`` and trying
    again. After ``max_attempts`` consecutive failures, the last error
    is re-raised so the request 500s and Fathom can retry the whole
    pipeline cleanly (rather than wedging mid-rebase).

    Important: if the ``pull --rebase`` itself fails (e.g., true content
    conflict on the same line), we run ``git rebase --abort`` to leave
    the working tree on a clean detached state and then raise. We do
    NOT try to auto-resolve — that would silently overwrite one webhook
    call's bullet with another's.

    Modelled on src/cp_engine/capture_session.py:_push_with_retry but
    parameterized for the webhook's per-request SSH env + named-branch
    push.
    """
    last_err: subprocess.CalledProcessError | None = None
    for attempt in range(1, max_attempts + 1):
        push = subprocess.run(
            ["git", "push", "origin", target_branch],
            cwd=tenant_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if push.returncode == 0:
            if attempt > 1:
                log.info(
                    "push succeeded on attempt %d (after rebase)", attempt
                )
            return

        last_err = subprocess.CalledProcessError(
            push.returncode, push.args, output=push.stdout, stderr=push.stderr
        )

        stderr_lc = (push.stderr or "").lower()
        is_non_ff = any(m in stderr_lc for m in _NON_FAST_FORWARD_MARKERS)
        if not is_non_ff or attempt == max_attempts:
            # Either a non-recoverable class of failure (auth, hook
            # reject, network) or we've exhausted retries. Surface it.
            if not is_non_ff:
                log.warning(
                    "push failed with non-recoverable error: %s",
                    (push.stderr or "")[:240],
                )
            raise last_err

        log.warning(
            "push rejected non-fast-forward (attempt %d/%d); "
            "rebasing and retrying",
            attempt, max_attempts,
        )
        rebase = subprocess.run(
            ["git", "pull", "--rebase", "origin", target_branch],
            cwd=tenant_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if rebase.returncode != 0:
            # Don't leave the tenant in a mid-rebase state — abort so
            # the next clone (whether this same request or a Fathom
            # retry) starts from a clean tree. Then surface the
            # original push failure: that's the operationally
            # actionable signal.
            log.warning(
                "pull --rebase failed (%s); aborting rebase and giving up",
                (rebase.stderr or "")[:240],
            )
            abort = subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=tenant_root,
                env=env,
                capture_output=True,
                text=True,
            )
            if abort.returncode != 0:
                # Don't swallow this — a wedged worktree is the kind of
                # thing the operator needs to see in logs (next clone
                # may inherit a half-rebase state).
                log.warning(
                    "git rebase --abort failed (rc=%d): %s",
                    abort.returncode,
                    (abort.stderr or "")[:200],
                )
            raise last_err


def _commit_and_push(
    *, tenant_root: Path, meeting_id: str, ingested: list[dict]
) -> str:
    """Stage + commit + push. Returns the new HEAD SHA."""
    env = _ssh_env()

    subprocess.run(["git", "add", "-A"], cwd=tenant_root, check=True)
    codes = ", ".join(e["code"] for e in ingested if e["files_written"])
    summary_lines = []
    for e in ingested:
        if not e["files_written"]:
            continue
        verbs = ", ".join(f"{k}={v}" for k, v in (e["plan_summary"] or {}).items())
        summary_lines.append(f"- {e['code']}: {verbs}")
    body = "\n".join(summary_lines)

    message = (
        f"[auto-ingest] {codes}: meeting {meeting_id[:8]}\n\n"
        f"{body}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
    )

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=tenant_root,
        check=True,
        env=env,
    )
    # CP_TENANT_BRANCH lets local tests push to a throwaway branch rather
    # than main. Production deploys leave it unset so the default applies.
    target_branch = os.environ.get("CP_TENANT_BRANCH", "main")
    # The clone lands the remote default (main) into local main; if we're
    # targeting a different branch, rename HEAD first.
    if target_branch != "main":
        subprocess.run(
            ["git", "branch", "-M", target_branch],
            cwd=tenant_root,
            check=True,
        )
    _push_with_retry(tenant_root, target_branch=target_branch, env=env)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_meeting_artifacts(
    *, tenant_root: Path, meeting_id: str, artifact_paths: list[Path]
) -> str | None:
    """Commit + push the per-meeting artifact files.

    Separate from _commit_and_push because a meeting can produce an
    artifact even when it wrote no sprint-file bullets (so the per-project
    commit loop would never fire). Stages only the artifact paths.

    Best-effort: returns None on failure rather than raising — a failed
    artifact commit must not break the auto-ingest contract.
    """
    if not artifact_paths:
        return None
    try:
        env = _ssh_env()
        rels = [str(p.relative_to(tenant_root)) for p in artifact_paths]
        subprocess.run(["git", "add", *rels], cwd=tenant_root, check=True)

        # If the sprint-file commit already swept these in via `git add
        # -A`, there's nothing staged here — bail quietly.
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=tenant_root, env=env
        )
        if staged.returncode == 0:
            return None

        message = (
            f"[auto-ingest] meeting artifacts: meeting {meeting_id[:8]}\n\n"
            f"Per-meeting synthesis + transcript for {len(rels)} file(s).\n"
            f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
        )
        subprocess.run(
            ["git", "commit", "-m", message], cwd=tenant_root, check=True, env=env
        )
        target_branch = os.environ.get("CP_TENANT_BRANCH", "main")
        _push_with_retry(tenant_root, target_branch=target_branch, env=env)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tenant_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "meeting-artifact: commit failed for meeting=%s: %s", meeting_id, exc
        )
        return None


def _generate_meeting_artifacts(
    *,
    tenant_root: Path,
    meeting_id: str,
    transcript_text: str,
    project_codes: list[str],
    meeting: dict | None = None,
) -> dict:
    """Fetch the meeting, write per-meeting artifacts, commit them.

    Shared by all auto-ingest endpoints. Best-effort — never raises.
    Returns a summary dict for the response payload.

    `meeting` lets a caller pass in an already-fetched fathom_meetings
    row to skip the redundant Supabase round-trip. The /api/auto-ingest
    endpoint fetches the meeting up front to extract `action_items` for
    plan generation; it passes the same row here. Other endpoints that
    don't pre-fetch leave `meeting=None` and we fetch ourselves.
    """
    summary = {"files_written": 0, "commit_sha": None}
    if not project_codes:
        return summary
    try:
        if meeting is None:
            meeting = _fetch_meeting(meeting_id)
        if meeting is None:
            return summary
        paths = write_meeting_artifacts(
            tenant_root=tenant_root,
            meeting=meeting,
            transcript_text=transcript_text,
            project_codes=project_codes,
        )
        summary["files_written"] = len(paths)
        if paths:
            sha = _commit_meeting_artifacts(
                tenant_root=tenant_root,
                meeting_id=meeting_id,
                artifact_paths=paths,
            )
            summary["commit_sha"] = sha
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("meeting-artifact: generation step failed: %s", exc)
    return summary


# ─────────────────────────────────────────────────────────────
#  Config bootstrap
# ─────────────────────────────────────────────────────────────


def _load_tenant_config(tenant_root: Path) -> TenantConfig:
    """Load .cp-engine.toml from the freshly-cloned tenant root.

    The webhook deployment doesn't get .cp-engine.local.toml — that's
    a per-machine file with absolute paths to project clones. cp_engine.config.load()
    already treats local as optional when the committed config declares
    no [[projects]] (the CI-runner case), which is true for the cp tenant
    (MC-2-backed, no committed [[projects]]). So plain load() just works.
    """
    from cp_engine import config as cfg_mod

    return cfg_mod.load(tenant_root=tenant_root)


# ─────────────────────────────────────────────────────────────
#  Observability (Phase C.4)
# ─────────────────────────────────────────────────────────────


def _status_from_ingested(ingested: list[dict], *, anything_wrote: bool) -> str:
    """Derive the auto_ingest_runs.status enum value.

    - 'failed': at least one project errored out
    - 'skipped_no_op': nothing wrote AND nothing errored (clean no-op)
    - 'success': we wrote files (used in the commit-and-push branch)
    """
    if any(entry.get("errors") for entry in ingested):
        return "failed"
    if not anything_wrote:
        return "skipped_no_op"
    return "success"


def _find_successful_duplicate_run(
    meeting_id: str, project_codes: list[str]
) -> str | None:
    """Return the existing ``auto_ingest_runs.id`` if a successful run
    already exists for this (meeting_id, sorted project_codes) tuple.

    Used by /api/auto-ingest to short-circuit Fathom's retry-on-timeout
    behavior. A duplicate delivery is harmless thanks to the content-hash
    dedupe inside execute_plan, but the second pass still spends a Claude
    call + git push for zero net change and pollutes auto_ingest_runs
    with two rows. This check turns that into a 200 with
    ``status: duplicate_delivery_skipped`` and the original run_id.

    Best-effort: any Supabase failure logs and returns None, so a
    transient observability outage degrades to "process every request"
    rather than "fail every request".

    Project codes are sorted before comparison so the dedupe key is
    independent of the caller's ordering — Fathom may not preserve
    array order between retries.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key or create_client is None:
        return None
    sorted_codes = sorted(project_codes)
    try:
        client = create_client(url, key)
        # Filter to status='success' — a previous failure should NOT
        # block a fresh attempt. Limit 20 lets us tolerate a handful
        # of past runs against the same meeting while still being a
        # single-page query.
        resp = (
            client.table("auto_ingest_runs")
            .select("id, project_codes")
            .eq("meeting_id", meeting_id)
            .eq("status", "success")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        for row in resp.data or []:
            row_codes = row.get("project_codes") or []
            if sorted(row_codes) == sorted_codes:
                return row.get("id")
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "duplicate-delivery check failed for meeting=%s: %s",
            meeting_id, exc,
        )
    return None


def _log_run_to_supabase(
    *,
    meeting_id: str,
    project_codes: list[str],
    status: str,
    ingested: list[dict],
    commit_sha: str | None,
    top_level_errors: list[str] | None = None,
) -> None:
    """Insert one row into auto_ingest_runs. Never raises.

    Observability is best-effort: a failure to log must not break the
    primary auto-ingest contract with fathom-meeting-sync. We log + swallow.

    `top_level_errors` captures failures that happen BEFORE the per-project
    loop runs (plan generation crashed, transcript fetch failed, etc.) —
    cases where `ingested=[]` so the per-project error aggregation has
    nothing to surface. Without this, top-level failures landed in
    auto_ingest_runs as status=failed but errors=null, leaving the
    dashboard unable to show what actually went wrong.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("auto_ingest_runs insert skipped: Supabase env not set")
        return

    plan_summary = {
        entry["code"]: entry.get("plan_summary") or {}
        for entry in ingested
    }
    errors_flat: list[str] = list(top_level_errors or [])
    for entry in ingested:
        for err in entry.get("errors") or []:
            errors_flat.append(f"{entry['code']}: {err}")

    try:
        from supabase import create_client

        client = create_client(url, key)
        client.table("auto_ingest_runs").insert({
            "meeting_id": meeting_id,
            "project_codes": project_codes,
            "status": status,
            "plan_summary": plan_summary,
            "commit_sha": commit_sha,
            "errors": errors_flat or None,
        }).execute()
    except Exception as exc:  # noqa: BLE001 — observability must never throw
        log.warning("auto_ingest_runs insert failed for %s: %s", meeting_id, exc)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
