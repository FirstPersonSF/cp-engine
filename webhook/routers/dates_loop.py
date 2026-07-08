"""Dates-loop route: the weekly Slack what's-coming-due post.

Commitments consolidation, cp-engine #38. The webhook has no scheduler —
like every other scheduled trigger, the Monday cron lives in
fathom-meeting-sync (same Railway project) and HMAC-POSTs here.
Design: cp/docs/plans/2026-07-07-commitments-consolidation-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging

import git_ops
import observability
import pipeline
import signatures
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


@router.post("/dates-loop")
async def dates_loop(request: Request) -> dict:
    """Run the weekly Slack dates loop and apply ratification write-backs.

    Renders one post per active project/initiative with open commitments
    or MC-2 schedule milestones inside the window (due this week / next N
    days / needs a date / slipped) plus a tenant-wide partners rollup,
    posts each to its mapped Slack channel(s), then bumps posted_count /
    promotes proposed→agreed / stamps slipped in ``public.commitments``.

    Request body (JSON, all optional):
        {"dry_run": false, "window_days": 14, "today": "YYYY-MM-DD"}

    ``dry_run=true`` renders and returns everything without posting or
    touching ratification state — the review path before the cron is
    enabled, and the safe default for manual pokes.

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(...))
        X-Webhook-Timestamp: (optional, per the phased-rollout gate)

    Response (200):
        {"ok": true, "posts": <n>, "posted": <n>, "partners_posted": bool,
         "skipped_no_channel": [...], "ratification": {...},
         "errors": [...], "dry_run": bool,
         "rendered": [{"code", "channels", "text"}, ...]}  # dry_run only
    """
    from datetime import date as _date

    from cp_engine.dates_loop import run_dates_loop

    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    payload: dict = {}
    if raw_body.strip():
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    dry_run = bool(payload.get("dry_run", False))
    window_days = payload.get("window_days")
    today_raw = payload.get("today")
    today = None
    if today_raw:
        try:
            today = _date.fromisoformat(str(today_raw))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid today date: {today_raw}"
            ) from exc

    def _run():
        # The loop needs TenantConfig (channel map, [dates_loop] block) —
        # clone the tenant like every other engine-invoking route. No file
        # writes happen; the clone is config-read-only.
        with git_ops._cloned_tenant() as tenant_root:
            config = pipeline._load_tenant_config(tenant_root)
            return run_dates_loop(
                config, today=today, post=not dry_run, window_days=window_days
            )

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 — surface as a clean 500
        observability.capture(exc, area="dates_loop")
        raise HTTPException(status_code=500, detail=f"dates-loop failed: {exc}") from exc

    response = {
        "ok": not result.errors,
        "dry_run": dry_run,
        "posts": len(result.posts),
        "posted": sum(1 for p in result.posts if p.posted),
        "partners_posted": result.partners_posted,
        "skipped_no_channel": result.skipped_no_channel,
        "ratification": {
            "posted_count_bumped": result.posted_count_bumped,
            "agreed_promoted": result.agreed_promoted,
            "slipped_stamped": result.slipped_stamped,
        },
        "errors": result.errors,
    }
    if dry_run:
        response["rendered"] = [
            {"code": p.code, "channels": list(p.channel_ids), "text": p.text}
            for p in result.posts
        ]
        response["partners_text"] = result.partners_text
    return response
