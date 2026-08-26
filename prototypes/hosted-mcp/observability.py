"""Sentry + correlation-id plumbing for the hosted-cp MCP server.

Ported from `cp-engine/webhook/observability.py` (copied, not imported — the
webhook is a different deployable with its own image, exactly as
`tree_ssh_env` copies `git_ops._ssh_env`). Three deliberate differences from
the original are marked DIFF below.

WHY THIS EXISTS HERE. The 2026-08-26 audit found the tenant tree had been
frozen at a nine-day-old commit while the DB verbs returned current rows. The
code was not silent about it: `tenant tree pull failed` was logged on every
single read, correctly, for nine days — into a void. Response-level provenance
(shipped in the same audit) makes a stall visible to a *caller who looks*; it
does nothing for an operator who is not looking. Every swallow-and-continue
block on this server has the same property, and there are dozens.

`capture()` is the fail-soft mirror for those blocks: they call it so the
failure reaches an alert WITHOUT changing their control flow. Degrading
silently is the bug; degrading loudly-to-an-operator is the fix.

Two concerns, one module:

1. **Sentry** — `init_sentry()` is a strict no-op without ``SENTRY_DSN``, so
   `sentry-sdk` never has to be importable outside the Railway image and local
   runs stay dependency-free.
2. **Correlation IDs** — one id per inbound MCP message, threaded via a
   contextvar so no tool signature changes. Every log line carries it via
   `CorrelationIdFilter`, and `capture()` tags Sentry events with it, so one
   grep reconstructs one tool call end-to-end.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_sentry_enabled = False

_LOGGER_NAME = "hosted-mcp"


def new_correlation_id(incoming: str | None = None) -> str:
    """Set (and return) the contextvar for this message.

    ``incoming`` honors an upstream id when one is available; falls back to a
    fresh 12-hex id (uuid4-derived — short enough to read in a log line, unique
    enough to grep). Trimmed and length-capped so a hostile value cannot bloat
    logs.
    """
    cid = (incoming or "").strip()[:64] or uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


def current_correlation_id() -> str | None:
    return correlation_id.get()


class CorrelationIdFilter(logging.Filter):
    """Stamp every record with ``.cid`` so the log format can include it.

    Attached to the ROOT handler: records from other loggers (uvicorn, the MCP
    SDK) pass through too, so the filter must always set the attribute — ``-``
    outside a message context.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.cid = correlation_id.get() or "-"
        return True


def init_sentry(release: str) -> bool:
    """Initialize Sentry iff SENTRY_DSN is set. Never raises.

    Environment tag: SENTRY_ENVIRONMENT > RAILWAY_ENVIRONMENT_NAME >
    RAILWAY_ENVIRONMENT > "unknown". Errors-only (no tracing) — the goal is
    alerting, not APM.
    """
    global _sentry_enabled
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            # DIFF 1: released as the hosted server, versioned by
            # SERVER_VERSION — this deployable has no cp_engine import.
            release=f"hosted-cp@{release}",
            environment=(
                os.environ.get("SENTRY_ENVIRONMENT")
                or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
                or os.environ.get("RAILWAY_ENVIRONMENT")
                or "unknown"
            ),
            traces_sample_rate=0.0,
            # DIFF 2: no FastAPI integration. This is an MCP server; unhandled
            # tool exceptions are caught by the SDK, not by a route handler, so
            # `capture()` from the swallow blocks is the primary path rather
            # than a supplement to automatic route capture.
            #
            # send_default_pii stays OFF (the default). Every tool here runs
            # under a caller's Supabase JWT and touches client-confidential
            # project material; an alerting channel is not the place for it.
            # `capture()` tags identity as a subject UUID only — see below.
        )
    except Exception:  # noqa: BLE001 — observability must never block startup
        logging.getLogger(_LOGGER_NAME).warning(
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

    Call this from swallow-and-continue blocks: it routes the failure to an
    alert without touching control flow. Tags carry the correlation id so an
    alert links back to the call's log lines.

    Pass only NON-SENSITIVE tags — an area name, a project code, a caller's
    subject UUID. Never a file body, a JWT, or an email address.
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


# DIFF 3: the webhook tags Sentry's per-request scope from FastAPI HTTP
# middleware. The MCP SDK has its own middleware chain instead —
# `async (ctx, call_next)` wrapping every inbound message — which is a better
# seam: one id per TOOL CALL rather than per HTTP request, and a streamable-http
# session can carry several calls.
async def correlation_middleware(ctx, call_next):
    """Assign one correlation id per inbound MCP message.

    Registered on `MCPServer(middleware=[...])`. Wraps every message, so a
    failure in any tool carries an id that ties its log lines together.

    Never raises on its own account: if anything here failed it would take down
    every tool call, which is precisely the trade observability must not make.
    """
    try:
        new_correlation_id()
        tag_scope()
    except Exception:  # noqa: BLE001 — never break a call to label it
        pass
    return await call_next(ctx)


def tag_scope() -> None:
    """Tag Sentry's current scope with the correlation id.

    So exceptions the SDK captures on its own — not routed through
    `capture()` — carry the id too.
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
