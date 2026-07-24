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
import signatures
from fastapi import APIRouter, HTTPException, Request

from cp_engine import mc2_db
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

# The bare cp mailbox, no "+suffix": cp@… . A message here was meant for cp
# (someone forgot the +code) — worth recording as unrouted. Anything else that
# reached the catch-all is a stray and stays a true no-op.
_BARE_CP = re.compile(r"^cp@", re.IGNORECASE)


def _is_cp_mailbox(to_addr: str) -> bool:
    """True if the address is the bare ``cp@…`` mailbox (no +code)."""
    return bool(_BARE_CP.search((to_addr or "").strip()))


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


def _record_unrouted(payload: dict, *, reason: str, attempted_code: str | None) -> bool:
    """Persist an unroutable email to MC-2's ``unrouted_emails`` holding pen.

    The two dead ends — a bare ``cp@`` (reason='no_code') and a ``cp+<code>@``
    whose code matches no live project (reason='unknown_code') — used to be a
    silent no-op. Now they land here so the Inputs routing queue can surface
    them for a human to route. Idempotent on the Message-ID (upsert), so a
    Worker retry doesn't duplicate.

    Best-effort by contract: a None client (env missing / pkg absent) or any
    write failure logs and returns False — the endpoint still 200s so the
    Worker doesn't retry-storm. We never had the mail in MC-2 before, so a
    miss is no worse than the old behavior, just louder in the logs.
    """
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning(
            "inbound-email: unrouted (%s) but MC-2 client unavailable — not recorded",
            reason,
        )
        return False

    strip = email_strip.strip_quoted_history(payload.get("text", "") or "")
    mid = (payload.get("message_id", "") or "").strip().strip("<>")
    # No Message-ID (rare): synthesize a stable-ish key so the row still lands.
    row_id = mid or f"nomid-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    row = {
        "id": row_id,
        "thread_id": (payload.get("in_reply_to") or "").strip().strip("<>") or None,
        "from_addr": payload.get("from") or None,
        "to_addr": payload.get("to") or payload.get("header_to") or None,
        "subject": payload.get("subject") or None,
        "delta": strip.delta or None,
        "raw_text": payload.get("text") or None,
        "received_at": payload.get("received_at") or None,
        "reason": reason,
        "attempted_code": attempted_code,
        "status": "unrouted",
    }
    try:
        client.table("unrouted_emails").upsert(row, on_conflict="id").execute()
        log.info(
            "inbound-email: recorded unrouted email id=%s reason=%s attempted=%s",
            row_id, reason, attempted_code,
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, never fail the request
        log.warning(
            "inbound-email: failed to record unrouted email (%s)", reason, exc_info=True
        )
        return False


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
        # No cp+<code>. A bare `cp@` (someone forgot the +code) is worth
        # capturing so a human can route it; a genuinely foreign local-part
        # (a catch-all stray, not meant for cp at all) stays a true no-op so
        # we don't fill the routing queue with spam.
        if _is_cp_mailbox(to_addr):
            recorded = _record_unrouted(payload, reason="no_code", attempted_code=None)
            log.warning(
                "inbound-email: bare cp@ with no project code (%r) — unrouted (recorded=%s)",
                to_addr, recorded,
            )
            return {"status": "unrouted", "reason": "no_code", "to": to_addr, "recorded": recorded}
        log.warning("inbound-email: unrelated address %r — no-op", to_addr)
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
            # A +code that matches no live project (typo, archived, wrong
            # tenant). Record it in the unrouted holding pen — same routing
            # queue as a bare cp@, but with the attempted code so the human
            # sees what was tried. Still 200 so the Worker doesn't retry.
            recorded = _record_unrouted(
                payload, reason="unknown_code", attempted_code=routing.code
            )
            log.warning(
                "inbound-email: code %r did not resolve to a project — unrouted (recorded=%s)",
                routing.code, recorded,
            )
            return {
                "status": "unrouted",
                "reason": "unknown_code",
                "code": routing.code,
                "shape": routing.shape,
                "recorded": recorded,
            }

        result = _park_and_distill(
            tenant_root=tenant_root,
            config=config,
            project_dir=project_dir,
            code=routing.code,
            payload=payload,
            strip=strip,
        )

    result["shape"] = routing.shape
    return result


@router.post("/api/route-email")
async def route_email(request: Request) -> dict:
    """Route a previously-unrouted email to a chosen project. Human-triggered.

    MC-2's Inputs routing queue calls this when a partner picks the target
    project for an ``unrouted_emails`` row. We reconstruct the email payload
    from that row, run the SAME park+distill core, and flip the row to
    ``routed``. Signed with INBOUND_EMAIL_SECRET, same as ``/api/inbound-email``
    (MC-2 holds the secret to sign server-side).

    Request body (JSON):
        { message_id, code, project_id? }
          message_id — the unrouted_emails.id
          code       — the chosen project's code (for logging + the routed_to
                       stamp; a human-readable label)
          project_id — the chosen project's MC-2 UUID (optional but PREFERRED).
                       find_spine_dir resolves UUID-first off the cp.md MC-id
                       stamp, so this resolves even when the MC-2 `code` column
                       differs from the dir's slug-of-full_job_name (they
                       routinely do: `SLT-brand-campaign-26` the code vs
                       `slt-5196-brand-campaign-26` the dir). Without it we fall
                       back to matching `code` by name, which only works when
                       `code` already IS the dir slug (e.g. email-addressed).

    Signed with the standard MC-2→webhook secret (WEBHOOK_HMAC_SECRET, via
    signatures._verify_signature) — the same hop as promote-transcript /
    asset-ingest, NOT the Worker's INBOUND_EMAIL_SECRET (that one is only for
    the Cloudflare Worker → /api/inbound-email leg).

    Response mirrors /api/inbound-email's park+distill result, plus
    ``routed_from`` (the original reason).
    """
    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        req = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    message_id = (req.get("message_id") or "").strip()
    code = (req.get("code") or "").strip()
    project_id = (req.get("project_id") or "").strip() or None
    if not message_id or not code:
        raise HTTPException(status_code=400, detail="message_id and code are required")

    client = mc2_db.get_client(required=False)
    if client is None:
        raise HTTPException(status_code=500, detail="MC-2 client unavailable")

    # Load the unrouted row — the source of truth for the email's content.
    resp = (
        client.table("unrouted_emails")
        .select("id, thread_id, from_addr, to_addr, subject, raw_text, received_at, status")
        .eq("id", message_id)
        .single()
        .execute()
    )
    row = resp.data
    if not row:
        raise HTTPException(status_code=404, detail=f"unrouted email {message_id!r} not found")
    if row.get("status") == "routed":
        # Idempotent: already routed (double-click / retry). Report, don't re-ingest.
        return {"status": "already_routed", "message_id": message_id, "code": row.get("routed_to_code")}

    # Rebuild the payload shape _park_and_distill expects, then re-strip from
    # the stored raw text (don't trust a stored delta — re-derive it).
    payload = {
        "message_id": row["id"],
        "in_reply_to": row.get("thread_id"),
        "from": row.get("from_addr"),
        "to": row.get("to_addr"),
        "subject": row.get("subject"),
        "text": row.get("raw_text") or "",
        "received_at": row.get("received_at"),
    }
    strip = email_strip.strip_quoted_history(payload["text"])

    with git_ops._cloned_tenant() as tenant_root:
        config = load_config(tenant_root)
        try:
            # UUID-first (project_id) so a synced project resolves off its
            # cp.md MC-id stamp regardless of code/slug drift; falls back to
            # matching `code` by dir name.
            project_dir = find_spine_dir(config.root, code, mc2_id=project_id)
        except SpineDirNotFound:
            # The project has no working dir in the cp tree yet — a brand-new
            # Deal that hasn't been synced. Leave the row unrouted (the human
            # can sync it and route again) and say so plainly.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{code}' isn't set up in cp yet — no working directory. "
                    "Run a sync for it first, then route this email."
                ),
            ) from None

        result = _park_and_distill(
            tenant_root=tenant_root,
            config=config,
            project_dir=project_dir,
            code=code,
            payload=payload,
            strip=strip,
        )

    # Flip the row to routed only after the ingest actually committed.
    try:
        client.table("unrouted_emails").update(
            {"status": "routed", "routed_to_code": code}
        ).eq("id", message_id).execute()
    except Exception:  # noqa: BLE001 — the ingest succeeded; a status-flip miss is recoverable
        log.warning(
            "route-email: ingest committed for %s → %s but status flip failed",
            message_id, code, exc_info=True,
        )

    result["routed_from"] = "unrouted"
    result["message_id"] = message_id
    return result


