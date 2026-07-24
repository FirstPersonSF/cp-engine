"""Inbound-email route: POST /api/inbound-email (Phase 1 — park-only pipe).

The cp-email-worker (a Cloudflare Email Worker) receives mail at
``cp+<code>@thinkermakers.com``, MIME-parses it, normalizes to JSON,
HMAC-signs, and POSTs here. Phase 1 does the minimum that proves the pipe
end-to-end: verify → resolve the plus-address to a project code → PARK the
raw message into that project's working dir → commit + push → 200. No
distill yet (that's Phase 2); no thread-keyed DB source parking yet (that's
a later MC-2 migration).

Design: cp/docs/plans/2026-07-22-email-ingest-design.md.

The signature scheme mirrors signatures._verify_signature (timestamped
HMAC-SHA256, ``<timestamp>.<body>``) but uses its OWN secret,
INBOUND_EMAIL_SECRET, so email and Fathom rotate independently — the same
precedent as CLICKUP_WEBHOOK_SECRET / SLACK_SIGNING_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime

import git_ops
from fastapi import APIRouter, HTTPException, Request

from cp_engine.config import load as load_config
from cp_engine.spine import SpineDirNotFound, find_spine_dir

import email_strip

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()

# Replay window, matching the Fathom/Slack budget.
_TIMESTAMP_REPLAY_WINDOW_SEC = 300

# Local part of the receiving address, before the "+": cp+<code>@…
# We accept "cp" as the base; the shape + code ride in the "+..." suffix.
_PLUS_ADDR = re.compile(
    r"^cp\+(?P<suffix>[^@]+)@",
    re.IGNORECASE,
)


@dataclass
class Routing:
    """Parsed plus-address → assignment shape + target."""

    shape: str  # "project" | "account" | "planning"
    code: str  # the resolved <code>, <company>, or <scope>


def _verify_inbound_signature(
    raw_body: bytes, provided: str, timestamp: str
) -> None:
    """Timestamped HMAC-SHA256 under INBOUND_EMAIL_SECRET.

    Base is ``<timestamp>.<body>`` — byte-identical to what the Cloudflare
    Worker signs. Enforces the 5-minute freshness window.
    """
    secret = os.environ.get("INBOUND_EMAIL_SECRET")
    if not secret:
        raise HTTPException(
            status_code=500, detail="INBOUND_EMAIL_SECRET not configured"
        )
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Webhook-Signature header")
    if not timestamp:
        raise HTTPException(status_code=401, detail="missing X-Webhook-Timestamp header")
    try:
        ts_int = int(timestamp)
    except ValueError:
        raise HTTPException(
            status_code=401, detail="X-Webhook-Timestamp not an integer"
        ) from None
    skew = time.time() - ts_int
    if abs(skew) > _TIMESTAMP_REPLAY_WINDOW_SEC:
        log.warning(
            "inbound-email verify rejected: timestamp outside %ds window (skew=%.1fs)",
            _TIMESTAMP_REPLAY_WINDOW_SEC, skew,
        )
        raise HTTPException(
            status_code=401, detail="X-Webhook-Timestamp outside freshness window"
        )
    base = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")


def parse_plus_address(to_addr: str) -> Routing | None:
    """Resolve ``cp+<suffix>@…`` into a Routing, or None if unrecognized.

    Shapes (the same "+ Assign" vocabulary as Fathom):
        cp+ibx-5192@…        → project(ibx-5192)
        cp+account+infoblox@ → account(infoblox)      [distill in Phase 3]
        cp+planning+1p@…     → planning(1p)            [distill in Phase 3]

    A suffix that doesn't match a known shape is treated as a project code
    (the common case). Callers still validate the code resolves to a real
    project before acting.
    """
    if not to_addr:
        return None
    m = _PLUS_ADDR.search(to_addr)
    if not m:
        return None
    suffix = m.group("suffix").strip().lower()
    if not suffix:
        return None
    parts = suffix.split("+")
    head = parts[0]
    if head == "account" and len(parts) >= 2:
        return Routing(shape="account", code=parts[1])
    if head == "planning" and len(parts) >= 2:
        return Routing(shape="planning", code=parts[1])
    # Default: the whole suffix is a project/initiative code.
    return Routing(shape="project", code=suffix)


def _park_filename(message_id: str, received_at: str) -> str:
    """A stable, message-id-keyed filename for the parked raw email.

    Re-delivery of the same Message-ID overwrites (idempotent), matching
    the transcript-parking convention.
    """
    # Message-IDs look like "<abc.def@host>"; strip the angle brackets and
    # sanitize to a filesystem-safe slug.
    mid = (message_id or "").strip().strip("<>")
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", mid) if mid else ""
    date_part = (received_at or datetime.utcnow().isoformat())[:10]
    if slug:
        return f"{date_part} email {slug}.md"
    # No Message-ID (rare): fall back to a timestamp so we never collide-clobber.
    return f"{date_part} email {datetime.utcnow().strftime('%H%M%S')}.md"


def _render_parked_email(payload: dict, strip: email_strip.StripResult) -> str:
    """The parked artifact: envelope + net-new delta + full raw body.

    Phase-1 keeps the FULL text below the delta so nothing is lost before
    the DB-source-parking migration exists; the delta is surfaced at the top
    as what the distill will eventually consume.
    """
    lines = [
        "---",
        f"From: {payload.get('from', '')}",
        f"To: {payload.get('to', '')}",
        f"Subject: {payload.get('subject', '')}",
        f"Message-ID: {payload.get('message_id', '')}",
        f"In-Reply-To: {payload.get('in_reply_to') or ''}",
        f"Received: {payload.get('received_at', '')}",
        f"Strip: {'stripped' if strip.stripped else 'fail-open'}"
        + (f" (cut@{strip.cut_at})" if strip.cut_at is not None else ""),
        "---",
        "",
        "## Net-new (delta)",
        "",
        strip.delta or "(empty)",
        "",
        "## Full body (verbatim)",
        "",
        payload.get("text", "") or "(no text body)",
        "",
    ]
    return "\n".join(lines)


@router.post("/api/inbound-email")
async def inbound_email(request: Request) -> dict:
    """Park an inbound email into its project's working dir. Phase 1.

    Request body (JSON, from the Cloudflare Worker):
        { to, from, subject, message_id, in_reply_to, references,
          text, html, header_to, received_at }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256("<ts>." + body, INBOUND_EMAIL_SECRET))
        X-Webhook-Timestamp: unix seconds

    Response:
        { status, code, shape, parked_path, stripped, commit_sha }
        or { status: "unresolved"|"park_only_unknown_code", ... } (still 200).
    """
    raw_body = await request.body()
    _verify_inbound_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    to_addr = payload.get("to") or payload.get("header_to") or ""
    routing = parse_plus_address(to_addr)
    if routing is None:
        # Not a cp+<code> address — acknowledge without acting so the Worker
        # doesn't reject/retry. Nothing to park.
        log.warning("inbound-email: unresolved address %r — no-op", to_addr)
        return {"status": "unresolved", "to": to_addr}

    # Phase 1 only parks the project shape. Account/planning fan-out reuses
    # the existing meeting fan-out in Phase 3; until then, acknowledge them
    # without parking (they'd need a company/scope→projects expansion).
    if routing.shape != "project":
        log.info(
            "inbound-email: shape=%s code=%s not parked in Phase 1 (acknowledged)",
            routing.shape, routing.code,
        )
        return {"status": "acknowledged_not_parked", "shape": routing.shape, "code": routing.code}

    # Strip quoted history to the net-new delta (server-side, one place).
    strip = email_strip.strip_quoted_history(payload.get("text", "") or "")

    # Park into the project's working dir on a fresh tenant clone, commit, push.
    with git_ops._cloned_tenant() as tenant_root:
        config = load_config(tenant_root)
        try:
            project_dir = find_spine_dir(config.root, routing.code)
        except SpineDirNotFound:
            # Address parsed but the code isn't a live project. Acknowledge
            # (200) so the Worker doesn't retry; log for the review surface.
            log.warning(
                "inbound-email: code %r did not resolve to a project dir — not parked",
                routing.code,
            )
            return {
                "status": "park_only_unknown_code",
                "code": routing.code,
                "shape": routing.shape,
            }

        out_dir = project_dir / "email-ingest"
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = _park_filename(
            payload.get("message_id", ""), payload.get("received_at", "")
        )
        parked = out_dir / filename
        parked.write_text(_render_parked_email(payload, strip), encoding="utf-8")

        rel = parked.relative_to(tenant_root)
        mid_prefix = (payload.get("message_id", "") or "").strip().strip("<>")[:16]
        commit_sha = git_ops._commit_with_message_and_push(
            tenant_root,
            f"[auto-ingest] email:{routing.code}: {mid_prefix or 'message'}",
        )

    log.info(
        "inbound-email parked: code=%s file=%s stripped=%s commit=%s",
        routing.code, rel, strip.stripped, commit_sha,
    )
    return {
        "status": "parked",
        "code": routing.code,
        "shape": routing.shape,
        "parked_path": str(rel),
        "stripped": strip.stripped,
        "commit_sha": commit_sha,
    }
