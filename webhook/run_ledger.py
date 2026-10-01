"""One run record per webhook delivery, on EVERY path (architecture plan step 3).

WHY. Before this, four routes wrote a run row (auto-ingest's three into
``auto_ingest_runs``, spine promote into ``spine_promote_runs``, asset ingest
into ``asset_ingest_runs``) and the other thirteen wrote nothing: a
``/api/sessions/capture`` whose push failed, a ``/slack-action`` click whose
background plan raised, a ``/clickup-task-closed`` whose lookup hit a DB error,
an ``/api/inbound-email`` whose distill blew up — each left a log line on
Railway and nothing anyone reads. Even the routes WITH a domain table wrote
nothing when the request died before the pipeline started (bad signature,
bad JSON, a duplicate-delivery short-circuit). #262 is the shape: ``ok=true``
with no run row.

HOW. `RunLedgerMiddleware` wraps every POST and writes one ``webhook_runs`` row
after the response is decided — or after the handler raised — so no route can
forget. The status comes from the response, not from the route's goodwill
(`derive_status`). Background tails (202 routes) add a second row when they
finish via `record_run(..., route=f"{path}#background")`.

A ledger write that fails must not fail the delivery, but it must not vanish
either: it logs at ERROR and goes to Sentry (`observability.capture`). That is
the one place a swallow is allowed, and it is loud.

The table is created by ``webhook/migrations/04_webhook_runs.sql`` (owner:
cp-engine-webhook). Until it is applied, every ledger write logs an ERROR and
`cxp health` reports the table as missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import observability
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from cp_engine import mc2_db

log = logging.getLogger("cp-engine-webhook")

from cp_engine.daily_health import WEBHOOK_RUNS  # noqa: E402 — one spelling

STATUSES = ("ok", "partial", "failed", "rejected", "accepted")

# Keys in a 2xx JSON body that, when non-empty, make a run PARTIAL: the route
# did its main job but told the caller something went wrong on the side.
_PARTIAL_KEYS = ("errors", "warnings", "distill_errors")

_MAX_ERROR = 2000


def _truncate(text: str | None, n: int = _MAX_ERROR) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _first_error(body: dict) -> str | None:
    for key in ("error", "reason", "detail"):
        val = body.get(key)
        if val:
            return str(val)
    for key in _PARTIAL_KEYS:
        vals = body.get(key)
        if isinstance(vals, list) and vals:
            return str(vals[0])
    for entry in _entries(body):
        if entry.get("errors"):
            return f"{entry.get('code')}: {entry['errors'][0]}"
    return None


def _entries(body: dict) -> list[dict]:
    """Per-project entries of an auto-ingest-shaped body (`ingested` may be a
    bool on other routes — e.g. /clickup-task-closed)."""
    val = body.get("ingested")
    if not isinstance(val, list):
        return []
    return [e for e in val if isinstance(e, dict)]


def derive_status(http_status: int, body: Any) -> tuple[str, str | None]:
    """(status, error_text) for one finished request.

    - 5xx → failed; 4xx → rejected (signature, bad JSON, unknown code — the
      sender's problem, still recorded so "every call 401s since the secret
      rotated" is visible); 202 → accepted (a background tail records its own
      outcome).
    - 2xx JSON: ``ok: false`` or ``status: failed`` → failed; any non-empty
      ``errors``/``warnings``/``distill_errors``, a per-project ``ingested``
      entry with errors, or ``status: partial`` → partial; else ok.
    """
    b = body if isinstance(body, dict) else {}
    if http_status >= 500:
        return "failed", _first_error(b)
    if http_status >= 400:
        return "rejected", _first_error(b)
    if http_status == 202:
        return "accepted", None
    if b.get("ok") is False or b.get("status") == "failed":
        return "failed", _first_error(b)
    partial = b.get("status") == "partial" or any(
        isinstance(b.get(k), list) and b.get(k) for k in _PARTIAL_KEYS
    )
    if not partial:
        partial = any(e.get("errors") for e in _entries(b))
    if partial:
        return "partial", _first_error(b)
    return "ok", None


def _project_code(body: Any, request_body: Any) -> str | None:
    for src in (body, request_body):
        if isinstance(src, dict):
            for key in ("code", "project_code"):
                if isinstance(src.get(key), str) and src.get(key):
                    return src[key]
            codes = src.get("project_codes")
            if isinstance(codes, list) and codes:
                return ",".join(str(c) for c in codes)[:200]
    return None


def record_run(
    *,
    route: str,
    status: str,
    http_status: int | None = None,
    project_code: str | None = None,
    error: str | None = None,
    detail: dict | None = None,
    duration_ms: int | None = None,
    client=None,
) -> bool:
    """Insert one ``webhook_runs`` row. Returns True iff it landed.

    Never raises: a delivery must not fail because its ledger write did. But a
    miss is logged at ERROR and captured to Sentry, never dropped quietly.
    """
    if status not in STATUSES:
        status = "failed"
    row = {
        "route": route,
        "status": status,
        "http_status": http_status,
        "project_code": project_code,
        "error": _truncate(error),
        "detail": detail or {},
        "duration_ms": duration_ms,
        "correlation_id": observability.current_correlation_id(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        db = client if client is not None else mc2_db.get_client(required=False)
        if db is None:
            log.error(
                "webhook_runs: NOT recorded (Supabase env not set): %s %s %s",
                route, status, _truncate(error, 200),
            )
            return False
        db.table(WEBHOOK_RUNS).insert(row).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — loud, but never fails the delivery
        log.error(
            "webhook_runs: insert FAILED for %s %s (%s): %s",
            route, status, _truncate(error, 200), exc,
        )
        observability.capture(exc, area="webhook_runs_insert", route=route)
        return False


def _json_or_none(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


class RunLedgerMiddleware(BaseHTTPMiddleware):
    """Record one ``webhook_runs`` row for every POST, whatever happened."""

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)
        started = time.monotonic()
        route = request.url.path
        try:
            response = await call_next(request)
        except Exception as exc:
            # Unhandled — the server will 500. Record it, then let it propagate
            # (Sentry's FastAPI integration and the 500 still happen).
            await asyncio.to_thread(
                record_run,
                route=route,
                status="failed",
                http_status=500,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise

        raw = b""
        async for chunk in response.body_iterator:
            raw += chunk if isinstance(chunk, bytes) else chunk.encode()
        try:
            body = _json_or_none(raw)
            status, error = derive_status(response.status_code, body)
            code = _project_code(body, None)
            # A route may hand the ledger a structured `run_detail` (the cron
            # route stores its slot, day and posted ts — its idempotency key).
            detail = body.get("run_detail") if isinstance(body, dict) else None
            if not isinstance(detail, dict):
                detail = None
        except Exception as exc:  # noqa: BLE001 — the ledger never breaks a response
            log.error("webhook_runs: could not classify %s response: %s", route, exc)
            observability.capture(exc, area="webhook_runs_classify", route=route)
            status = "failed" if response.status_code >= 500 else "partial"
            error, code, detail = f"ledger could not classify response: {exc}", None, None
        await asyncio.to_thread(
            record_run,
            route=route,
            status=status,
            http_status=response.status_code,
            project_code=code,
            error=error,
            detail=detail,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return Response(
            content=raw,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
