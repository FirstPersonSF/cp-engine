"""Tests for Slack-action endpoint surface on cp-engine-webhook (v0.14.0).

This file will grow across Tasks 3.1, 3.2, and 3.3. For now: just the
signature verifier.
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import time
from pathlib import Path

import pytest

# webhook/ is a sibling of src/; mirror the import shim from test_webhook_rerun.py.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))


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
