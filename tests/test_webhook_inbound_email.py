"""Tests for POST /api/inbound-email (Phase 1 — park-only pipe).

Covers the two units that don't require a live tenant clone:
  - plus-address parsing → (shape, code)
  - HMAC signature verification (timestamped, INBOUND_EMAIL_SECRET)
  - the route's early-return paths (unresolved address, non-project shape)

Parking itself (clone → write → commit → push) is exercised end-to-end
against the deployed service, not in unit tests — it needs git + SSH.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient  # noqa: E402

import main as webhook_main  # noqa: E402
from routers import email as email_router  # noqa: E402

_SECRET = "test-inbound-secret"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("INBOUND_EMAIL_SECRET", _SECRET)
    return TestClient(webhook_main.app)


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    base = f"{ts}.".encode() + raw
    sig = hmac.new(_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return raw, {"x-webhook-signature": sig, "x-webhook-timestamp": ts}


# ── plus-address parsing ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr,shape,code",
    [
        ("cp+ibx-5192@cp.firstperson.is", "project", "ibx-5192"),
        ("cp+mission-control@cp.firstperson.is", "project", "mission-control"),
        ("cp+account+infoblox@cp.firstperson.is", "account", "infoblox"),
        ("cp+planning+1p@cp.firstperson.is", "planning", "1p"),
        ("CP+IBX-5192@cp.firstperson.is", "project", "ibx-5192"),  # case-fold
    ],
)
def test_parse_plus_address(addr, shape, code):
    r = email_router.parse_plus_address(addr)
    assert r is not None
    assert r.shape == shape
    assert r.code == code


@pytest.mark.parametrize(
    "addr",
    [
        "drew@firstperson.is",  # no cp+ prefix
        "cp@cp.firstperson.is",  # no + suffix
        "cp+@cp.firstperson.is",  # empty suffix
        "",
    ],
)
def test_parse_plus_address_rejects(addr):
    assert email_router.parse_plus_address(addr) is None


# ── signature verification ────────────────────────────────────────────


def test_missing_signature_401(client):
    raw = json.dumps({"to": "cp+ibx-5192@cp.firstperson.is"}).encode()
    resp = client.post(
        "/api/inbound-email",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_bad_signature_401(client):
    body = {"to": "cp+ibx-5192@cp.firstperson.is"}
    raw, headers = _signed(body)
    headers["x-webhook-signature"] = "deadbeef"
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 401


def test_stale_timestamp_401(client):
    body = {"to": "cp+ibx-5192@cp.firstperson.is"}
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) - 10_000)  # way outside the 5-min window
    base = f"{ts}.".encode() + raw
    sig = hmac.new(_SECRET.encode(), base, hashlib.sha256).hexdigest()
    resp = client.post(
        "/api/inbound-email",
        content=raw,
        headers={"x-webhook-signature": sig, "x-webhook-timestamp": ts},
    )
    assert resp.status_code == 401


# ── early-return paths (no clone needed) ──────────────────────────────


def test_unresolved_address_200_noop(client):
    body = {"to": "drew@firstperson.is", "text": "hi"}
    raw, headers = _signed(body)
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "unresolved"


def test_account_shape_acknowledged_not_parked(client):
    body = {"to": "cp+account+infoblox@cp.firstperson.is", "text": "hi"}
    raw, headers = _signed(body)
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "acknowledged_not_parked"
    assert data["shape"] == "account"
    assert data["code"] == "infoblox"
