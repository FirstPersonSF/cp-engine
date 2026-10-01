"""webhook/cron/trigger.py — the Railway cron service's start command.

Pins: slot derivation from the current UTC time (and refusing to guess when
the fire is too late), the signature the webhook verifies, and exit codes
(0 on 2xx, 1 on non-2xx / network, 2 on config).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "webhook" / "cron" / "trigger.py"
_spec = importlib.util.spec_from_file_location("cron_trigger", _PATH)
trigger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trigger)

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

SCHED = "23 12,13 * * *"
LAG = timedelta(minutes=45)


@pytest.mark.parametrize("now,slot", [
    (datetime(2026, 9, 30, 12, 23, tzinfo=UTC), "23 12 * * *"),
    (datetime(2026, 9, 30, 12, 29, tzinfo=UTC), "23 12 * * *"),   # a few min late
    (datetime(2026, 9, 30, 12, 22, tzinfo=UTC), "23 12 * * *"),   # a minute early
    (datetime(2026, 9, 30, 13, 25, tzinfo=UTC), "23 13 * * *"),
])
def test_derive_slot(now, slot):
    assert trigger.derive_slot(SCHED, now, LAG) == slot


def test_a_fire_too_late_to_attribute_is_refused_not_guessed():
    with pytest.raises(trigger.ConfigError, match="not guessing"):
        trigger.derive_slot(SCHED, datetime(2026, 9, 30, 15, 0, tzinfo=UTC), LAG)


def test_non_daily_schedule_is_a_config_error():
    with pytest.raises(trigger.ConfigError):
        trigger.parse_schedule("*/5 * * * *")


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _env(**kw):
    return {"CP_WEBHOOK_URL": "https://hook.example", "WEBHOOK_HMAC_SECRET": "s3",
            "CRON_SCHEDULE": SCHED, **kw}


NOW = datetime(2026, 9, 30, 12, 24, tzinfo=UTC)


def test_signs_what_the_webhook_verifies(monkeypatch):
    """The request the trigger builds passes `signatures._verify_signature`
    with the timestamp gate ON (production sets WEBHOOK_REQUIRE_TIMESTAMP)."""
    import signatures

    seen = {}

    def opener(req, timeout):
        seen["req"] = req
        return _Resp(200, {"ok": True, "status": "posted"})

    assert trigger.run(_env(), now=NOW, opener=opener) == 0
    req = seen["req"]
    assert req.full_url == "https://hook.example/api/cron/health"
    assert json.loads(req.data) == {"slot": "23 12 * * *"}
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "s3")
    monkeypatch.setenv("WEBHOOK_REQUIRE_TIMESTAMP", "true")
    signatures._verify_signature(req.data, req.get_header("X-webhook-signature"),
                                 req.get_header("X-webhook-timestamp"))  # no raise
    from fastapi import HTTPException

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "other")
    with pytest.raises(HTTPException, match="invalid signature"):
        signatures._verify_signature(req.data, req.get_header("X-webhook-signature"),
                                     req.get_header("X-webhook-timestamp"))


@pytest.mark.parametrize("status,body,code", [
    (200, {"ok": True, "status": "posted"}, 0),
    (200, {"ok": True, "status": "skipped"}, 0),
    (200, {"ok": True, "status": "duplicate"}, 0),
    (200, {"ok": True, "status": "running"}, 0),
    (202, {"ok": True, "status": "accepted"}, 0),   # sync / draft-summaries
])
def test_exit_zero_on_2xx(status, body, code):
    assert trigger.run(_env(), now=NOW, opener=lambda r, timeout: _Resp(status, body)) == code


@pytest.mark.parametrize("status", [401, 404, 500, 502])
def test_exit_nonzero_on_non_2xx(status):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, status, "x", {},
                                     io.BytesIO(b'{"ok": false, "status": "failed"}'))

    assert trigger.run(_env(), now=NOW, opener=opener) == 1


def test_exit_nonzero_on_network_error():
    def opener(req, timeout):
        raise urllib.error.URLError("connection refused")

    assert trigger.run(_env(), now=NOW, opener=opener) == 1


def test_exit_two_on_missing_config_or_unattributable_fire():
    never = lambda r, timeout: pytest.fail("must not POST")  # noqa: E731
    assert trigger.run({"CRON_SCHEDULE": SCHED}, now=NOW, opener=never) == 2
    late = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    assert trigger.run(_env(), now=late, opener=never) == 2


def test_cron_slot_overrides_derivation_and_dry_run_passes_through():
    seen = {}

    def opener(req, timeout):
        seen["body"] = json.loads(req.data)
        return _Resp(200, {"ok": True, "status": "dry_run"})

    late = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    assert trigger.run(_env(CRON_SLOT="23 13 * * *", CRON_DRY_RUN="1"),
                       now=late, opener=opener) == 0
    assert seen["body"] == {"slot": "23 13 * * *", "dry_run": True}


# ── the sync and draft-summaries schedules (Railway cron, 2026-10) ─────────

MONDAY = datetime(2026, 10, 5, tzinfo=UTC)   # 2026-10-05 is a Monday
SYNC_SCHED = "0 14,22 * * *"
DRAFT_SCHED = "17 12,13 * * 1"


@pytest.mark.parametrize("now,slot", [
    (MONDAY.replace(hour=14, minute=1), "0 14 * * *"),
    (MONDAY.replace(hour=13, minute=59), "0 14 * * *"),     # a minute early
    (MONDAY.replace(hour=22, minute=4), "0 22 * * *"),
])
def test_sync_schedule_slots(now, slot):
    assert trigger.derive_slot(SYNC_SCHED, now, LAG) == slot


def test_sync_fire_between_slots_is_refused():
    with pytest.raises(trigger.ConfigError, match="not guessing"):
        trigger.derive_slot(SYNC_SCHED, MONDAY.replace(hour=18), LAG)


@pytest.mark.parametrize("now,slot", [
    (MONDAY.replace(hour=12, minute=18), "17 12 * * 1"),
    (MONDAY.replace(hour=13, minute=25), "17 13 * * 1"),
])
def test_weekly_schedule_slots_carry_the_weekday(now, slot):
    """FAILS on the daily-only trigger: `17 12,13 * * 1` was a ConfigError."""
    assert trigger.derive_slot(DRAFT_SCHED, now, LAG) == slot


@pytest.mark.parametrize("now", [
    datetime(2026, 10, 6, 12, 18, tzinfo=UTC),   # Tuesday, same clock time
    datetime(2026, 10, 4, 13, 18, tzinfo=UTC),   # Sunday
])
def test_weekly_schedule_off_day_is_refused(now):
    with pytest.raises(trigger.ConfigError, match="not guessing"):
        trigger.derive_slot(DRAFT_SCHED, now, LAG)


def test_sunday_is_0_or_7():
    sunday = datetime(2026, 10, 4, 9, 1, tzinfo=UTC)
    assert trigger.derive_slot("0 9 * * 0", sunday, LAG) == "0 9 * * 0"
    assert trigger.derive_slot("0 9 * * 7", sunday, LAG) == "0 9 * * 0"


@pytest.mark.parametrize("bad", ["17 12 1 * *", "17 12 * 10 *", "17 12 * * 1-5", "17 12 * * */2"])
def test_unsupported_fields_are_config_errors(bad):
    with pytest.raises(trigger.ConfigError):
        trigger.parse_schedule(bad)


def test_trigger_posts_the_weekly_slot_to_its_job():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return _Resp(202, {"ok": True, "status": "accepted"})

    env = _env(CRON_JOB="draft-summaries", CRON_SCHEDULE=DRAFT_SCHED)
    assert trigger.run(env, now=MONDAY.replace(hour=12, minute=17), opener=opener) == 0
    assert seen["url"].endswith("/api/cron/draft-summaries")
    assert seen["body"] == {"slot": "17 12 * * 1"}
