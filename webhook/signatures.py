"""HMAC + timestamp verification for every inbound webhook route.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

from fastapi import HTTPException

log = logging.getLogger("cp-engine-webhook")


# Replay-window (seconds). Matches the Slack signature freshness budget.
_TIMESTAMP_REPLAY_WINDOW_SEC = 300


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _verify_signature(
    raw_body: bytes, provided: str, timestamp: str | None = None
) -> None:
    """HMAC-SHA256 verify the fathom-meeting-sync -> cp-engine-webhook body.

    Two HMAC shapes are accepted to allow a phased rollout:

    1. Legacy (no timestamp): ``hmac(secret, body)`` — accepted when
       ``timestamp`` is empty AND the env var ``WEBHOOK_REQUIRE_TIMESTAMP``
       is unset/false. A warning is logged so we can monitor when the
       caller side finishes rolling out.

    2. Replay-protected: ``hmac(secret, f"{timestamp}.{body}")`` —
       always accepted. The timestamp is rejected (401) if it's older
       or further in the future than 5 minutes (``abs(now - ts) > 300``).

    When ``WEBHOOK_REQUIRE_TIMESTAMP`` is true, missing timestamps and
    legacy-shape signatures both 401. This is the gate we flip after
    fathom-meeting-sync ships its own update.

    ``timestamp`` is Unix epoch seconds as a string (matches the Slack
    pattern at ``_verify_slack_signature``).
    """
    secret = os.environ.get("WEBHOOK_HMAC_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="WEBHOOK_HMAC_SECRET not configured")
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Webhook-Signature header")

    require_ts = _truthy_env("WEBHOOK_REQUIRE_TIMESTAMP")

    if not timestamp:
        if require_ts:
            raise HTTPException(
                status_code=401, detail="missing X-Webhook-Timestamp header"
            )
        # Phased rollout: accept the legacy body-only shape but log it
        # so we know when the caller side has cut over.
        log.warning(
            "webhook-verify: legacy unsigned-timestamp request accepted "
            "(set WEBHOOK_REQUIRE_TIMESTAMP=true to enforce)"
        )
        expected_legacy = hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_legacy, provided):
            raise HTTPException(status_code=401, detail="invalid signature")
        return

    try:
        ts_int = int(timestamp)
    except ValueError:
        raise HTTPException(
            status_code=401, detail="X-Webhook-Timestamp not an integer"
        ) from None

    skew = time.time() - ts_int
    if abs(skew) > _TIMESTAMP_REPLAY_WINDOW_SEC:
        log.warning(
            "webhook-verify rejected: timestamp outside %ds window (skew=%.1fs)",
            _TIMESTAMP_REPLAY_WINDOW_SEC, skew,
        )
        raise HTTPException(
            status_code=401, detail="X-Webhook-Timestamp outside freshness window"
        )

    base = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")


def _verify_clickup_signature(raw_body: bytes, provided: str) -> None:
    """HMAC-SHA256 validate a ClickUp webhook payload.

    ClickUp signs each webhook with HMAC-SHA256 (hex) under the
    `X-Signature` header, using a secret returned when the webhook is
    registered via their API. See:
    https://developer.clickup.com/docs/webhooksignature

    Uses CLICKUP_WEBHOOK_SECRET — kept distinct from the Fathom webhook's
    WEBHOOK_HMAC_SECRET so they can be rotated independently.
    """
    secret = os.environ.get("CLICKUP_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(
            status_code=500, detail="CLICKUP_WEBHOOK_SECRET not configured"
        )
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Signature header")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")


def _verify_slack_signature(raw_body: bytes, timestamp: str, provided: str) -> None:
    """Verify Slack's `X-Slack-Signature` header.

    Slack signs `v0:<timestamp>:<body>` with HMAC-SHA256 against
    SLACK_SIGNING_SECRET. The header value is `v0=<hex digest>`. Also
    enforces the 5-minute timestamp freshness window to prevent replays.

    Reference: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not secret:
        log.warning("slack-verify rejected: SLACK_SIGNING_SECRET not configured")
        raise HTTPException(500, "SLACK_SIGNING_SECRET not configured")
    if not provided or not provided.startswith("v0="):
        log.warning(
            "slack-verify rejected: missing or malformed X-Slack-Signature "
            "(provided=%r)", (provided or "")[:16],
        )
        raise HTTPException(401, "missing or malformed X-Slack-Signature")
    if not timestamp:
        log.warning("slack-verify rejected: missing X-Slack-Request-Timestamp")
        raise HTTPException(401, "missing X-Slack-Request-Timestamp")
    try:
        ts_int = int(timestamp)
    except ValueError:
        log.warning("slack-verify rejected: timestamp not an int: %r", timestamp[:32])
        raise HTTPException(401, "X-Slack-Request-Timestamp not an int") from None
    import time as _time
    skew = _time.time() - ts_int
    if abs(skew) > 300:
        log.warning(
            "slack-verify rejected: timestamp outside 5-min window (skew=%.1fs)",
            skew,
        )
        raise HTTPException(401, "Slack timestamp outside 5-minute freshness window")
    base = f"v0:{timestamp}:".encode() + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        # Diagnostic logging: never log the full secret OR the full signatures
        # (would leak HMAC state). Log first 6 hex chars of expected vs provided
        # so we can tell if it's a wrong-secret-entirely case (totally different
        # prefixes) vs body-encoding case (close prefixes but not exact match).
        log.warning(
            "slack-verify rejected: HMAC mismatch (expected_prefix=%s provided_prefix=%s "
            "body_len=%d secret_len=%d)",
            expected[:9],  # "v0=" + 6 hex chars
            provided[:9],
            len(raw_body),
            len(secret),
        )
        raise HTTPException(401, "invalid Slack signature")
