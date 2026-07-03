"""Tests for Fathom duplicate-delivery idempotency (v0.15.2 Fix 3 — I5).

Fathom retries the auto-ingest call on socket timeout. Before this fix,
both calls cloned the tenant, ran the LLM plan, and pushed —
``execute_plan`` deduped the actual on-disk bullet writes via content
hash, but auto_ingest_runs picked up two rows AND a second Claude
call was spent for nothing.

The fix checks for an existing ``status='success'`` row with the same
(meeting_id, sorted project_codes) tuple BEFORE cloning, and short-
circuits to a 200 with ``status: duplicate_delivery_skipped``.

The rerun endpoint must NOT short-circuit — explicit reruns are
intentional.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
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


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_first_delivery_runs_pipeline(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """No prior success row -> _perform_auto_ingest is called normally."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main, "_find_successful_duplicate_run", lambda mid, codes: None
    )
    called: dict = {}

    def fake_perform(*, meeting_id, project_codes, transcript_text=None):
        called["yes"] = True
        return {"ingested": [], "commit_sha": None, "skipped_no_op": True}

    monkeypatch.setattr(webhook_main, "_perform_auto_ingest", fake_perform)

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )
    assert resp.status_code == 200, resp.text
    assert called == {"yes": True}


def test_duplicate_delivery_short_circuits(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A second delivery with the same (meeting_id, project_codes) returns
    200 with status=duplicate_delivery_skipped and the existing run_id —
    no clone, no LLM, no second auto_ingest_runs row."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main,
        "_find_successful_duplicate_run",
        lambda mid, codes: "existing-run-id-uuid",
    )
    perform_called: dict = {}

    def fake_perform(**_kw):
        perform_called["yes"] = True
        return {"ingested": [], "commit_sha": None, "skipped_no_op": True}

    monkeypatch.setattr(webhook_main, "_perform_auto_ingest", fake_perform)

    body = json.dumps({"meeting_id": "m1", "project_codes": ["ggl-5168"]}).encode()
    resp = client.post(
        "/api/auto-ingest",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "duplicate_delivery_skipped"
    assert data["existing_run_id"] == "existing-run-id-uuid"
    assert data["meeting_id"] == "m1"
    assert data["project_codes"] == ["ggl-5168"]
    # Critical: _perform_auto_ingest must NOT have been entered.
    assert perform_called == {}


def test_dedupe_lookup_sorts_project_codes(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry that reordered project_codes (e.g., ['b', 'a'] vs ['a', 'b'])
    still finds the existing run."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    captured_args: dict = {}
    sb_client = MagicMock()
    # Returns one row with codes in a different order than what we'll query
    sb_client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
        {"id": "uuid-1", "project_codes": ["ggl-5136", "ggl-5168"]}
    ]

    def fake_create(url, key):
        captured_args["url"] = url
        captured_args["key"] = key
        return sb_client

    monkeypatch.setattr("supabase.create_client", fake_create, raising=False)

    # Query order is reversed from stored order; sort should normalize.
    found = webhook_main._find_successful_duplicate_run(
        "m1", ["ggl-5168", "ggl-5136"]
    )
    assert found == "uuid-1"


def test_dedupe_returns_none_when_codes_differ(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same meeting_id but DIFFERENT project_codes is not a duplicate."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    sb_client = MagicMock()
    sb_client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
        {"id": "uuid-1", "project_codes": ["ggl-5168"]}
    ]
    monkeypatch.setattr(
        "supabase.create_client", lambda u, k: sb_client, raising=False
    )

    # Different code -> not a duplicate
    found = webhook_main._find_successful_duplicate_run("m1", ["ibx-5167"])
    assert found is None


def test_dedupe_lookup_failure_degrades_to_no_duplicate(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Supabase is down, the dedupe check must not break the request —
    return None so the caller falls through to running the pipeline."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")

    def boom(url, key):
        raise RuntimeError("supabase down")

    monkeypatch.setattr("supabase.create_client", boom, raising=False)

    assert webhook_main._find_successful_duplicate_run("m1", ["ggl-5168"]) is None


def test_dedupe_returns_none_when_supabase_env_unset(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Supabase env -> can't dedupe, treat as 'not a duplicate' so
    the pipeline runs (best-effort observability stance)."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    assert webhook_main._find_successful_duplicate_run("m1", ["x"]) is None


def test_rerun_endpoint_does_not_dedupe(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Reruns from the dashboard are intentional — even if there's a
    later successful run for the same (meeting_id, project_codes), the
    rerun endpoint must still process the request."""
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
        "supabase.create_client", lambda u, k: sb_client, raising=False
    )

    # Wire a dedupe match — the rerun MUST still run, ignoring it.
    monkeypatch.setattr(
        webhook_main,
        "_find_successful_duplicate_run",
        lambda mid, codes: "would-be-skipped-uuid",
    )
    called: dict = {}

    def fake_perform(**kw):
        called["yes"] = True
        return {"ingested": [], "commit_sha": None, "skipped_no_op": True}

    monkeypatch.setattr(webhook_main, "_perform_auto_ingest", fake_perform)

    body = b""
    sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/api/auto-ingest/runs/run-xyz/rerun",
        content=body,
        headers={"x-webhook-signature": sig},
    )
    assert resp.status_code == 200, resp.text
    assert called == {"yes": True}
