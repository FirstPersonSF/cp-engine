"""Tests for POST /api/meetings/promote-transcript (meeting transcript → RAG, async).

A fire-and-forget endpoint: the mc-2 meetings list "Promote transcript" click is
proxied here as ``{"recording_id": <int>}`` (signed). It verifies the HMAC,
fetches the fathom_meetings row by recording_id (INCLUDING the transcript jsonb
column), resolves the project (row.project_id preferred, else from project_tags),
resolves company_id via folders, returns 202 immediately, and runs the (sync,
slow) ``promote_meeting_transcript`` in a background task.

Unlike the spine promote endpoint this is simpler:
  - keyed on recording_id (int), not code+key;
  - NO tenant clone (transcript comes off the DB row, not a committed file);
  - NO runs table (fathom_meetings.transcript_promoted_at is the status signal).

Everything is mocked: no real DB, no real ingest, no network. Mirrors the
signing/mocking style of test_webhook_spine_promote_transcript.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main

from cp_engine.asset_ingest import ProjectFolders


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/meetings/promote-transcript",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


def _meeting(recording_id=4242, project_id="u-123", project_tags=None):
    return {
        "recording_id": recording_id,
        "title": "Kickoff call",
        "transcript": [{"speaker": "A", "text": "hi"}],
        "transcript_promoted_at": None,
        "project_tags": project_tags,
        "project_id": project_id,
    }


def _wire(
    monkeypatch,
    *,
    rec=None,
    meeting=None,
    project_id="u-123",
    company_id="c-1",
    resolve_from_tags=None,
):
    """Stub the HMAC secret, env creds, supabase client, the by-recording_id
    fetch, and the resolve chain (project_id from row/tags, company_id via
    folders). Returns a `rec` dict capturing spawned coros + promote calls."""
    rec = rec if rec is not None else {}
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")

    sb = object()  # opaque; the endpoint only passes it through (fetch is stubbed)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)
    rec["client"] = sb

    # by-recording_id fetch (the endpoint's new helper)
    row = meeting if meeting is not None else _meeting(project_id=project_id)
    rec["row"] = row
    monkeypatch.setattr(
        webhook_main, "_fetch_meeting_by_recording_id",
        lambda c, rid: row if (row is not None and rid == row["recording_id"]) else None,
    )

    # project_id from project_tags (fallback path)
    monkeypatch.setattr(
        "cp_engine.meetings.resolve_meeting_project",
        lambda c, tags: (resolve_from_tags, "tag") if resolve_from_tags else (None, None),
    )

    # company_id via folders
    folders = ProjectFolders(
        project_id=project_id, company_id=company_id, company_kind="client",
        google_drive_folder_id=None, mc_dropbox_folder_id=None,
        enable_google_drive=False, enable_dropbox=False,
    ) if (project_id is not None and company_id is not None) else (
        ProjectFolders(
            project_id=project_id, company_id=None, company_kind=None,
            google_drive_folder_id=None, mc_dropbox_folder_id=None,
            enable_google_drive=False, enable_dropbox=False,
        ) if project_id is not None else None
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda c, pid: folders,
    )

    # promote_meeting_transcript — captured, not run for real
    rec.setdefault("promote_calls", [])

    def _promote(c, meeting_row, pid, cid, *, supabase_url, supabase_key, **kw):
        rec["promote_calls"].append({
            "client": c, "meeting_row": meeting_row, "project_id": pid,
            "company_id": cid, "creds": (supabase_url, supabase_key),
        })
        return {"ok": True, "asset_id": "asset-99"}

    monkeypatch.setattr("cp_engine.meetings.promote_meeting_transcript", _promote)

    # capture spawned coros (run them by hand)
    rec.setdefault("spawned", [])
    monkeypatch.setattr(
        webhook_main, "_spawn_background",
        lambda coro: rec["spawned"].append(coro),
    )
    return rec


# ------------------------------------------------------------ happy path (202)


def test_promote_202_spawns_and_invokes_promote(monkeypatch, client):
    rec = _wire(monkeypatch)
    resp = _post(client, {"recording_id": 4242})
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["recording_id"] == 4242
    assert data["status"] == "running"
    assert len(rec["spawned"]) == 1

    # drive the background task → promote invoked with fetched row + resolved ids
    asyncio.run(rec["spawned"][0])
    assert len(rec["promote_calls"]) == 1
    call = rec["promote_calls"][0]
    assert call["meeting_row"] == rec["row"]
    assert call["project_id"] == "u-123"
    assert call["company_id"] == "c-1"
    assert call["creds"] == ("https://test.supabase.co", "test-service-key")


def test_promote_resolves_from_tags_when_no_project_id(monkeypatch, client):
    """Row has no project_id → fall back to resolve_meeting_project(project_tags)."""
    row = _meeting(project_id=None, project_tags=["ibx-5153"])
    rec = _wire(monkeypatch, meeting=row, project_id="u-tag", resolve_from_tags="u-tag")
    resp = _post(client, {"recording_id": 4242})
    assert resp.status_code == 202, resp.text
    asyncio.run(rec["spawned"][0])
    assert rec["promote_calls"][0]["project_id"] == "u-tag"


# ------------------------------------------------------------------- 400


def test_promote_400_when_recording_id_missing(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {})
    assert resp.status_code == 400
    assert "recording_id" in resp.text.lower()


def test_promote_400_when_recording_id_none(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"recording_id": None})
    assert resp.status_code == 400


# -------------------------------------------------------------------- 404s


def test_promote_404_when_no_meeting(monkeypatch, client):
    rec = _wire(monkeypatch)
    monkeypatch.setattr(
        webhook_main, "_fetch_meeting_by_recording_id", lambda c, rid: None
    )
    resp = _post(client, {"recording_id": 9999})
    assert resp.status_code == 404
    assert "9999" in resp.text


def test_promote_404_when_not_linked_to_project(monkeypatch, client):
    """No project_id on the row and tags don't resolve → 404."""
    row = _meeting(project_id=None, project_tags=["untagged"])
    rec = _wire(monkeypatch, meeting=row, project_id=None, resolve_from_tags=None)
    resp = _post(client, {"recording_id": 4242})
    assert resp.status_code == 404
    assert "project" in resp.text.lower()


