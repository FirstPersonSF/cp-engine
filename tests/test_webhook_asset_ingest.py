"""Tests for POST /api/assets/ingest (asset-ingest button, Phase 2).

A fire-and-forget endpoint: the mc-2 "Ingest assets" click lands here (signed).
It inserts a `running` asset_ingest_runs row, returns 202 immediately, and runs
the (slow, sync) `ingest_project_assets` in a background task — recording the
outcome on the run row when it finishes. mc-2 polls a status endpoint (later).

Everything is mocked: no real DB, no real ingest, no network. Mirrors the
signing/mocking style of test_webhook_spine_promote.py.
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
import pipeline

from cp_engine.asset_ingest import IngestRunResult, ProjectFolders


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/assets/ingest",
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
    background runner issue against asset_ingest_runs."""

    def __init__(self, rec: dict):
        self._rec = rec
        rec.setdefault("inserts", [])
        rec.setdefault("updates", [])

    def table(self, name):
        return _RecordingTable(self._rec, name)


def _wire(monkeypatch, *, rec=None, project_id="u-123", spawn=True):
    rec = rec if rec is not None else {}
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)
    rec["client"] = sb

    folders = ProjectFolders(
        project_id=project_id, company_id="c-1", company_kind="client",
        google_drive_folder_id=None, mc_dropbox_folder_id=None,
        enable_google_drive=False, enable_dropbox=False,
    ) if project_id is not None else None
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda c, code: folders,
    )

    rec.setdefault("spawned", [])
    monkeypatch.setattr(pipeline, "_spawn_background",
        lambda coro: (rec["spawned"].append(coro), coro.close())[0],
    )
    return rec


# ------------------------------------------------------------ happy path (202)


def test_ingest_202_inserts_running_and_spawns(monkeypatch, client):
    rec = _wire(monkeypatch)
    resp = _post(client, {"code": "ibx-5153", "run_id": "run-1"})
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data == {"run_id": "run-1", "status": "running"}

    # A running row was inserted into asset_ingest_runs.
    runs = [i for i in rec["inserts"] if i["table"] == "asset_ingest_runs"]
    assert len(runs) == 1
    inserted = runs[0]["data"]
    assert inserted["id"] == "run-1"
    assert inserted["project_code"] == "ibx-5153"
    assert inserted["project_id"] == "u-123"
    assert inserted["status"] == "running"
    assert "started_at" in inserted

    # The background runner was spawned exactly once.
    assert len(rec["spawned"]) == 1


def test_ingest_202_threads_mc_project_id(monkeypatch, client):
    """When the body carries mc_project_id, the endpoint uses it directly for the
    run row's project_id (skipping the by-code resolve, which would fail for slug
    codes) AND threads it to the spawned runner coroutine."""
    rec = _wire(monkeypatch)

    # The by-code resolver MUST NOT be called when mc_project_id is present.
    def _should_not_resolve(c, code):  # pragma: no cover - must not be called
        raise AssertionError("by-code resolve must be skipped when mc_project_id given")
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders", _should_not_resolve
    )

    # Capture the coro spawned by the endpoint (the default _wire spawn closes
    # it; here we keep + run it so we can prove mc_project_id flows all the way
    # through the runner into ingest_project_assets).
    spawned = {}
    monkeypatch.setattr(pipeline, "_spawn_background",
        lambda coro: spawned.__setitem__("coro", coro),
    )
    captured = {}

    def _capture(code, **kwargs):
        captured["code"] = code
        captured.update(kwargs)
        return IngestRunResult(project_found=True)
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    resp = _post(
        client,
        {"code": "SAP-vision-update-2026", "run_id": "run-1",
         "mc_project_id": "proj-uuid-5174"},
    )
    assert resp.status_code == 202, resp.text

    inserted = [i for i in rec["inserts"]
                if i["table"] == "asset_ingest_runs"][0]["data"]
    # Row project_id is the authoritative mc_project_id, not a by-code resolve.
    assert inserted["project_id"] == "proj-uuid-5174"

    # Drive the spawned runner coroutine to completion and assert the threading.
    asyncio.run(spawned["coro"])
    assert captured["code"] == "SAP-vision-update-2026"
    assert captured["mc_project_id"] == "proj-uuid-5174"


