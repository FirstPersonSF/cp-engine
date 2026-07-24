"""Tests for POST /api/inbound-email (Phase 2 — park + distill).

Covers:
  - plus-address parsing → (shape, code)
  - HMAC signature verification (timestamped, INBOUND_EMAIL_SECRET)
  - the route's early-return paths (unresolved address, non-project shape)
  - the distill-input helpers (envelope render, delta staging)
  - the park+distill wiring with the clone, commit, and distill mocked:
    a non-empty delta distills; an empty (pure-quote) delta parks only.

The real clone → write → commit → push (git + SSH) and the LLM distill are
exercised end-to-end against the deployed service, not in unit tests; here
those seams are monkeypatched.
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


# ── distill-input helpers (pure, no clone) ────────────────────────────


def test_render_distill_input_prepends_envelope():
    payload = {
        "from": "Janet Noe <jnoe@infoblox.com>",
        "subject": "Re: pillars",
        "received_at": "2026-07-24T09:00:00Z",
    }
    out = email_router._render_distill_input(payload, "Let's keep preemptive as a fourth leg.")
    # The distiller keys off who/what, so From + Subject + Date lead the text.
    assert "# Email: Re: pillars" in out
    assert "# From: Janet Noe <jnoe@infoblox.com>" in out
    assert "# Date: 2026-07-24" in out  # trimmed to the date
    assert out.rstrip().endswith("Let's keep preemptive as a fourth leg.")


def test_render_distill_input_tolerates_missing_fields():
    out = email_router._render_distill_input({}, "body only")
    assert "(no subject)" in out
    assert "(unknown sender)" in out
    assert out.rstrip().endswith("body only")


def test_stage_email_delta_writes_prefixed_file(tmp_path):
    path = email_router._stage_email_delta(
        tmp_path, "ibx-5192", "<abc.def@host>", "distill me"
    )
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "distill me"
    # Distinct 'email-' prefix so it never collides with a Fathom stage file,
    # and it lives under transcripts/incoming/ like the meeting path.
    assert path.name.startswith("email-")
    assert path.parent == tmp_path / "transcripts" / "incoming"


# ── park + distill wiring (clone + distill mocked) ────────────────────


class _FakeClone:
    """Stand-in for git_ops._cloned_tenant() as a context manager."""

    def __init__(self, root):
        self._root = root

    def __enter__(self):
        return self._root

    def __exit__(self, *exc):
        return False


@pytest.fixture
def _wire(monkeypatch, tmp_path):
    """Patch the clone, config, project-dir resolver, commit, and distill so
    the handler runs park+distill without git/SSH/LLM. Returns a dict the
    tests read to assert what happened.
    """
    calls = {"distill_args": None, "commit_msg": None}

    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5192-x"
    project_dir.mkdir(parents=True)

    monkeypatch.setattr(email_router.git_ops, "_cloned_tenant", lambda: _FakeClone(tmp_path))

    class _Cfg:
        root = tmp_path

    monkeypatch.setattr(email_router, "load_config", lambda root: _Cfg())
    monkeypatch.setattr(email_router, "find_spine_dir", lambda root, code: project_dir)
    monkeypatch.setattr(
        email_router.git_ops,
        "_commit_with_message_and_push",
        lambda root, msg: calls.__setitem__("commit_msg", msg) or "deadbee",
    )

    def _fake_ingest(*, config, code, transcript_path, **kw):
        calls["distill_args"] = {"code": code, "path": str(transcript_path)}
        return {
            "plan_summary": {"record-decision": 1},
            "files_written": [str(project_dir / "sprints" / "x.md")],
            "errors": [],
        }

    monkeypatch.setattr(email_router.pipeline, "_ingest_one_project", _fake_ingest)
    return calls


def test_nonempty_delta_distills(client, _wire):
    body = {
        "to": "cp+ibx-5192@cp.firstperson.is",
        "from": "Janet <jnoe@infoblox.com>",
        "subject": "Re: pillars",
        "message_id": "<m1@host>",
        "text": "Keep preemptive as a fourth leg.\n\nOn Wed, X wrote:\n> old stuff",
    }
    raw, headers = _signed(body)
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ingested"
    assert data["distilled"] is True
    assert data["plan_summary"] == {"record-decision": 1}
    # distill actually ran against the staged delta
    assert _wire["distill_args"]["code"] == "ibx-5192"
    assert "distilled" in _wire["commit_msg"]


def test_empty_delta_parks_without_distill(client, _wire):
    # A whitespace-only body strips to an empty delta → park, no distill.
    # (A pure-quote reply deliberately fails OPEN in the stripper, keeping
    # the body, so it is NOT the empty case — see email_strip contract.)
    body = {
        "to": "cp+ibx-5192@cp.firstperson.is",
        "from": "Janet <jnoe@infoblox.com>",
        "subject": "Re: pillars",
        "message_id": "<m2@host>",
        "text": "   \n\n  \n",
    }
    raw, headers = _signed(body)
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["distilled"] is False
    assert data["status"] == "parked"
    assert _wire["distill_args"] is None  # distill skipped
    assert "parked" in _wire["commit_msg"]
