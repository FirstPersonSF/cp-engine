"""Asset-ingest route: fire-and-forget Drive/Dropbox ingest for one project.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import observability
import pipeline
import signatures
from fastapi import APIRouter, HTTPException, Request, Response

from cp_engine.mc2_db import Tables

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


def _asset_runs_table(client):
    return client.table(Tables.ASSET_INGEST_RUNS)


async def _run_asset_ingest(
    run_id: str, code: str, mc_project_id: str | None = None,
    folder: str | None = None,
) -> None:
    """Background: run the (sync, slow) asset ingest off the event loop, then
    record the outcome on the asset_ingest_runs row. Never raises — a failure is
    recorded as status=failed (this is the fire-and-forget tail of
    /api/assets/ingest, which has already returned 202 to the caller)."""
    from cp_engine import asset_ingest
    from cp_engine.asset_ingest import _utc_now_iso
    client = pipeline._create_supabase_client()
    # Pass Supabase coords explicitly from the webhook ENV. The webhook runs in a
    # Railway container whose cwd is /app (the webhook app — NOT a tenant
    # checkout), so ingest_project_assets's lazy _resolve_creds() ->
    # cp_config.load(Path.cwd()) would throw "No .cp-engine.toml at /app". When
    # both url+key are supplied, that cwd-config path is never reached.
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    try:
        if not supabase_url or not supabase_key:
            # Fail fast with a clear message rather than letting the cwd-config
            # error surface from deep inside _resolve_creds(). In practice the
            # endpoint already 500s earlier when pipeline._create_supabase_client() is
            # None (same env vars), so this is a belt-and-suspenders guard.
            raise RuntimeError(
                "webhook missing SUPABASE_URL/SUPABASE_SERVICE_KEY env"
            )
        run = await asyncio.to_thread(
            asset_ingest.ingest_project_assets,
            code,
            mc_project_id=mc_project_id,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            only_folder=folder,
        )
        if not run.project_found:
            patch = {
                "status": "failed",
                "error": f"no MC-2 project resolved for '{code}'",
                "finished_at": _utc_now_iso(),
            }
        elif run.unconfigured_reason:
            # Confirm gate (#59): the project resolved but no ENABLED source
            # has a folder id — record a structured refusal, NOT a
            # normal-looking done-with-zero-counts run. The mc-2 button's
            # status line renders `error` for failed runs, so this message is
            # exactly what the user sees under the button.
            patch = {
                "status": "failed",
                "error": (
                    f"No Drive/Dropbox folder configured for '{code}' — set "
                    "the project's folders in MC-2, then re-run. "
                    f"({run.unconfigured_reason})"
                ),
                "source_notes": run.source_notes,
                "finished_at": _utc_now_iso(),
            }
        else:
            patch = {
                "status": "done",
                "created": run.created,
                "versioned": run.versioned,
                "skipped": run.skipped,
                "deduped": run.deduped,
                "failed": run.failed,
                "failures": [{"file": f, "error": e} for f, e in run.failures],
                "source_notes": run.source_notes,
                "finished_at": _utc_now_iso(),
            }
        _asset_runs_table(client).update(patch).eq("id", run_id).execute()
    except Exception as exc:  # noqa: BLE001 — record the whole-run failure, never crash the task
        log.warning("asset-ingest run %s failed: %s", run_id, exc, exc_info=True)
        observability.capture(exc, area="asset_ingest_run")
        try:
            # Rebuild a fresh client rather than reuse `client`: the original may be
            # the thing that failed (transient supabase/network error), so we record
            # the failure on a clean client instead of a possibly-broken one.
            _asset_runs_table(pipeline._create_supabase_client()).update(
                {"status": "failed", "error": str(exc), "finished_at": _utc_now_iso()}
            ).eq("id", run_id).execute()
        except Exception:  # noqa: BLE001 — best effort; nothing else to do
            log.error("asset-ingest run %s: could not record failure", run_id)


@router.post("/api/assets/ingest")
async def asset_ingest_endpoint(request: Request) -> Response:
    """Fire-and-forget asset ingest for one project.

    The mc-2 web UI's "Ingest assets" click lands here (signed). Asset ingest
    takes minutes (Drive/Dropbox list + download + embed), so this endpoint does
    NOT block on it: it verifies the HMAC, inserts a `running` asset_ingest_runs
    row, returns 202 immediately, and runs the ingest in a background task that
    updates the row to done/failed when it finishes. mc-2 polls a status endpoint
    (built later) keyed on the run_id.

    Unlike /api/spine/promote this needs NO tenant clone and NO git push — asset
    ingest writes only to rag_assets + the asset_ingest_runs row.

    Request body (JSON):
        {"code": "<project_code>", "run_id": "<uuid>",
         "mc_project_id": "<projects.id>"}  # code+run_id required; mc_project_id optional

    `mc_project_id` (= `projects.id`) is the authoritative resolution key. When
    present we resolve by it directly, sidestepping the by-number path that
    mis-reads slug codes (e.g. a year in `SAP-vision-update-2026`). When absent
    we fall back to by-code resolution for back-compat (CLI callers).

    Response (202):
        {"run_id": ..., "status": "running"}
    """
    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )
    payload = json.loads(raw_body)
    code = (payload.get("code") or "").strip()
    run_id = (payload.get("run_id") or "").strip()
    mc_project_id = (payload.get("mc_project_id") or "").strip() or None
    folder = (payload.get("folder") or "").strip() or None
    if not code or not run_id:
        raise HTTPException(status_code=400, detail="code and run_id are required")
    client = pipeline._create_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for asset ingest"
        )
    # Best-effort row enrichment with the MC-2 project_id. When the caller gave
    # us the authoritative id, use it directly — skip the by-code resolve, which
    # would fail for slug codes anyway. Otherwise fall back to by-code resolve;
    # the background ingest re-resolves regardless, so a miss here is non-fatal.
    project_id = mc_project_id
    if project_id is None:
        try:
            from cp_engine import asset_ingest
            folders = asset_ingest.resolve_project_folders(client, code)
            project_id = folders.project_id if folders else None
        except Exception:  # noqa: BLE001 — best-effort enrichment; ingest re-resolves
            pass
    from cp_engine.asset_ingest import _utc_now_iso
    _asset_runs_table(client).insert({
        "id": run_id,
        "project_id": project_id,
        "project_code": code,
        "status": "running",
        "started_at": _utc_now_iso(),
    }).execute()
    pipeline._spawn_background(_run_asset_ingest(run_id, code, mc_project_id, folder))
    return Response(
        content=json.dumps({"run_id": run_id, "status": "running"}),
        status_code=202,
        media_type="application/json",
    )
