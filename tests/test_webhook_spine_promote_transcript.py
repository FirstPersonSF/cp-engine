"""Tests for POST /api/spine/promote-transcript (spine importance → RAG, async).

A fire-and-forget endpoint mirroring /api/assets/ingest: the mc-2 spine
dashboard "Promote transcript" click lands here (signed). It resolves the
project + element, inserts a `running` spine_promote_runs row, returns 202
immediately, and runs the (sync, slow) `promote_transcript` in a background
task — recording the outcome on the run row when it finishes.

Everything is mocked: no real DB, no real ingest, no network. Mirrors the
signing/mocking style of test_webhook_asset_ingest.py.
"""
from __future__ import annotations

import asyncio
import contextlib
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
        "/api/spine/promote-transcript",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


class _RecordingTable:
    def __init__(self, rec, name):
        self._rec = rec
        self._name = name
        self._insert = None
        self._update = None
        self._eq = None

    def insert(self, data):
        self._rec["inserts"].append({"table": self._name, "data": data})
        self._insert = data
        return self

    def update(self, data):
        self._update = data
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        if self._update is not None:
            if self._rec.get("update_raises") is not None:
                raise self._rec["update_raises"]
            self._rec["updates"].append(
                {"table": self._name, "update": self._update, "eq": self._eq}
            )
        return type("Resp", (), {"data": []})()


class _RecordingClient:
    """Records the insert / update().eq().execute() chains the endpoint +
    background runner issue against spine_promote_runs."""

    def __init__(self, rec: dict):
        self._rec = rec
        rec.setdefault("inserts", [])
        rec.setdefault("updates", [])

    def table(self, name):
        return _RecordingTable(self._rec, name)


def _element(est_item_id="_authored/email-1", rel_path="meetings/m1.txt"):
    return {"est_item_id": est_item_id, "rel_path": rel_path, "label": "Email 1"}


def _install_fake_clone(monkeypatch, rec, *, root=Path("/tmp/cp")):
    """Replace _cloned_tenant with a contextmanager that yields a fixed Path and
    records enter/exit — so tests prove the clone is cleaned up (its __exit__ ran)
    without doing a real git clone. promote_transcript is mocked, so the yielded
    root is never actually read from disk."""
    rec.setdefault("clone_enters", 0)
    rec.setdefault("clone_exits", 0)

    @contextlib.contextmanager
    def _fake():
        rec["clone_enters"] += 1
        try:
            yield root
        finally:
            rec["clone_exits"] += 1

    monkeypatch.setattr(webhook_main, "_cloned_tenant", _fake)
    return rec


def _wire(
    monkeypatch,
    *,
    rec=None,
    project_id="u-123",
    company_id="c-1",
    element=None,
    spawn=True,
):
    """Stub the HMAC secret, env creds, supabase client, and the cp-engine
    resolve chain (project_id, company_id via folders, element_row)."""
    rec = rec if rec is not None else {}
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)
    rec["client"] = sb

    # Resolve project_code -> projects.id
    monkeypatch.setattr(
        webhook_main, "_resolve_project_id_for_promote",
        lambda c, code: project_id,
    )

    # Resolve project_id -> folders (company_id lives here; None for initiatives)
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

    # Resolve element_row
    el = element if element is not None else _element()
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda c, pid, key: el,
    )

    # tenant clone — a fake contextmanager yielding a fixed Path (never read,
    # since promote_transcript is mocked); records enter/exit for cleanup proof.
    _install_fake_clone(monkeypatch, rec)

    rec.setdefault("spawned", [])
    monkeypatch.setattr(
        webhook_main, "_spawn_background",
        lambda coro: (rec["spawned"].append(coro), coro.close())[0],
    )
    return rec


# ------------------------------------------------------------ happy path (202)


def test_promote_202_inserts_running_and_spawns(monkeypatch, client):
    rec = _wire(monkeypatch)
    resp = _post(client, {"code": "ibx-5153", "key": "_authored/email-1"})
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "running"
    run_id = data["run_id"]
    assert run_id.startswith("ibx-5153/")

    runs = [i for i in rec["inserts"] if i["table"] == "spine_promote_runs"]
    assert len(runs) == 1
    inserted = runs[0]["data"]
    assert inserted["id"] == run_id
    assert inserted["project_code"] == "ibx-5153"
    assert inserted["project_id"] == "u-123"
    assert inserted["est_item_id"] == "_authored/email-1"
    assert inserted["status"] == "running"
    assert "started_at" in inserted

    assert len(rec["spawned"]) == 1


