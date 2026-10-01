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

LONG JOBS: 202 + A BACKGROUND RUN. ``sync`` (the tenant's daily `cxp sync`,
was sync.yml) and ``draft-summaries`` (the Monday Exec Summary drafts, was
draft-summaries.yml) take minutes — longer than the trigger should hold a
request open. The request path validates the slot, gates, checks idempotency,
marks the job in flight and answers::

    202 {"ok": true, "status": "accepted", "slot": ..., "day": ...}
    200 {"ok": true, "status": "skipped",   "reason": ...}          (gate)
    200 {"ok": true, "status": "duplicate", "commit_sha": ...}      (slot+day done)
    200 {"ok": true, "status": "running",   "running": {slot, day}} (one in flight)

The 202 leaves an ``accepted`` ledger row; the background run writes a second
row on ``/api/cron/<job>#background`` when it ends — status ok / partial /
failed, ``detail.outcome`` pushed / no_changes / skipped / dry_run / failed,
plus ``commit_sha``. Per-slot idempotency reads those background rows: a slot
whose run for today ENDED ok (pushed / no_changes / skipped) is a duplicate;
a FAILED run may be retried. A process-local in-flight mark stops a second run
of the same job while one is going (one process — see git_ops SCOPE).

Both jobs clone full history WITHOUT the writer lock (`_full_history_clone`)
and take the lock around commit + push only, which is rebase-or-fail
(`_push_with_retry`) exactly like the workflows' push-with-rebase.sh. Both
commit as ``cp-engine-bot`` with the workflows' messages, so the tenant's
history reads the same whichever scheduler ran them.

ADDING A JOB. Write ``_job_<name>(payload) -> (http_status, body)`` and add it
to ``JOBS``. The route does signature, JSON and dispatch; the job owns its
gating and idempotency. A long job returns ``(202, body, work)`` via
`_accept_background`; ``work()`` runs off the request and returns
``(ledger_status, detail)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
            # What the line leaves out (UI rule 1): logs + body + run row.
            check_errors = report.errors()
            for label, error in check_errors.items():
                log.info("cron health: %s: %s", label, error)
            if check_errors:
                detail["check_errors"] = check_errors
            if dry_run:
                return 200, {"ok": True, "status": "dry_run", "slot": slot,
                             "day": day, "report_ok": report.ok, "lines": lines,
                             "check_errors": check_errors}
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
            "check_errors": check_errors,
            "run_detail": {**detail, "outcome": "posted", "posted_ts": ts}}
    if warnings:
        body["warnings"] = warnings
    return 200, body


# ── long tenant writers: 202 + a background run ──────────────────────────

# Daily ``M H * * *`` or weekly ``M H * * D`` — what webhook/cron/trigger.py
# derives (draft-summaries fires on Mondays: ``17 12 * * 1``).
_SLOT_DOW_RE = re.compile(r"^\d{1,2} \d{1,2} \* \* (\*|[0-7])$")

#: Background outcomes that END a slot for the day; a later request for the
#: same slot + day is a duplicate. `failed` is absent on purpose: retryable.
DONE_OUTCOMES = frozenset({"pushed", "no_changes", "skipped"})

Work = Callable[[], tuple[str, dict]]

_inflight: dict[str, dict] = {}                     # job → {slot, day}
_done_memo: dict[tuple[str, str, str], dict] = {}   # (job, slot, day) → detail
_bg_lock = threading.Lock()


