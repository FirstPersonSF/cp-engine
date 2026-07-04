"""Auto-ingest routes: per-meeting, rerun, account, sprint-planning.

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
from fastapi import APIRouter, HTTPException, Request

from cp_engine import mc2_db
from cp_engine.ingest import IngestPlanError, execute_plan
from cp_engine.mc2_db import Tables
from cp_engine.plan_from_account_meeting import (
    AccountPlanError,
    generate_account_plan,
    generate_sprint_planning_plan,
    list_active_for_company,
    list_active_for_scope,
)

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


@router.post("/api/auto-ingest")
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
    signatures._verify_signature(
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
    dup = pipeline._find_successful_duplicate_run(meeting_id, project_codes)
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

    return pipeline._perform_auto_ingest(
        meeting_id=meeting_id,
        project_codes=project_codes,
        transcript_text=transcript_text,
    )


@router.post("/api/auto-ingest/runs/{run_id}/rerun")
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
    signatures._verify_signature(
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
    require_ts = signatures._truthy_env("WEBHOOK_REQUIRE_TIMESTAMP")
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

    client = mc2_db.get_client(required=False)
    if client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for rerun"
        )
    resp = (
        client.table(Tables.AUTO_INGEST_RUNS)
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
    return pipeline._perform_auto_ingest(
        meeting_id=meeting_id,
        project_codes=project_codes,
    )


@router.post("/api/auto-ingest-account")
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
    signatures._verify_signature(
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
        transcript_text = pipeline._fetch_transcript(meeting_id)

    log.info(
        "auto-ingest-account start: meeting=%s company=%s",
        meeting_id, company_code,
    )

    with git_ops._cloned_tenant() as tenant_root:
        config = pipeline._load_tenant_config(tenant_root)

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
            pipeline._log_run_to_supabase(
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
        transcript_path = pipeline._stage_transcript(tenant_root, meeting_id, transcript_text)

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
            pipeline._log_run_to_supabase(
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

        # The whole Fathom summary feeds each touched project's Retrospective
        # (Part B: "every meeting that touches a project"). Fetched once;
        # action_items stay None here — they have no per-project attribution
        # on an account meeting (same constraint as the bullet routing above).
        meeting = pipeline._fetch_meeting(meeting_id)

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
                    supabase=pipeline._create_supabase_client(),
                    meeting_id=meeting_id,
                )
            except IngestPlanError as exc:
                entry["errors"].append(f"plan execution failed: {exc}")
                ingested.append(entry)
                continue
            entry["files_written"] = [str(p) for p in exec_result.files_written]
            entry["skipped_duplicate"] = exec_result.skipped_duplicate
            entry["errors"].extend(exec_result.errors)
            entry["retrospective"] = pipeline._append_retrospective(
                config=config,
                code=code,
                meeting=meeting,
                action_items=None,
                plan=single_project_plan,
            )
            ingested.append(entry)
            if exec_result.files_written or entry["retrospective"] == "appended":
                commit_sha = git_ops._commit_and_push(
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
                    supabase=pipeline._create_supabase_client(),
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
                    summary_commit = git_ops._commit_and_push(
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
        artifact_summary = pipeline._generate_meeting_artifacts(
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
            pipeline._log_run_to_supabase(
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
        pipeline._log_run_to_supabase(
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


@router.post("/api/auto-ingest-sprint-planning")
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
    signatures._verify_signature(
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
        transcript_text = pipeline._fetch_transcript(meeting_id)

    log.info(
        "auto-ingest-sprint-planning start: meeting=%s scope=%s",
        meeting_id, scope,
    )

    with git_ops._cloned_tenant() as tenant_root:
        config = pipeline._load_tenant_config(tenant_root)

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
            pipeline._log_run_to_supabase(
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

        transcript_path = pipeline._stage_transcript(tenant_root, meeting_id, transcript_text)

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
            pipeline._log_run_to_supabase(
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

        # Whole Fathom summary → each touched project's Retrospective (Part B).
        # Fetched once; action_items stay None (no per-project attribution).
        meeting = pipeline._fetch_meeting(meeting_id)

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
                    supabase=pipeline._create_supabase_client(),
                    meeting_id=meeting_id,
                )
            except IngestPlanError as exc:
                entry["errors"].append(f"plan execution failed: {exc}")
                ingested.append(entry)
                continue
            entry["files_written"] = [str(p) for p in exec_result.files_written]
            entry["skipped_duplicate"] = exec_result.skipped_duplicate
            entry["errors"].extend(exec_result.errors)
            entry["retrospective"] = pipeline._append_retrospective(
                config=config,
                code=code,
                meeting=meeting,
                action_items=None,
                plan=single_project_plan,
            )
            ingested.append(entry)
            if exec_result.files_written or entry["retrospective"] == "appended":
                commit_sha = git_ops._commit_and_push(
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
                    supabase=pipeline._create_supabase_client(),
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
                    summary_commit = git_ops._commit_and_push(
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
        artifact_summary = pipeline._generate_meeting_artifacts(
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
            pipeline._log_run_to_supabase(
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
        pipeline._log_run_to_supabase(
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