# -------------------------------------------------------------------- 401


def test_promote_401_on_bad_signature(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"recording_id": 4242}).encode()
    resp = client.post(
        "/api/meetings/promote-transcript",
        content=body,
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_promote_401_when_signature_missing(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"recording_id": 4242}).encode()
    resp = client.post("/api/meetings/promote-transcript", content=body)
    assert resp.status_code == 401


# -------------------------------------------------------------------- 500


def test_promote_500_when_supabase_unconfigured(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: None)
    resp = _post(client, {"recording_id": 4242})
    assert resp.status_code == 500
    assert "supabase" in resp.text.lower()


# ------------------------------------------- initiative (company_id None) → 202


def test_promote_202_initiative_carries_company_none(monkeypatch, client):
    """An initiative has no company_id; the endpoint still 202s and the
    background promote is invoked with company_id=None (its own gate defers)."""
    rec = _wire(monkeypatch, project_id="u-init", company_id=None)
    resp = _post(client, {"recording_id": 4242})
    assert resp.status_code == 202, resp.text
    asyncio.run(rec["spawned"][0])
    assert len(rec["promote_calls"]) == 1
    assert rec["promote_calls"][0]["company_id"] is None


# ------------------------------- background runner: never crashes on ok:False


def test_run_meeting_promote_logs_on_not_ok(monkeypatch):
    """ok:False (e.g. initiative deferral) → runner does not raise."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")

    monkeypatch.setattr(
        "cp_engine.meetings.promote_meeting_transcript",
        lambda *a, **k: {"ok": False, "reason": "initiative promotion not yet supported"},
    )
    # Must not raise.
    asyncio.run(
        webhook_main._run_meeting_promote(object(), _meeting(), "u-init", None)
    )


def test_run_meeting_promote_logs_on_ok(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")

    monkeypatch.setattr(
        "cp_engine.meetings.promote_meeting_transcript",
        lambda *a, **k: {"ok": True, "asset_id": "a-1"},
    )
    asyncio.run(
        webhook_main._run_meeting_promote(object(), _meeting(), "u-123", "c-1")
    )