# ------------------------------------------------------------------- 400s


def test_promote_400_when_code_missing(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"key": "_authored/email-1"})
    assert resp.status_code == 400
    assert "code" in resp.text.lower()


def test_promote_400_when_key_missing(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"code": "ibx-5153"})
    assert resp.status_code == 400
    assert "key" in resp.text.lower()


def test_promote_400_when_code_blank(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"code": "   ", "key": "k"})
    assert resp.status_code == 400


# -------------------------------------------------------------------- 404s


def test_promote_404_when_project_unresolved(monkeypatch, client):
    rec = _wire(monkeypatch, project_id=None)
    resp = _post(client, {"code": "nope", "key": "k"})
    assert resp.status_code == 404
    assert "nope" in resp.text.lower() or "project" in resp.text.lower()


def test_promote_404_when_no_element(monkeypatch, client):
    rec = _wire(monkeypatch)
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda c, pid, key: None,
    )
    resp = _post(client, {"code": "ibx-5153", "key": "missing"})
    assert resp.status_code == 404


# -------------------------------------------------------------------- 401


def test_promote_401_on_bad_signature(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"code": "ibx-5153", "key": "k"}).encode()
    resp = client.post(
        "/api/spine/promote-transcript",
        content=body,
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_promote_401_when_signature_missing(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"code": "ibx-5153", "key": "k"}).encode()
    resp = client.post("/api/spine/promote-transcript", content=body)
    assert resp.status_code == 401


# -------------------------------------------------------------------- 500


def test_promote_500_when_supabase_unconfigured(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: None)
    resp = _post(client, {"code": "ibx-5153", "key": "k"})
    assert resp.status_code == 500
    assert "supabase" in resp.text.lower()


# ------------------------------------------------- 202 then drive the coroutine


def test_promote_202_runner_records_done_on_ok(monkeypatch, client):
    """Capture the spawned coro, run it, and assert promote_transcript was called
    with (client, tenant_root, code, project_id, company_id, element_row, creds)
    and the run row flipped to done with asset_id set."""
    rec = _wire(monkeypatch)
    el = _element()

    spawned = {}
    monkeypatch.setattr(
        webhook_main, "_spawn_background",
        lambda coro: spawned.__setitem__("coro", coro),
    )

    captured = {}

    def _capture(client, tenant_root, code, project_id, company_id, element_row,
                 *, supabase_url, supabase_key):
        captured["args"] = (client, tenant_root, code, project_id, company_id,
                            element_row)
        captured["creds"] = (supabase_url, supabase_key)
        return {"ok": True, "title": "Email 1", "ids": ["asset-99"]}

    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript", _capture
    )

    resp = _post(client, {"code": "ibx-5153", "key": "_authored/email-1"})
    assert resp.status_code == 202, resp.text

    asyncio.run(spawned["coro"])

    cl, tenant_root, code, project_id, company_id, element_row = captured["args"]
    assert code == "ibx-5153"
    assert project_id == "u-123"
    assert company_id == "c-1"
    assert element_row == el
    assert tenant_root == Path("/tmp/cp")
    assert captured["creds"] == (
        "https://test.supabase.co", "test-service-key"
    )

    patch = [u for u in rec["updates"]
             if u["table"] == "spine_promote_runs"][0]["update"]
    assert patch["status"] == "done"
    assert patch["asset_id"] == "asset-99"
    assert "finished_at" in patch


