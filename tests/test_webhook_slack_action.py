"""Tests for Slack-action endpoint surface on cp-engine-webhook (v0.14.0).

This file will grow across Tasks 3.1, 3.2, and 3.3. For now: signature
verifier (Task 3.1) + endpoint dispatch (Task 3.2).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# webhook/ is a sibling of src/; mirror the import shim from test_webhook_rerun.py.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))


@pytest.fixture
def client() -> TestClient:
    import main as webhook_main
    return TestClient(webhook_main.app)


def _signed_slack_request(
    payload: dict, secret: bytes = b"test-slack-secret"
) -> tuple[bytes, dict]:
    """Build a properly-signed Slack interactive payload + headers."""
    import urllib.parse
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(
        secret, f"v0:{ts}:".encode() + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Signature": sig,
        "X-Slack-Request-Timestamp": ts,
    }
    return body, headers


def test_slack_signature_valid(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    from main import _verify_slack_signature
    body = b'{"type":"block_actions"}'
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(
        b"test-slack-secret",
        f"v0:{ts}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    # Should not raise.
    _verify_slack_signature(body, ts, sig)


def test_slack_signature_rejects_replay(monkeypatch):
    """Slack timestamps older than 5 minutes are rejected to prevent replay."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    from main import _verify_slack_signature
    from fastapi import HTTPException
    body = b"{}"
    old_ts = str(int(time.time()) - 600)  # 10 min ago
    sig = "v0=" + hmac.new(
        b"test-slack-secret",
        f"v0:{old_ts}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(HTTPException) as exc:
        _verify_slack_signature(body, old_ts, sig)
    assert exc.value.status_code == 401


def test_slack_signature_rejects_bad_sig(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    from main import _verify_slack_signature
    from fastapi import HTTPException
    body = b"{}"
    ts = str(int(time.time()))
    with pytest.raises(HTTPException):
        _verify_slack_signature(body, ts, "v0=0000000000000000")


# ─────────────────────────────────────────────────────────────
#  Task 3.2: /slack-action endpoint dispatch
# ─────────────────────────────────────────────────────────────


def test_slack_action_resolve_risk_acks_immediately_and_queues_work(
    monkeypatch, client
):
    """A button click with value 'resolve-risk|ibx-5167|09e3d0c7':
    - Returns 200 to Slack synchronously (the 3-second ack window).
    - Schedules the actual plan run as a background asyncio task.
    - That background task calls _run_plan_for_one_item with the right args.
    """
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}

    def fake_run(*, verb, code, cp_hash, **extras):
        called["verb"] = verb
        called["code"] = code
        called["hash"] = cp_hash
        called["extras"] = extras
        return {"committed": True, "commit_sha": "abc123", "errors": []}

    update_calls = []

    def fake_update(*, response_url, original_message, confirmation):
        update_calls.append({"url": response_url, "confirmation": confirmation})

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(webhook_main, "_post_response_url_update", fake_update)

    payload = {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "actions": [{
            "action_id": "resolve-risk_ibx-5167_09e3d0c7",
            "value": "resolve-risk|ibx-5167|09e3d0c7",
            "type": "button",
        }],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {"ts": "1234.5678", "blocks": []},
        "trigger_id": "trg-abc",
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json().get("queued") is True

    # Background task should have run by now.
    for _ in range(50):
        if called:
            break
        time.sleep(0.01)

    assert called == {
        "verb": "resolve-risk",
        "code": "ibx-5167",
        "hash": "09e3d0c7",
        "extras": {"closed_by": "slack", "user": "U02CF0L7M"},
    }
    assert len(update_calls) == 1
    assert "Resolved" in update_calls[0]["confirmation"]


def test_slack_action_snooze_7d_passes_until_date(monkeypatch, client):
    """Snooze-7d buttons compute today+7 in the handler before dispatching."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}

    def fake_run(*, verb, code, cp_hash, **extras):
        called["verb"] = verb
        called["code"] = code
        called["extras"] = extras
        return {"committed": True, "commit_sha": "def456", "errors": []}

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(
        webhook_main, "_post_response_url_update", lambda **_kw: None
    )

    payload = {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "actions": [{
            "action_id": "snooze-risk-7d_ibx-5167_09e3d0c7",
            "value": "snooze-risk-7d|ibx-5167|09e3d0c7",
            "type": "button",
        }],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {"blocks": []},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200

    for _ in range(50):
        if called:
            break
        time.sleep(0.01)

    from datetime import date as _date, timedelta as _td
    expected_until = (_date.today() + _td(days=7)).isoformat()
    assert called["verb"] == "snooze-risk"  # the -7d suffix was stripped
    assert called["extras"]["until"] == expected_until


def test_slack_action_rejects_invalid_value_format(monkeypatch, client):
    """Malformed value (only 2 pipe segments) returns 400 — NOT a
    background-task crash. Validation must happen BEFORE create_task."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    payload = {
        "type": "block_actions",
        "actions": [
            {"action_id": "x", "value": "only-two|parts", "type": "button"}
        ],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 400


def test_confirmation_text_surfaces_no_match_as_silent_dedupe() -> None:
    """When _run_plan_for_one_item returns committed=False, no errors, no sha
    (silent dedupe path: hash not in file because already resolved or moved
    sprint), _confirmation_text must NOT render '✅ Resolved' — that misleads
    the user. Render an explicit 'No matching item' message instead."""
    from main import _confirmation_text
    result = {"committed": False, "commit_sha": None, "errors": []}
    msg = _confirmation_text(verb="resolve-risk", extras={}, result=result)
    assert "No matching item" in msg
    assert "✅ Resolved" not in msg


def test_confirmation_text_renders_success_when_committed() -> None:
    """When committed=True with a SHA, render the success label + short SHA."""
    from main import _confirmation_text
    result = {"committed": True, "commit_sha": "abc12345def67890", "errors": []}
    msg = _confirmation_text(verb="resolve-risk", extras={}, result=result)
    assert "✅ Resolved" in msg
    assert "abc12345" in msg


def test_confirmation_text_renders_failure_when_errors_present() -> None:
    """When errors are present and no SHA, render the failure label with
    the first error text (truncated)."""
    from main import _confirmation_text
    result = {"committed": False, "commit_sha": None, "errors": ["hash not found"]}
    msg = _confirmation_text(verb="resolve-risk", extras={}, result=result)
    assert "⚠️ Action failed" in msg
    assert "hash not found" in msg


def test_post_response_url_update_logs_non_ok_status(monkeypatch, caplog):
    """If Slack's response_url returns non-2xx, log a warning with the status
    code and response body (truncated). Don't raise — fire-and-forget UX."""
    from main import _post_response_url_update
    import logging as _logging

    class FakeResp:
        status_code = 410
        text = "expired response_url"
        ok = False

    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)

    with caplog.at_level(_logging.WARNING):
        _post_response_url_update(
            response_url="https://hooks.slack.com/expired",
            original_message={"blocks": []},
            confirmation="✅ Done",
        )
    assert any("410" in rec.message for rec in caplog.records)
    assert captured["url"] == "https://hooks.slack.com/expired"
