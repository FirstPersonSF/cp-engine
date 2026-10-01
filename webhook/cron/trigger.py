"""Railway cron trigger: sign and POST ``{"slot": ...}`` to the webhook.

Runs as the start command of a Railway cron service (docs/railway-cron.md).
Standard library only — the image is python:3.11-slim plus this file.

WHICH SLOT FIRED. Railway's cron gives the process no schedule context, so
the trigger derives it: ``CRON_SCHEDULE`` repeats the service's schedule
(``"23 12,13 * * *"``) and the slot is the most recent scheduled time at or
before now. Railway fires "within a few minutes"; a fire more than
``CRON_MAX_LAG_MIN`` (default 45, under the 60-minute slot spacing) after its
slot cannot be attributed safely and exits nonzero instead of guessing — a
wrong guess would skip the day's post (or move it an hour). ``CRON_SLOT``
pins one slot explicitly (a one-slot service, or a manual run).

ENV.
    CP_WEBHOOK_URL       https://cp-engine-production.up.railway.app
    WEBHOOK_HMAC_SECRET  same secret as the webhook service (reference var)
    CRON_JOB             default "health"
    CRON_SCHEDULE        the service's cron schedule, verbatim
    CRON_SLOT            optional: send this slot, skip derivation
    CRON_MAX_LAG_MIN     optional, default 45
    CRON_DRY_RUN         optional "1": the webhook renders, never posts
    CRON_TIMEOUT_SEC     optional, default 240

EXIT. 0 on any 2xx (posted / skipped / duplicate / dry_run); 1 on a non-2xx
response or a network error; 2 on bad configuration or an unattributable fire.
Railway marks a nonzero exit as a failed run, which is the point.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

EARLY_TOLERANCE = timedelta(minutes=2)  # a fire slightly BEFORE its slot


class ConfigError(Exception):
    pass


def _ints(field: str, lo: int, hi: int) -> list[int]:
    try:
        vals = sorted({int(v) for v in field.split(",")})
    except ValueError:
        raise ConfigError(f"unsupported cron field {field!r} (integers and commas only)") from None
    if not vals or any(v < lo or v > hi for v in vals):
        raise ConfigError(f"cron field {field!r} out of range {lo}-{hi}")
    return vals


def parse_schedule(schedule: str) -> list[tuple[int, int]]:
    """``"23 12,13 * * *"`` → ``[(12, 23), (13, 23)]`` (hour, minute). Daily
    schedules only: the slot is what the health gate reads, and it is daily."""
    parts = schedule.split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        raise ConfigError(f"CRON_SCHEDULE must be daily 'M H[,H] * * *', got {schedule!r}")
    minutes = _ints(parts[0], 0, 59)
    hours = _ints(parts[1], 0, 23)
    return sorted((h, m) for h in hours for m in minutes)


def derive_slot(schedule: str, now: datetime, max_lag: timedelta) -> str:
    """The single-slot cron expression (``"23 12 * * *"``) for the fire at
    `now` (UTC). Raises ConfigError when no slot is within ``max_lag``."""
    now = now.astimezone(UTC)
    best = None
    for day in (now.date() - timedelta(days=1), now.date()):
        for h, m in parse_schedule(schedule):
            at = datetime(day.year, day.month, day.day, h, m, tzinfo=UTC)
            if at <= now + EARLY_TOLERANCE and (best is None or at > best):
                best = at
    if best is None or now - best > max_lag:
        raise ConfigError(
            f"fired at {now:%H:%M}Z, no slot of {schedule!r} within "
            f"{int(max_lag.total_seconds() // 60)} min — not guessing")
    return f"{best.minute} {best.hour} * * *"


def sign(secret: str, body: bytes, ts: str) -> str:
    """The webhook's replay-protected shape: hmac(secret, f"{ts}.{body}")."""
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def run(env: dict | None = None, now: datetime | None = None, opener=None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout
    try:
        url = (env.get("CP_WEBHOOK_URL") or "").rstrip("/")
        secret = env.get("WEBHOOK_HMAC_SECRET") or ""
        if not url or not secret:
            raise ConfigError("CP_WEBHOOK_URL and WEBHOOK_HMAC_SECRET are required")
        job = env.get("CRON_JOB") or "health"
        slot = (env.get("CRON_SLOT") or "").strip()
        if not slot:
            schedule = env.get("CRON_SCHEDULE") or ""
            if not schedule:
                raise ConfigError("set CRON_SCHEDULE (the service's schedule) or CRON_SLOT")
            lag = timedelta(minutes=int(env.get("CRON_MAX_LAG_MIN") or 45))
            slot = derive_slot(schedule, now or datetime.now(UTC), lag)
        timeout = float(env.get("CRON_TIMEOUT_SEC") or 240)
    except (ConfigError, ValueError) as exc:
        print(f"cron trigger: config error: {exc}", file=out)
        return 2

    payload = {"slot": slot}
    if (env.get("CRON_DRY_RUN") or "").strip().lower() in {"1", "true", "yes"}:
        payload["dry_run"] = True
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(
        f"{url}/api/cron/{job}", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Webhook-Timestamp": ts,
                 "X-Webhook-Signature": sign(secret, body, ts)},
    )
    open_ = opener or urllib.request.urlopen
    print(f"cron trigger: POST /api/cron/{job} slot={slot!r}", file=out)
    try:
        with open_(req, timeout=timeout) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"cron trigger: request failed: {exc}", file=out)
        return 1
    try:
        parsed = json.loads(text)
        summary = " · ".join(str(v) for v in (
            parsed.get("status"), parsed.get("reason"), parsed.get("error"),
            parsed.get("detail"), *(parsed.get("warnings") or [])) if v)
        lines = parsed.get("lines")
    except (ValueError, AttributeError):
        summary, lines = text[:300], None
    print(f"cron trigger: HTTP {status} · {summary}", file=out)
    if lines:
        print(lines, file=out)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(run())