def _prior_completion(job: str, slot: str, day: str,
                      now: datetime) -> tuple[dict | None, str | None]:
    """(detail, None) if today's ledger holds a finished run of this job's
    slot; (None, None) if not; (None, why) if the ledger could not be read."""
    memo = _done_memo.get((job, slot, day))
    if memo:
        return memo, None
    try:
        db = mc2_db.get_client(required=False)
        if db is None:
            return None, "ledger unreadable (Supabase env not set)"
        since = (now - timedelta(hours=30)).isoformat()
        rows = (db.table(run_ledger.WEBHOOK_RUNS)
                .select("detail, created_at")
                .eq("route", f"/api/cron/{job}#background")
                .in_("status", ["ok", "partial"])
                .gte("created_at", since)
                .order("created_at", desc=True)
                .limit(50).execute().data) or []
    except Exception as exc:  # noqa: BLE001 — reported, not hidden
        return None, f"ledger unreadable ({type(exc).__name__}: {exc})"
    for r in rows:
        d = r.get("detail") or {}
        if (d.get("job"), d.get("slot"), d.get("day")) == (job, slot, day) \
                and d.get("outcome") in DONE_OUTCOMES:
            return d, None
    return None, None


def _accept_background(
    job: str,
    payload: dict,
    *,
    gate: Callable[[str, datetime], str | None] | None,
    make_work: Callable[..., Work],
) -> tuple:
    """The request half of a long job: slot → gate → in flight? → done today?
    → mark in flight → ``(202, body, work)``. See LONG JOBS."""
    slot = payload.get("slot")
    if not isinstance(slot, str) or not _SLOT_DOW_RE.match(slot.strip()):
        return 400, {"ok": False, "status": "rejected",
                     "error": f"slot must be a cron 'M H * * *' or 'M H * * D', got {slot!r}"}
    slot = slot.strip()
    dry_run = bool(payload.get("dry_run", False))
    now = _now()
    day = now.date().isoformat()
    detail: dict = {"job": job, "slot": slot, "day": day}
    if dry_run:
        detail["dry_run"] = True
    if gate is not None and not dry_run:
        reason = gate(slot, now)
        if reason:
            return 200, {"ok": True, "status": "skipped", "slot": slot, "day": day,
                         "reason": reason, "run_detail": {**detail, "outcome": "skipped"}}
    warnings: list[str] = []
    with _bg_lock:
        running = _inflight.get(job)
        if running:
            return 200, {"ok": True, "status": "running", "slot": slot, "day": day,
                         "running": running,
                         "run_detail": {**detail, "outcome": "running"}}
        if not dry_run:
            prior, why = _prior_completion(job, slot, day, now)
            if prior:
                return 200, {"ok": True, "status": "duplicate", "slot": slot, "day": day,
                             "prior_outcome": prior.get("outcome"),
                             "commit_sha": prior.get("commit_sha"),
                             "run_detail": {**detail, "outcome": "duplicate",
                                            "commit_sha": prior.get("commit_sha")}}
            if why:
                warnings.append(f"idempotency unverified: {why}")
        _inflight[job] = {"slot": slot, "day": day}
    try:
        work = make_work(slot=slot, day=day, dry_run=dry_run)
    except Exception:
        with _bg_lock:
            _inflight.pop(job, None)
        raise
    run_detail = {**detail, "outcome": "accepted"}
    body = {"ok": True, "status": "accepted", "slot": slot, "day": day,
            "run_detail": run_detail}
    if warnings:
        body["warnings"] = run_detail["warnings"] = warnings
    return 202, body, work