def _park_and_distill(
    *,
    tenant_root,
    config,
    project_dir,
    code: str,
    payload: dict,
    strip: email_strip.StripResult,
) -> dict:
    """Park the raw email, distill its delta, and commit — the shared core.

    Runs INSIDE an open tenant clone (the caller owns the ``_cloned_tenant``
    context). Used by both ``/api/inbound-email`` (a cp+<code>@ that resolved)
    and ``/api/route-email`` (a human routed a previously-unrouted email to
    this code). Returns the response dict sans ``shape`` (the caller adds it).
    """
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
    #    _ingest_one_project the Fathom path runs. Everything it writes lands
    #    PROPOSED (the review gate). Skipped when the delta is empty: a
    #    pure-scheduling forward strips to nothing, and the "produce nothing"
    #    discipline says don't invent a plan from an empty body.
    delta = (strip.delta or "").strip()
    distill: dict | None = None
    if delta:
        distill_text = _render_distill_input(payload, delta)
        transcript_path = _stage_email_delta(
            tenant_root, code, payload.get("message_id", ""), distill_text
        )
        try:
            distill = pipeline._ingest_one_project(
                config=config,
                code=code,
                transcript_path=transcript_path,
                # No Fathom meeting behind an email: no action_items, no
                # meeting_id, no roster. _ingest_one_project guards each —
                # the LLM-only plan path runs cleanly.
            )
        except Exception:  # noqa: BLE001 — distill must never lose the parked mail
            log.warning(
                "inbound-email: distill failed for %s (email parked, committed anyway)",
                code, exc_info=True,
            )
            distill = {"errors": ["distill raised — see logs"], "files_written": []}
    else:
        log.info(
            "inbound-email: empty delta for %s — parked, no distill (scheduling-only?)",
            code,
        )

    # 3) ONE commit sweeps the parked email + any distilled bullets
    #    (`git add -A` inside git_ops). The message attributes distill so the
    #    log distinguishes "distilled" from "parked only".
    mid_prefix = (payload.get("message_id", "") or "").strip().strip("<>")[:16]
    wrote = bool(distill and distill.get("files_written"))
    verb = "distilled" if wrote else "parked"
    commit_sha = git_ops._commit_with_message_and_push(
        tenant_root,
        f"[auto-ingest] email:{code}: {verb} {mid_prefix or 'message'}",
    )

    plan_summary = (distill or {}).get("plan_summary")
    distill_errors = (distill or {}).get("errors") or []
    log.info(
        "inbound-email done: code=%s file=%s stripped=%s distilled=%s plan=%s commit=%s",
        code, rel, strip.stripped, bool(plan_summary), plan_summary, commit_sha,
    )
    return {
        "status": "ingested" if (distill and not distill_errors) else "parked",
        "code": code,
        "parked_path": str(rel),
        "stripped": strip.stripped,
        "distilled": bool(distill),
        "plan_summary": plan_summary,
        "files_written": (distill or {}).get("files_written", []),
        "distill_errors": distill_errors,
        "commit_sha": commit_sha,
    }
