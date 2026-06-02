"""Tests for webhook HMAC replay protection + run_id binding (v0.15.2 Fix 1).

The /api/auto-ingest and /api/auto-ingest/runs/{run_id}/rerun endpoints
previously used body-only HMAC. A captured signature could be replayed
forever, and the rerun endpoint ignored the body — meaning one captured
signature opened up rerun of any failed run.

This patch:
  - Folds X-Webhook-Timestamp into the HMAC base (Slack-style)
  - Rejects stale timestamps (>5min old/future) when the env gate is on
  - Logs a warning + accepts legacy unsigned-timestamp requests when the
    gate is off (phased rollout)
  - Binds rerun signatures to a specific run_id via the body
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _sign_legacy(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _sign_with_ts(
    body: bytes, *, ts: str, secret: bytes = b"test-secret"
) -> str:
    base = f"{ts}.".encode() + body
    return hmac.new(secret, base, hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────────────────────────────
#  Auto-ingest endpoint signature tests
# ─────────────────────────────────────────────────────────────────────


def test_auto_ingest_valid_timestamp_and_hmac_accepted(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Fresh timestamp + matching ts-bound HMAC = 200 path runs."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.delenv("WEBHOOK_REQUIRE_TIMESTAMP", raising=False)
    monkeypatch.setattr(
        webhook_main,
        "_perform_auto_ingest",
        lambda **kw: {"ingested": [], "commit_sha": None, "skipped_no_op": True},
    )

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    ts = str(int(time.time()))
    sig = _sign_with_ts(body, ts=ts)
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={
            "x-webhook-signature": sig,
            "x-webhook-timestamp": ts,
        },
    )
    assert resp.status_code == 200, resp.text


def test_auto_ingest_stale_timestamp_rejected_when_gate_enabled(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """When WEBHOOK_REQUIRE_TIMESTAMP=true, a stale ts (>5min old) is 401."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("WEBHOOK_REQUIRE_TIMESTAMP", "true")

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    stale_ts = str(int(time.time()) - 400)  # 6m40s old
    sig = _sign_with_ts(body, ts=stale_ts)
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={
            "x-webhook-signature": sig,
            "x-webhook-timestamp": stale_ts,
        },
    )
    assert resp.status_code == 401
    assert "freshness" in resp.text.lower()


def test_auto_ingest_future_timestamp_rejected_when_gate_enabled(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A ts far in the future is also rejected (clock skew / attacker)."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("WEBHOOK_REQUIRE_TIMESTAMP", "true")

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    future_ts = str(int(time.time()) + 400)
    sig = _sign_with_ts(body, ts=future_ts)
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={
            "x-webhook-signature": sig,
            "x-webhook-timestamp": future_ts,
        },
    )
    assert resp.status_code == 401


def test_auto_ingest_missing_timestamp_accepted_with_warning_when_gate_off(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, caplog
) -> None:
    """Without WEBHOOK_REQUIRE_TIMESTAMP, the old body-only HMAC path
    keeps working — backwards compat during the rollout window."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.delenv("WEBHOOK_REQUIRE_TIMESTAMP", raising=False)
    monkeypatch.setattr(
        webhook_main,
        "_perform_auto_ingest",
        lambda **kw: {"ingested": [], "commit_sha": None, "skipped_no_op": True},
    )

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    sig = _sign_legacy(body)
    with caplog.at_level(logging.WARNING, logger="cp-engine-webhook"):
        resp = client.post(
            "/api/auto-ingest",
            content=body,
            headers={"x-webhook-signature": sig},
        )
    assert resp.status_code == 200, resp.text
    assert any("legacy" in r.message.lower() for r in caplog.records)


def test_auto_ingest_missing_timestamp_rejected_when_gate_enabled(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Once we flip the gate, missing timestamp = 401."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("WEBHOOK_REQUIRE_TIMESTAMP", "true")

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    sig = _sign_legacy(body)
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 401
    assert "timestamp" in resp.text.lower()


def test_auto_ingest_timestamp_not_int_rejected(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    body = b"{}"
    sig = _sign_with_ts(body, ts="not-an-int")
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={
            "x-webhook-signature": sig,
            "x-webhook-timestamp": "not-an-int",
        },
    )
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────
#  Rerun endpoint run_id binding
# ─────────────────────────────────────────────────────────────────────


def test_rerun_body_run_id_must_match_url(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A captured signed body for run-A cannot be replayed against run-B."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    monkeypatch.delenv("WEBHOOK_REQUIRE_TIMESTAMP", raising=False)

    body = json.dumps({"run_id": "run-A"}).encode()
    sig = _sign_legacy(body)
    resp = client.post(
        "/api/auto-ingest/runs/run-B/rerun",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 400
    assert "run_id" in resp.text


def test_rerun_body_run_id_matching_url_accepted(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Body-bound rerun with matching run_id flows through to the rerun
    loader. The Supabase lookup is stubbed."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    failed_row = {
        "meeting_id": "m1",
        "project_codes": ["ggl-5168"],
        "status": "failed",
    }
    sb_client = MagicMock()
    sb_client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = failed_row
    monkeypatch.setattr(
        webhook_main, "create_client", lambda u, k: sb_client, raising=False
    )

    monkeypatch.setattr(
        webhook_main,
        "_perform_auto_ingest",
        lambda **kw: {"ingested": [], "commit_sha": None, "skipped_no_op": True},
    )

    body = json.dumps({"run_id": "run-A"}).encode()
    sig = _sign_legacy(body)
    resp = client.post(
        "/api/auto-ingest/runs/run-A/rerun",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 200, resp.text


def test_rerun_empty_body_rejected_when_gate_enabled(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """With the gate flipped, an empty-body rerun is 400 (must bind to run_id)."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    monkeypatch.setenv("WEBHOOK_REQUIRE_TIMESTAMP", "true")

    body = b""
    ts = str(int(time.time()))
    sig = _sign_with_ts(body, ts=ts)
    resp = client.post(
        "/api/auto-ingest/runs/run-A/rerun",
        content=body,
        headers={
            "x-webhook-signature": sig,
            "x-webhook-timestamp": ts,
        },
    )
    assert resp.status_code == 400