async def _run_background(job: str, work: Work, detail: dict) -> None:
    """The background half: run `work` off the loop, write the completion
    row, release the in-flight mark. Never raises."""
    started = time.monotonic()
    try:
        status, extra = await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001 — the row says how it ended
        observability.capture(exc, area=f"cron_{job}_background")
        log.error("cron %s background failed: %s", job, exc, exc_info=True)
        why = f"{type(exc).__name__}: {exc}"
        stderr = getattr(exc, "stderr", None)  # a failed git push / rebase
        if stderr:
            why += f" — {' '.join(str(stderr).split())[-300:]}"
        status, extra = "failed", {"outcome": "failed", "error": why}
    extra = dict(extra)
    error = extra.pop("error", None)
    row_detail = {k: v for k, v in detail.items() if k != "outcome"}
    row_detail.update(extra)
    try:
        if (status in ("ok", "partial") and not detail.get("dry_run")
                and row_detail.get("outcome") in DONE_OUTCOMES):
            _done_memo[(job, detail["slot"], detail["day"])] = row_detail
        await asyncio.to_thread(
            run_ledger.record_run,
            route=f"/api/cron/{job}#background",
            status=status,
            error=error,
            detail=row_detail,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        with _bg_lock:
            _inflight.pop(job, None)
    log.info("cron %s background: %s / %s%s", job, status, row_detail.get("outcome"),
             f" — {error}" if error else "")


def _porcelain(root: Path, *paths: str, all_untracked: bool = False) -> list[str]:
    cmd = ["git", "status", "--porcelain"]
    if all_untracked:
        cmd.append("--untracked-files=all")
    if paths:
        cmd += ["--", *paths]
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


# ── sync (was the tenant's sync.yml) ──────────────────────────────────────


def sync_commit_message(at: datetime) -> str:
    """sync.yml: ``git commit -m "[cp-sync] $(date -u +%Y-%m-%dT%H:%MZ) — auto-sync
    from MC-2"`` — byte for byte (the Sync health line greps the prefix)."""
    return f"[cp-sync] {at.astimezone(UTC):%Y-%m-%dT%H:%MZ} — auto-sync from MC-2"


def _region_edits(root: Path) -> list[str]:
    """sync.yml's "Flag managed-region edits" step: every path under
    exceptions/region-edits/ the sync just wrote (`cxp sync` quarantines a
    hand edit inside a managed region there before replacing it)."""
    from cp_engine.region_guard import QUARANTINE_DIR

    return [ln[3:].strip() for ln in
            _porcelain(root, QUARANTINE_DIR.as_posix(), all_untracked=True)]


def _sync_work(*, slot: str, day: str, dry_run: bool) -> Work:
    def work() -> tuple[str, dict]:
        from cp_engine.sync import SyncError, sync_tenant

        with git_ops._full_history_clone(identity=git_ops.BOT_IDENTITY) as root:
            config = pipeline._load_tenant_config(root)
            try:
                result = sync_tenant(config, dry_run=dry_run)
            except SyncError as exc:
                return "failed", {"outcome": "failed", "error": f"Sync failed: {exc}"}
            extra: dict = {"projects": result.projects_seen,
                           "files_written": len(result.files_written),
                           "sync_warnings": result.warnings}
            if result.warning_messages:
                extra["sync_warning_messages"] = list(result.warning_messages)
            edits = _region_edits(root)
            for f in edits:
                log.warning("cron sync: a managed region was edited by hand; the render "
                            "replaced it and the edit is preserved in %s", f)
            if edits:
                extra["region_edits"] = edits
            if dry_run:
                return "ok", {**extra, "outcome": "dry_run"}
            with git_ops._writer_lock():
                sha = git_ops._commit_with_message_and_push(
                    root, sync_commit_message(datetime.now(UTC)),
                    identity=git_ops.BOT_IDENTITY, guard_regions=False)
        extra["outcome"] = "pushed" if sha else "no_changes"
        if sha:
            extra["commit_sha"] = sha
        if edits:
            # A person's edit was replaced (and preserved): the run worked,
            # and someone needs to look — partial, so the health line says so.
            extra["error"] = (f"{len(edits)} managed-region edit(s) replaced by the "
                              "render; preserved under exceptions/region-edits/")
            return "partial", extra
        return "ok", extra
    return work


def _job_sync(payload: dict) -> tuple:
    """The tenant's daily sync (was sync.yml, 14:00 + 22:00 UTC)."""
    return _accept_background("sync", payload, gate=None, make_work=_sync_work)


# ── draft-summaries (was the tenant's draft-summaries.yml) ────────────────


def draft_commit_message(results, n_changed: int) -> str:
    """draft-summaries.yml's commit, same subject and body: ``[draft-summary] N
    Exec Summaries drafted from recent material (#251)`` where N is the
    workflow's ``git status --porcelain | wc -l``, then one line per written
    summary and the NOT-written list."""
    lines = [f"- {r.code}: from {r.source_note or '—'}"
             for r in results if r.outcome == "written"]
    rejected = [r for r in results if r.outcome in ("rejected", "error")]
    if rejected:
        lines.append("")
        lines.append(f"NOT written ({len(rejected)}) — needs a person:")
        lines += [f"- {r.code}: {r.outcome} — {(r.detail or '')[:200]}" for r in rejected]
    subject = f"[draft-summary] {n_changed} Exec Summaries drafted from recent material (#251)"
    body = "\n".join(lines).strip("\n")
    return f"{subject}\n\n{body}" if body else subject


def _drafts_gate(_slot: str, now: datetime) -> str | None:
    from cp_engine.exec_summary_draft import is_planning_morning

    return None if is_planning_morning(now) else "not Monday morning tenant time"


def _drafts_work(*, slot: str, day: str, dry_run: bool) -> Work:
    def work() -> tuple[str, dict]:
        from cp_engine.exec_summary_draft import all_attempts_errored, run_drafts

        with git_ops._full_history_clone(identity=git_ops.BOT_IDENTITY) as root:
            config = pipeline._load_tenant_config(root)
            now = _now()  # after load: the tenant timezone is set
            if not dry_run and _drafts_gate(slot, now):
                return "ok", {"outcome": "skipped", "reason": _drafts_gate(slot, now)}
            results = run_drafts(config, now=now, apply=not dry_run)
            tally: dict[str, int] = {}
            for r in results:
                tally[r.outcome] = tally.get(r.outcome, 0) + 1
            extra: dict = {"tally": tally, "results": [
                {"code": r.code, "outcome": r.outcome, "detail": (r.detail or "")[:200]}
                for r in results if r.outcome != "skipped"][:60]}
            if all_attempts_errored(results):
                first = next(r for r in results if r.outcome == "error")
                return "failed", {**extra, "outcome": "failed",
                                  "error": f"every draft errored — {first.code}: {first.detail}"}
            if dry_run:
                return "ok", {**extra, "outcome": "dry_run"}
            n_changed = len(_porcelain(root))
            sha = None
            if n_changed:
                with git_ops._writer_lock():
                    sha = git_ops._commit_with_message_and_push(
                        root, draft_commit_message(results, n_changed),
                        identity=git_ops.BOT_IDENTITY)
        extra["outcome"] = "pushed" if sha else "no_changes"
        if sha:
            extra["commit_sha"] = sha
        errored = [r for r in results if r.outcome == "error"]
        if errored:
            extra["error"] = f"{len(errored)} draft(s) errored — {errored[0].code}: {errored[0].detail}"
            return "partial", extra
        return "ok", extra
    return work


def _job_draft_summaries(payload: dict) -> tuple:
    """Monday Exec Summary drafts (was draft-summaries.yml, 12:17 + 13:17 UTC
    Mondays), with `--planning-morning-only`'s gate: Monday before 10:00
    tenant time. dry_run drafts and checks, writes and commits nothing."""
    return _accept_background("draft-summaries", payload, gate=_drafts_gate,
                              make_work=_drafts_work)


JOBS: dict[str, Callable[[dict], tuple]] = {
    "health": _job_health,
    "sync": _job_sync,
    "draft-summaries": _job_draft_summaries,
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
        result = await asyncio.to_thread(fn, payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — a clean 500 with the reason
        observability.capture(exc, area=f"cron_{job}")
        log.error("cron %s failed: %s", job, exc)
        return JSONResponse(status_code=500, content={
            "ok": False, "status": "failed", "error": f"cron {job} failed: "
            f"{type(exc).__name__}: {exc}"})
    status, body = result[0], result[1]
    if len(result) > 2 and result[2] is not None:
        # A long job: answered 202, the work runs after the response.
        pipeline._spawn_background(_run_background(job, result[2], body["run_detail"]))
    log.info("cron %s: %s", job, body.get("status"))
    return JSONResponse(status_code=status, content=body)