def test_ingest_202_when_folders_unresolved(monkeypatch, client):
    """resolve_project_folders returning None → row enrichment project_id is
    None, but the endpoint still 202s and spawns (ingest re-resolves)."""
    rec = _wire(monkeypatch, project_id=None)
    resp = _post(client, {"code": "ibx-5153", "run_id": "run-1"})
    assert resp.status_code == 202, resp.text
    inserted = [i for i in rec["inserts"]
                if i["table"] == "asset_ingest_runs"][0]["data"]
    assert inserted["project_id"] is None
    assert len(rec["spawned"]) == 1


def test_ingest_202_when_resolve_raises(monkeypatch, client):
    """A raising resolve_project_folders is swallowed (best-effort enrichment);
    the row still inserts with project_id None and the runner still spawns."""
    rec = _wire(monkeypatch)

    def boom(c, code):
        raise RuntimeError("mc-2 down")
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders", boom
    )
    resp = _post(client, {"code": "ibx-5153", "run_id": "run-1"})
    assert resp.status_code == 202, resp.text
    inserted = [i for i in rec["inserts"]
                if i["table"] == "asset_ingest_runs"][0]["data"]
    assert inserted["project_id"] is None
    assert len(rec["spawned"]) == 1


# ------------------------------------------------------------------- 400s


def test_ingest_400_when_code_missing(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"run_id": "run-1"})
    assert resp.status_code == 400
    assert "code" in resp.text.lower()


def test_ingest_400_when_run_id_missing(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"code": "ibx-5153"})
    assert resp.status_code == 400
    assert "run_id" in resp.text.lower()


def test_ingest_400_when_code_blank(monkeypatch, client):
    _wire(monkeypatch)
    resp = _post(client, {"code": "   ", "run_id": "run-1"})
    assert resp.status_code == 400


# -------------------------------------------------------------------- 401


def test_ingest_401_on_bad_signature(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"code": "ibx-5153", "run_id": "run-1"}).encode()
    resp = client.post(
        "/api/assets/ingest",
        content=body,
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_ingest_401_when_signature_missing(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"code": "ibx-5153", "run_id": "run-1"}).encode()
    resp = client.post("/api/assets/ingest", content=body)
    assert resp.status_code == 401


# -------------------------------------------------------------------- 500


def test_ingest_500_when_supabase_unconfigured(monkeypatch, client):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: None)
    resp = _post(client, {"code": "ibx-5153", "run_id": "run-1"})
    assert resp.status_code == 500
    assert "supabase" in resp.text.lower()


# ------------------------------------------------- background runner: happy


