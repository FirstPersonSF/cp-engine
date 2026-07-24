"""Inbound-email route: POST /api/inbound-email (Phase 2 — park + distill).

The cp-email-worker (a Cloudflare Email Worker) receives mail at
``cp+<code>@thinkermakers.com``, MIME-parses it, normalizes to JSON,
HMAC-signs, and POSTs here. The route: verify → resolve the plus-address to
a project code → strip quoted history to the net-new delta → PARK the raw
message into that project's working dir → run the delta through the EXISTING
distill (``_ingest_one_project``, the same path Fathom auto-ingest uses) so
it proposes commitments / decisions / stakeholder signals / agenda items
into the review gate → commit + push → 200.

Everything the distill writes lands PROPOSED (the review gate), identical to
Fathom auto-ingest — nothing is auto-confirmed. Email is an ingest SOURCE
feeding the existing pipeline, not a new spine object.

Still to come: thread-keyed DB source parking (a later MC-2 migration so
replies on one thread append to one source); account/planning fan-out
(Phase 3); Option A label-driven auto-forward (Phase 2.5).

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
from datetime import datetime, timezone

import git_ops
import pipeline
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
    date_part = (received_at or datetime.now(timezone.utc).isoformat())[:10]
    if slug:
        return f"{date_part} email {slug}.md"
    # No Message-ID (rare): fall back to a timestamp so we never collide-clobber.
    return f"{date_part} email {datetime.now(timezone.utc).strftime('%H%M%S')}.md"


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


def _render_distill_input(payload: dict, delta: str) -> str:
    """The text handed to the distiller: a light envelope over the delta.

    The distiller keys stakeholder + feedback signals off WHO said something
    and about WHAT, so we prepend From/Subject/Date rather than feed the bare
    delta. Shaped like the header the transcript path stages
    (``# <title>\\n# Date: …``) so the same prompt reads it naturally.
    """
    frm = (payload.get("from") or "").strip()
    subject = (payload.get("subject") or "").strip()
    date_part = (payload.get("received_at") or datetime.now(timezone.utc).isoformat())[:10]
    header = (
        f"# Email: {subject or '(no subject)'}\n"
        f"# From: {frm or '(unknown sender)'}\n"
        f"# Date: {date_part}\n\n"
    )
    return header + (delta or "")


def _stage_email_delta(tenant_root, code: str, message_id: str, text: str):
    """Stage the distill input into transcripts/incoming/ and return its Path.

    Mirrors pipeline._stage_transcript's location + audit role, but keyed on
    the email Message-ID instead of a meeting id, and never collides with a
    Fathom stage file (distinct ``email-`` prefix).
    """
    incoming = tenant_root / "transcripts" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    mid = (message_id or "").strip().strip("<>")
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", mid)[:24] if mid else "nomid"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = incoming / f"email-{stamp}-{slug}.txt"
    path.write_text(text, encoding="utf-8")
    return path


@router.post("/api/inbound-email")
async def inbound_email(request: Request) -> dict:
    """Park + distill an inbound email into its project's working dir.

    Verify → resolve the plus-address → strip to the net-new delta → park the
    raw email → distill the delta through the existing pipeline (PROPOSED,
    review-gated) → one commit + push.

    Request body (JSON, from the Cloudflare Worker):
        { to, from, subject, message_id, in_reply_to, references,
          text, html, header_to, received_at }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256("<ts>." + body, INBOUND_EMAIL_SECRET))
        X-Webhook-Timestamp: unix seconds

    Response:
        { status, code, shape, parked_path, stripped, distilled,
          plan_summary, files_written, distill_errors, commit_sha }
        or { status: "unresolved"|"acknowledged_not_parked"|
             "park_only_unknown_code", ... } (still 200).
        status is "ingested" when the delta distilled clean, else "parked".
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

    # Park + distill on a fresh tenant clone, then ONE commit + push.
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

        # 1) Park the raw email — the durable audit + the distill input.
        out_dir = project_dir / "email-ingest"
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = _park_filename(
            payload.get("message_id", ""), payload.get("received_at", "")
        )
        parked = out_dir / filename
        parked.write_text(_render_parked_email(payload, strip), encoding="utf-8")
        rel = parked.relative_to(tenant_root)

        # 2) Distill the net-new delta through the EXISTING pipeline — the same
        #    _ingest_one_project the Fathom path runs. Everything it writes
        #    lands PROPOSED (the review gate). Skipped when the delta is empty:
        #    a pure-scheduling forward strips to nothing, and the "produce
        #    nothing" discipline says don't invent a plan from an empty body.
        delta = (strip.delta or "").strip()
        distill: dict | None = None
        if delta:
            distill_text = _render_distill_input(payload, delta)
            transcript_path = _stage_email_delta(
                tenant_root, routing.code, payload.get("message_id", ""), distill_text
            )
            try:
                distill = pipeline._ingest_one_project(
                    config=config,
                    code=routing.code,
                    transcript_path=transcript_path,
                    # No Fathom meeting behind an email: no action_items,
                    # no meeting_id, no roster. _ingest_one_project guards
                    # each — the LLM-only plan path runs cleanly.
                )
            except Exception:  # noqa: BLE001 — distill must never lose the parked mail
                log.warning(
                    "inbound-email: distill failed for %s (email parked, committed anyway)",
                    routing.code, exc_info=True,
                )
                distill = {"errors": ["distill raised — see logs"], "files_written": []}
        else:
            log.info(
                "inbound-email: empty delta for %s — parked, no distill (scheduling-only?)",
                routing.code,
            )

        # 3) ONE commit sweeps the parked email + any distilled bullets
        #    (`git add -A` inside git_ops). The message attributes distill so
        #    the log distinguishes "distilled" from "parked only".
        mid_prefix = (payload.get("message_id", "") or "").strip().strip("<>")[:16]
        wrote = bool(distill and distill.get("files_written"))
        verb = "distilled" if wrote else "parked"
        commit_sha = git_ops._commit_with_message_and_push(
            tenant_root,
            f"[auto-ingest] email:{routing.code}: {verb} {mid_prefix or 'message'}",
        )

    plan_summary = (distill or {}).get("plan_summary")
    distill_errors = (distill or {}).get("errors") or []
    log.info(
        "inbound-email done: code=%s file=%s stripped=%s distilled=%s plan=%s commit=%s",
        routing.code, rel, strip.stripped, bool(plan_summary), plan_summary, commit_sha,
    )
    return {
        "status": "ingested" if (distill and not distill_errors) else "parked",
        "code": routing.code,
        "shape": routing.shape,
        "parked_path": str(rel),
        "stripped": strip.stripped,
        "distilled": bool(distill),
        "plan_summary": plan_summary,
        "files_written": (distill or {}).get("files_written", []),
        "distill_errors": distill_errors,
        "commit_sha": commit_sha,
    }
