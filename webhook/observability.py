"""Sentry + correlation-id plumbing for the cp-engine webhook (arch-phase-3).

Two concerns, one module:

1. **Sentry** — error alerting for the "silent DB row" class. `init_sentry()`
   is a strict no-op without a ``SENTRY_DSN`` env var (local dev, tests), so
   ``sentry-sdk`` never has to be importable outside the Railway image.
   `capture()` is the fail-soft mirror: swallow-and-continue except blocks
   call it so the failure reaches an alert WITHOUT changing their control
   flow. It never raises.

2. **Correlation IDs** — one id per webhook delivery, threaded via a
   contextvar (no signature changes through the clone→plan→commit→push
   pipeline). The FastAPI middleware in main.py sets it at receipt (honoring
   an incoming ``X-Correlation-ID`` header so fathom-meeting-sync can
   originate the id), every log line carries it via `CorrelationIdFilter`,
   commit messages and the auto_ingest_runs row embed it, and `capture()`
   tags Sentry events with it — one grep reconstructs one delivery
   end-to-end.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_sentry_enabled = False


def new_correlation_id(incoming: str | None = None) -> str:
    """Set (and return) the contextvar for this delivery.

    ``incoming`` honors an upstream ``X-Correlation-ID`` header — trimmed
    and length-capped so a hostile header can't bloat logs/DB rows. Falls
    back to a fresh 12-hex id (uuid4-derived; short enough for commit
    messages, unique enough for log grepping).
    """
    cid = (incoming or "").strip()[:64] or uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


def current_correlation_id() -> str | None:
    return correlation_id.get()


class CorrelationIdFilter(logging.Filter):
    """Stamp every record with ``.cid`` so the log format can include it.

    Attached to the ROOT handler: records from other loggers (uvicorn)
    pass through too, so the filter must always set the attribute —
    ``-`` outside a request context.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.cid = correlation_id.get() or "-"
        return True


def init_sentry(release: str) -> bool:
    """Initialize Sentry iff SENTRY_DSN is set. Never raises.

    Environment tag: SENTRY_ENVIRONMENT > RAILWAY_ENVIRONMENT_NAME >
    RAILWAY_ENVIRONMENT > "unknown". Release = the cp-engine version the
    image shipped with. Errors-only (no tracing) — the goal is alerting,
    not APM.
    """
    global _sentry_enabled
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            release=f"cp-engine-webhook@{release}",
            environment=(
                os.environ.get("SENTRY_ENVIRONMENT")
                or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
                or os.environ.get("RAILWAY_ENVIRONMENT")
                or "unknown"
            ),
            traces_sample_rate=0.0,
        )
    except Exception:  # noqa: BLE001 — observability must never block startup
        logging.getLogger("cp-engine-webhook").warning(
            "Sentry init failed; continuing without error alerting",
            exc_info=True,
        )
        return False
    _sentry_enabled = True
    return True


def sentry_enabled() -> bool:
    return _sentry_enabled


def capture(exc: BaseException, **tags: str) -> None:
    """Best-effort capture_exception. No-op without init; never raises.

    Tags the event with the current correlation id (plus any caller
    tags) so a Sentry alert links straight back to the delivery's log
    lines, commit, and auto_ingest_runs row.
    """
    if not _sentry_enabled:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            cid = correlation_id.get()
            if cid:
                scope.set_tag("correlation_id", cid)
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 — a broken capture must stay invisible
        pass


def tag_request_scope() -> None:
    """Tag Sentry's per-request scope with the correlation id.

    Called from the middleware AFTER new_correlation_id, so unhandled
    route exceptions (captured by the FastAPI integration, not by
    `capture()`) carry the id too.
    """
    if not _sentry_enabled:
        return
    try:
        import sentry_sdk

        cid = correlation_id.get()
        if cid:
            sentry_sdk.set_tag("correlation_id", cid)
    except Exception:  # noqa: BLE001
        pass