def test_runner_records_done(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    result = IngestRunResult(
        created=3, versioned=0, skipped=32, deduped=7, failed=0, failures=[],
        source_notes=[{"source": "drive", "note": "RuntimeError: x"}],
        project_found=True,
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets",
        lambda code, **kwargs: result,
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    ups = [u for u in rec["updates"] if u["table"] == "asset_ingest_runs"]
    assert len(ups) == 1
    patch = ups[0]["update"]
    assert patch["status"] == "done"
    assert patch["created"] == 3
    assert patch["versioned"] == 0
    assert patch["skipped"] == 32
    assert patch["deduped"] == 7
    assert patch["failed"] == 0
    assert patch["failures"] == []
    assert patch["source_notes"] == [
        {"source": "drive", "note": "RuntimeError: x"}
    ]
    assert "finished_at" in patch
    assert ups[0]["eq"] == ("id", "run-1")


def test_runner_maps_failures(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    result = IngestRunResult(
        created=1, versioned=0, skipped=0, failed=2,
        failures=[("a.pdf", "boom"), ("b.docx", "kaboom")],
        source_notes=[], project_found=True,
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets",
        lambda code, **kwargs: result,
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    patch = [u for u in rec["updates"]
             if u["table"] == "asset_ingest_runs"][0]["update"]
    assert patch["status"] == "done"
    assert patch["failed"] == 2
    assert patch["failures"] == [
        {"file": "a.pdf", "error": "boom"},
        {"file": "b.docx", "error": "kaboom"},
    ]


# ------------------------------------ background runner: env creds passthrough


def test_runner_passes_env_supabase_creds_as_kwargs(monkeypatch):
    """Regression guard for the cwd-config prod bug: the runner MUST pass the
    webhook ENV's Supabase coords as supabase_url/supabase_key kwargs. Without
    them, ingest_project_assets falls into _resolve_creds() ->
    cp_config.load(Path.cwd()), which dies on the webhook container
    ('No .cp-engine.toml at /app')."""
    monkeypatch.setenv("SUPABASE_URL", "https://env-url.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "env-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    captured = {}

    def _capture(code, **kwargs):
        captured["code"] = code
        captured.update(kwargs)
        return IngestRunResult(project_found=True)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    assert captured["code"] == "ibx-5153"
    assert captured["supabase_url"] == "https://env-url.supabase.co"
    assert captured["supabase_key"] == "env-service-key"


def test_runner_threads_mc_project_id_to_ingest(monkeypatch):
    """The runner MUST pass mc_project_id through to ingest_project_assets so the
    authoritative by-id resolution is used (fixes slug-code mis-resolution)."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    captured = {}

    def _capture(code, **kwargs):
        captured["code"] = code
        captured.update(kwargs)
        return IngestRunResult(project_found=True)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    asyncio.run(
        webhook_main._run_asset_ingest(
            "run-1", "SAP-vision-update-2026", "proj-uuid-5174"
        )
    )

    assert captured["code"] == "SAP-vision-update-2026"
    assert captured["mc_project_id"] == "proj-uuid-5174"


def test_runner_threads_folder_to_only_folder(monkeypatch):
    """The runner MUST pass its folder arg through to ingest_project_assets as
    only_folder= so a targeted single-folder scan reaches the ingest."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    captured = {}

    def _capture(code, **kwargs):
        captured["code"] = code
        captured.update(kwargs)
        return IngestRunResult(project_found=True)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    asyncio.run(
        webhook_main._run_asset_ingest(
            "run-1", "ibx-5153", "proj-uuid", folder="Strategy"
        )
    )

    assert captured["only_folder"] == "Strategy"


def test_runner_threads_none_only_folder_by_default(monkeypatch):
    """Back-compat: without a folder, the runner passes only_folder=None so
    ingest_project_assets scans all folders (today's behavior)."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    captured = {}

    def _capture(code, **kwargs):
        captured.update(kwargs)
        return IngestRunResult(project_found=True)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    assert captured["only_folder"] is None


def test_ingest_202_threads_folder(monkeypatch, client):
    """When the body carries folder, the endpoint threads it through the spawned
    runner coroutine into ingest_project_assets as only_folder."""
    rec = _wire(monkeypatch)

    spawned = {}
    monkeypatch.setattr(pipeline, "_spawn_background",
        lambda coro: spawned.__setitem__("coro", coro),
    )
    captured = {}

    def _capture(code, **kwargs):
        captured.update(kwargs)
        return IngestRunResult(project_found=True)
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    resp = _post(
        client,
        {"code": "ibx-5153", "run_id": "run-1", "folder": "Strategy"},
    )
    assert resp.status_code == 202, resp.text

    asyncio.run(spawned["coro"])
    assert captured["only_folder"] == "Strategy"


def test_ingest_202_normalizes_empty_folder(monkeypatch, client):
    """folder: "" behaves like absent → only_folder=None (scan all)."""
    rec = _wire(monkeypatch)

    spawned = {}
    monkeypatch.setattr(pipeline, "_spawn_background",
        lambda coro: spawned.__setitem__("coro", coro),
    )
    captured = {}

    def _capture(code, **kwargs):
        captured.update(kwargs)
        return IngestRunResult(project_found=True)
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    resp = _post(
        client,
        {"code": "ibx-5153", "run_id": "run-1", "folder": ""},
    )
    assert resp.status_code == 202, resp.text

    asyncio.run(spawned["coro"])
    assert captured["only_folder"] is None


def test_runner_threads_none_mc_project_id_by_default(monkeypatch):
    """Back-compat: without an mc_project_id, the runner passes mc_project_id=None
    so ingest_project_assets falls back to by-code resolution (CLI behavior)."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    captured = {}

    def _capture(code, **kwargs):
        captured.update(kwargs)
        return IngestRunResult(project_found=True)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _capture
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    assert captured["mc_project_id"] is None


def test_runner_fails_fast_when_env_creds_missing(monkeypatch):
    """If the webhook env lacks SUPABASE_URL/SUPABASE_SERVICE_KEY, the runner
    records status=failed with a clear message rather than letting the deep
    cwd-config error surface (and never calls ingest_project_assets)."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    called = {"n": 0}

    def _should_not_run(code, **kwargs):
        called["n"] += 1
        return IngestRunResult(project_found=True)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _should_not_run
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    assert called["n"] == 0
    patch = [u for u in rec["updates"]
             if u["table"] == "asset_ingest_runs"][0]["update"]
    assert patch["status"] == "failed"
    assert "SUPABASE_URL" in patch["error"]
    assert "finished_at" in patch


# ------------------------------------------------- background runner: failure


def test_runner_records_failed_on_exception(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    def boom(code, **kwargs):
        raise RuntimeError("ingest exploded")
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", boom
    )

    # Must NOT raise — fire-and-forget.
    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    patch = [u for u in rec["updates"]
             if u["table"] == "asset_ingest_runs"][0]["update"]
    assert patch["status"] == "failed"
    assert "ingest exploded" in patch["error"]
    assert "finished_at" in patch
    assert [u for u in rec["updates"]][0]["eq"] == ("id", "run-1")


def test_runner_records_failed_when_project_not_found(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets",
        lambda code, **kwargs: IngestRunResult(project_found=False),
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    patch = [u for u in rec["updates"]
             if u["table"] == "asset_ingest_runs"][0]["update"]
    assert patch["status"] == "failed"
    assert "no MC-2 project" in patch["error"]
    assert "ibx-5153" in patch["error"]
    assert "finished_at" in patch


def test_runner_records_refusal_when_unconfigured(monkeypatch):
    """Confirm gate (#59): a NULL-folder project records a structured refusal
    (status=failed + actionable error), NOT a done-with-zero-counts run."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    rec = {}
    sb = _RecordingClient(rec)
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    reason = (
        "no Drive/Dropbox folder configured "
        "(drive: enabled but folder not set; dropbox: disabled)"
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets",
        lambda code, **kwargs: IngestRunResult(
            unconfigured_reason=reason,
            source_notes=[{"source": "config", "note": reason}],
        ),
    )

    asyncio.run(webhook_main._run_asset_ingest("run-1", "ibx-5153"))

    ups = [u for u in rec["updates"] if u["table"] == "asset_ingest_runs"]
    assert len(ups) == 1
    patch = ups[0]["update"]
    assert patch["status"] == "failed"
    assert "No Drive/Dropbox folder configured" in patch["error"]
    assert "ibx-5153" in patch["error"]
    assert "MC-2" in patch["error"]
    assert patch["source_notes"] == [{"source": "config", "note": reason}]
    assert "created" not in patch  # not a normal-looking empty run
    assert "finished_at" in patch
    assert ups[0]["eq"] == ("id", "run-1")
