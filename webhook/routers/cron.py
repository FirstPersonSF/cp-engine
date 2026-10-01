"""Scheduled jobs, triggered by a Railway cron: ``POST /api/cron/{job}``.

WHY. GitHub Actions' scheduler ran this tenant's crons 4–8 hours late
(measured 2026-09/10: the 14:00 UTC sync ran 18:02 / 20:21 / 19:05; the 09:47
daily digest 14:49–17:48; the 12:23 health line had not fired by 13:53). A
"morning" health line that lands in the afternoon is not one. Railway's cron
fires on the minute, so the schedule moves there: a tiny trigger service
(``webhook/cron/trigger.py``) signs and POSTs ``{"slot": "<cron>"}`` here, and
the work runs in this service, which already holds the tenant deploy key, the
MC-2 service key and the Slack bot token.

CONTRACT.

    POST /api/cron/{job}
    Headers: X-Webhook-Signature, X-Webhook-Timestamp — the same HMAC scheme
             as every other route (`signatures._verify_signature`).
    Body:    {"slot": "23 12 * * *", "dry_run": false}

    200 {"ok": true, "status": "posted",    "posted_ts": "...", ...}
    200 {"ok": true, "status": "skipped",   "reason": "not the morning slot"}
    200 {"ok": true, "status": "duplicate", "posted_ts": "<the earlier post>"}
    200 {"ok": true, "status": "dry_run",   "lines": "..."}       (no post)
    400 bad JSON / bad slot · 401 bad signature · 404 unknown job
    502 {"ok": false, "status": "failed", "error": "health line NOT posted: ..."}

The ``webhook_runs`` row (written by `run_ledger.RunLedgerMiddleware`) carries
the outcome in ``detail`` (``{"job", "slot", "day", "outcome", "posted_ts"}``):
its status column is ok for posted/skipped/duplicate (the table's CHECK has no
'skipped'; the outcome says which), failed for a post that did not land,
rejected for a bad signature.

IDEMPOTENCY. One post per tenant day. Before posting, the job reads today's
ledger rows for this route; a row whose detail says ``outcome: posted`` for
today short-circuits to ``duplicate``. A process-local memo covers the window
between a post and its ledger row landing (the middleware writes after the
response), and a lock serialises concurrent retries. If the ledger cannot be
read the job still posts — a health line that stays silent because MC-2 is
down hides the thing it reports — but says so in ``warnings`` (→ a partial
row), never quietly.

ADDING A JOB. Write ``_job_<name>(payload) -> (http_status, body)`` and add it
to ``JOBS``. The route does signature, JSON and dispatch; the job owns its
gating and idempotency. (``draft-summaries`` is the next candidate; not here
yet.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import git_ops
import observability
import pipeline
import run_ledger
import signatures
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from cp_engine import daily_health, mc2_db

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()

_SLOT_RE = re.compile(r"^\d{1,2} \d{1,2} \* \* \*$")

# (job, tenant day) → Slack ts of the post that landed. See IDEMPOTENCY.
_posted_memo: dict[tuple[str, str], str] = {}
_job_lock = threading.Lock()


def _now() -> datetime:
    """Tenant wall clock, aware. A seam for tests (PDT vs PST)."""
    from cp_engine.clock import tenant_now, tenant_timezone

    return tenant_now().replace(tzinfo=tenant_timezone())


def _prior_post(job: str, day: str, now: datetime) -> tuple[str | None, str | None]:
    """(posted_ts, None) if today's ledger already holds a posted run of this
    job; (None, None) if not; (None, why) if the ledger could not be read."""
    memo = _posted_memo.get((job, day))
    if memo:
        return memo, None
    try:
        db = mc2_db.get_client(required=False)
        if db is None:
            return None, "ledger unreadable (Supabase env not set)"
        since = (now - timedelta(hours=30)).isoformat()
        rows = (db.table(run_ledger.WEBHOOK_RUNS)
                .select("detail, created_at")
                .eq("route", f"/api/cron/{job}")
                .in_("status", ["ok", "partial"])
                .gte("created_at", since)
                .order("created_at", desc=True)
                .limit(50).execute().data) or []
    except Exception as exc:  # noqa: BLE001 — reported in the response, not hidden
        return None, f"ledger unreadable ({type(exc).__name__}: {exc})"
    for r in rows:
        d = r.get("detail") or {}
        if d.get("job") == job and d.get("day") == day and d.get("outcome") == "posted":
            return d.get("posted_ts") or "?", None
    return None, None


def _job_health(payload: dict) -> tuple[int, dict]:
    """The daily health line — `cxp health --post --scheduled <slot>`, here."""
    slot = payload.get("slot")
    if not isinstance(slot, str) or not _SLOT_RE.match(slot.strip()):
        return 400, {"ok": False, "status": "rejected",
                     "error": f"slot must be a daily cron 'M H * * *', got {slot!r}"}
    slot = slot.strip()
    dry_run = bool(payload.get("dry_run", False))

    with git_ops._read_only_clone() as root:
        # Load first: config.load sets the tenant timezone the gate reads.
        config = pipeline._load_tenant_config(root)
        now = _now()
        day = now.date().isoformat()
        detail = {"job": "health", "slot": slot, "day": day}
        if not dry_run and not daily_health.is_morning_slot(slot, now):
            return 200, {"ok": True, "status": "skipped", "slot": slot, "day": day,
                         "reason": "not the morning slot",
                         "run_detail": {**detail, "outcome": "skipped"}}

        with _job_lock:
            warnings: list[str] = []
            if not dry_run:
                prior, why = _prior_post("health", day, now)
                if prior:
                    return 200, {"ok": True, "status": "duplicate", "slot": slot,
                                 "day": day, "posted_ts": prior,
                                 "run_detail": {**detail, "outcome": "duplicate",
                                                "posted_ts": prior}}
                if why:
                    warnings.append(f"idempotency unverified: {why}")

            client = mc2_db.get_client(config, required=False)
            report = daily_health.gather(tenant_root=root, client=client)
            lines = report.render()
            if dry_run:
                return 200, {"ok": True, "status": "dry_run", "slot": slot,
                             "day": day, "report_ok": report.ok, "lines": lines}
            try:
                ts = daily_health.post(report, config=config, client=client)
            except Exception as exc:  # noqa: BLE001 — a post that failed fails the run
                observability.capture(exc, area="cron_health_post")
                return 502, {"ok": False, "status": "failed", "slot": slot, "day": day,
                             "error": f"health line NOT posted: {type(exc).__name__}: {exc}",
                             "lines": lines,
                             "run_detail": {**detail, "outcome": "failed"}}
            _posted_memo[("health", day)] = ts

    body = {"ok": True, "status": "posted", "slot": slot, "day": day,
            "posted_ts": ts, "report_ok": report.ok, "lines": lines,
            "run_detail": {**detail, "outcome": "posted", "posted_ts": ts}}
    if warnings:
        body["warnings"] = warnings
    return 200, body


JOBS: dict[str, Callable[[dict], tuple[int, dict]]] = {
    "health": _job_health,
}


@router.post("/api/cron/{job}")
async def cron_job(job: str, request: Request) -> JSONResponse:
    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )
    fn = JOBS.get(job)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"unknown cron job {job!r}")
    try:
        payload = json.loads(raw_body) if raw_body.strip() else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        status, body = await asyncio.to_thread(fn, payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — a clean 500 with the reason
        observability.capture(exc, area=f"cron_{job}")
        log.error("cron %s failed: %s", job, exc)
        return JSONResponse(status_code=500, content={
            "ok": False, "status": "failed", "error": f"cron {job} failed: "
            f"{type(exc).__name__}: {exc}"})
    log.info("cron %s: %s", job, body.get("status"))
    return JSONResponse(status_code=status, content=body)