def test_runner_records_failed_on_not_ok(monkeypatch):
    """promote_transcript returns ok:false → run row failed with reason."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    _install_fake_clone(monkeypatch, rec)
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)

    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript",
        lambda *a, **k: {"ok": False, "reason": "transcript file not found"},
    )

    asyncio.run(
        webhook_main._run_promote(
            "ibx-5153/run-1", "ibx-5153", "u-123", "c-1", _element()
        )
    )

    patch = [u for u in rec["updates"]
             if u["table"] == "spine_promote_runs"][0]["update"]
    assert patch["status"] == "failed"
    assert "transcript file not found" in patch["error"]
    assert "finished_at" in patch
    assert [u for u in rec["updates"]][0]["eq"] == ("id", "ibx-5153/run-1")


def test_runner_engagement_only_carries_through_as_failed(monkeypatch):
    """company_id=None (an initiative) is passed through to promote_transcript,
    which returns ok:false with an 'initiative' reason → run row failed with
    that reason (NOT a crash)."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    _install_fake_clone(monkeypatch, rec)
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)

    captured = {}

    def _capture(client, tenant_root, code, project_id, company_id, element_row,
                 *, supabase_url, supabase_key):
        captured["company_id"] = company_id
        # mirror promote_transcript's CONTRACT A return for an initiative
        return {"ok": False, "reason": "initiative promotion not yet supported"}

    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript", _capture
    )

    asyncio.run(
        webhook_main._run_promote(
            "storyos/run-1", "storyos", "u-init", None, _element()
        )
    )

    assert captured["company_id"] is None
    patch = [u for u in rec["updates"]
             if u["table"] == "spine_promote_runs"][0]["update"]
    assert patch["status"] == "failed"
    assert "initiative" in patch["error"]


def test_runner_records_failed_on_exception(monkeypatch):
    """A raising promote_transcript is recorded as failed; never crashes."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    _install_fake_clone(monkeypatch, rec)
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)

    def boom(*a, **k):
        raise RuntimeError("promote exploded")
    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript", boom
    )

    asyncio.run(
        webhook_main._run_promote(
            "ibx-5153/run-1", "ibx-5153", "u-123", "c-1", _element()
        )
    )

    patch = [u for u in rec["updates"]
             if u["table"] == "spine_promote_runs"][0]["update"]
    assert patch["status"] == "failed"
    assert "promote exploded" in patch["error"]
    assert "finished_at" in patch


def test_runner_done_guards_empty_ids(monkeypatch):
    """ok:true with empty ids → done but asset_id None (no IndexError)."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    _install_fake_clone(monkeypatch, rec)
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)

    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript",
        lambda *a, **k: {"ok": True, "title": "t", "ids": []},
    )

    asyncio.run(
        webhook_main._run_promote(
            "ibx-5153/run-1", "ibx-5153", "u-123", "c-1", _element()
        )
    )

    patch = [u for u in rec["updates"]
             if u["table"] == "spine_promote_runs"][0]["update"]
    assert patch["status"] == "done"
    assert patch["asset_id"] is None


# ----------------------------------------------- clone lifecycle (no disk leak)


def test_runner_cleans_up_clone_on_ok(monkeypatch):
    """The promotion is driven through _cloned_tenant (the shared contextmanager
    whose finally: rmtree guarantees cleanup). Prove the clone is ENTERED and
    EXITED for a successful promote — i.e. no per-promote disk leak."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    _install_fake_clone(monkeypatch, rec)
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)

    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript",
        lambda *a, **k: {"ok": True, "title": "t", "ids": ["a-1"]},
    )

    asyncio.run(
        webhook_main._run_promote(
            "ibx-5153/run-1", "ibx-5153", "u-123", "c-1", _element()
        )
    )

    assert rec["clone_enters"] == 1
    assert rec["clone_exits"] == 1  # __exit__/finally ran → clone removed


def test_runner_cleans_up_clone_when_promote_raises(monkeypatch):
    """Even when promote_transcript raises, the `with _cloned_tenant()` exit runs
    (finally: rmtree) — the clone is removed on the failure path too."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    _install_fake_clone(monkeypatch, rec)
    sb = _RecordingClient(rec)
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)

    def boom(*a, **k):
        raise RuntimeError("promote exploded")
    monkeypatch.setattr("cp_engine.spine_promote.promote_transcript", boom)

    asyncio.run(
        webhook_main._run_promote(
            "ibx-5153/run-1", "ibx-5153", "u-123", "c-1", _element()
        )
    )

    assert rec["clone_enters"] == 1
    assert rec["clone_exits"] == 1
    # And the failure was still recorded (never-raise contract intact).
    patch = [u for u in rec["updates"]
             if u["table"] == "spine_promote_runs"][0]["update"]
    assert patch["status"] == "failed"
