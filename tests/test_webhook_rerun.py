"""Tests for POST /api/auto-ingest/runs/:id/rerun (v0.13.0).

Failed auto_ingest_runs rows should be re-runnable from the dashboard.
The endpoint loads the original payload (meeting_id + project_codes)
from Supabase, re-fires the pipeline, and writes a NEW auto_ingest_runs
row — never mutates the original (which stays as a failure record for
diagnostics).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main
import pipeline


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_rerun_endpoint_loads_failed_row_and_refires(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """POST .../runs/:id/rerun reads the failed run from Supabase,
    extracts (meeting_id, project_codes), and calls _perform_auto_ingest."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    # Stub Supabase: the failed row we want to rerun.
    failed_row = {
        "meeting_id": "meet-abc",
        "project_codes": ["ggl-5168"],
        "status": "failed",
    }
    sb_client = MagicMock()
    sb_client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = failed_row
    monkeypatch.setattr("supabase.create_client", lambda url, key: sb_client, raising=False)

    # Stub _perform_auto_ingest to record the call without actually running.
    called_with: dict = {}

    def fake_perform(*, meeting_id, project_codes, transcript_text=None):
        called_with["meeting_id"] = meeting_id
        called_with["project_codes"] = project_codes
        called_with["transcript_text"] = transcript_text
        return {"ingested": [], "commit_sha": None, "skipped_no_op": True}

    monkeypatch.setattr(pipeline, "_perform_auto_ingest", fake_perform)

    body = b""
    sig = _signed(body)
    resp = client.post(
        "/api/auto-ingest/runs/run-xyz/rerun",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 200, resp.text
    assert called_with["meeting_id"] == "meet-abc"
    assert called_with["project_codes"] == ["ggl-5168"]
    assert called_with["transcript_text"] is None


def test_rerun_endpoint_404_when_row_not_found(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    sb_client = MagicMock()
    sb_client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = None
    monkeypatch.setattr("supabase.create_client", lambda url, key: sb_client, raising=False)

    body = b""
    sig = _signed(body)
    resp = client.post(
        "/api/auto-ingest/runs/missing-id/rerun",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 404


def test_rerun_endpoint_rejects_non_failed_status(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Only failed runs may be re-fired. A successful run rerun is risky
    (sprint file may have been hand-edited) — block it."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    sb_client = MagicMock()
    sb_client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "meeting_id": "meet-abc",
        "project_codes": ["ggl-5168"],
        "status": "success",
    }
    monkeypatch.setattr("supabase.create_client", lambda url, key: sb_client, raising=False)

    body = b""
    sig = _signed(body)
    resp = client.post(
        "/api/auto-ingest/runs/run-xyz/rerun",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 400
    assert "failed" in resp.text.lower()


def test_rerun_endpoint_requires_signature(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    resp = client.post("/api/auto-ingest/runs/run-xyz/rerun", content=b"")
    assert resp.status_code == 401
