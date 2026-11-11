"""Close-out route: distil a project as it closes (learning phase 4).

MC-2 calls this (signed) when a workstream's status becomes Closed. It runs
`cp_engine.closing.run_close` in the background — final ingest, a drafted
close-out report committed to the tenant, lesson candidates and freelancer
review requests — and returns 202 at once. The outcome lands in
`webhook_runs` (route `/api/close-out`), one row per run.

Like `/api/sessions/capture`, the report write needs the tenant's WRITE
key, which only this service holds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import git_ops
import observability
import pipeline
import run_ledger
import signatures
from fastapi import APIRouter, HTTPException, Request, Response

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


def _close_sync(code: str, project_id: str) -> dict:
    from cp_engine.render import ENGINE_VERSION
    from cp_engine import asset_ingest
    from cp_engine.closing import CLOSE_MODEL, run_close
    from cp_engine.plan_from_transcript import _call_claude

    client = pipeline._create_supabase_client()
    if client is None:
        raise RuntimeError("Supabase not configured")
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")

    def _ingest() -> dict:
        asset_ingest._clear_listing_cache()
        run = asset_ingest.ingest_project_assets(
            code, mc_project_id=project_id, supabase_url=url, supabase_key=key)
        if run.unconfigured_reason:
            return {"note": f"no folder to ingest ({run.unconfigured_reason})"}
        return {"created": run.created, "versioned": run.versioned,
                "skipped": run.skipped, "failed": run.failed}

    def _llm(prompt: str) -> str:
        return _call_claude(prompt, model=CLOSE_MODEL,
                            api_key=os.environ.get("ANTHROPIC_API_KEY"),
                            timeout=240, project_id=project_id)

    embed = None
    if os.environ.get("VOYAGE_API_KEY"):
        from cp_engine.lessons import voyage_embedder

        embed = voyage_embedder("document")

    # Ingest first, outside the tenant lock: it touches only MC-2 and can
    # take minutes.
    ingest_result: dict
    try:
        ingest_result = {"ok": True, **_ingest()}
    except Exception as exc:  # noqa: BLE001
        ingest_result = {"ok": False, "error": str(exc)[:300]}

    def _publish(report_file):
        # The drafted report goes to the project's Dropbox spine folder and is
        # ingested, so MC-2's job page shows it and search finds it.
        from cp_engine.closing import publish_report

        def _ingest_one(report_file):
            from cp_engine.spine_promote import ingest_single_file

            res = ingest_single_file(str(report_file), project_id, report_file.name,
                                     supabase_url=url, supabase_key=key)
            return {"ingested": bool(res)}

        return publish_report(client, asset_ingest._dropbox_connector(), project_id=project_id,
                              code=code, report_file=report_file, ingest=_ingest_one)

    with git_ops._cloned_tenant() as tenant_root:
        out = run_close(client, code=code, project_id=project_id, tenant_root=tenant_root,
                        llm=_llm, ingest=lambda: ingest_result, embed=embed,
                        engine_version=ENGINE_VERSION, publish=_publish)
        if out.get("report_path"):
            out["commit"] = git_ops._commit_with_message_and_push(
                tenant_root, f"[close-out] {code}: draft close-out report (owner to confirm)")
    return out


async def _run_close(code: str, project_id: str) -> None:
    started = time.monotonic()
    try:
        out = await asyncio.to_thread(_close_sync, code, project_id)
        failed = [k for k, v in out["steps"].items() if not v.get("ok")]
        run_ledger.record_run(
            route="/api/close-out", status="partial" if failed else "ok",
            project_code=code, detail=out,
            duration_ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 — record, never crash the task
        log.warning("close-out %s failed: %s", code, exc, exc_info=True)
        observability.capture(exc, area="close_out", project_code=code)
        run_ledger.record_run(route="/api/close-out", status="failed", project_code=code,
                              error=str(exc)[:500], detail={"project_id": project_id},
                              duration_ms=int((time.monotonic() - started) * 1000))


@router.post("/api/close-out")
async def close_out_endpoint(request: Request) -> Response:
    """Start a project's Close run. Body: `{"code", "mc_project_id"}`. 202."""
    raw = await request.body()
    signatures._verify_signature(
        raw,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )
    payload = json.loads(raw)
    code = (payload.get("code") or "").strip()
    project_id = (payload.get("mc_project_id") or "").strip()
    if not code or not project_id:
        raise HTTPException(status_code=400, detail="code and mc_project_id are required")
    pipeline._spawn_background(_run_close(code, project_id))
    return Response(content=json.dumps({"status": "running", "code": code}),
                    status_code=202, media_type="application/json")
