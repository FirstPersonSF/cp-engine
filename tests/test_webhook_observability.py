"""Tests for webhook observability (arch-phase-3, issue #28).

Covers the correlation-id plumbing (contextvar, middleware header
round-trip, commit-message trailer, auto_ingest_runs row with the
pre-migration column-tolerant retry), the Sentry no-op-without-DSN
contract, and the self-heal hook consolidation (plugin hook defers to
the tenant pin inside a tenant).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main
import git_ops
import observability
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_cid():
    observability.correlation_id.set(None)
    yield
    observability.correlation_id.set(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


# ──────────────────────────────────────────────────────────────────────
#  Correlation id primitives
# ──────────────────────────────────────────────────────────────────────


def test_new_correlation_id_generates_short_hex() -> None:
    cid = observability.new_correlation_id()
    assert len(cid) == 12
    int(cid, 16)  # hex or raise
    assert observability.current_correlation_id() == cid


def test_new_correlation_id_honors_incoming() -> None:
    assert observability.new_correlation_id("  fathom-abc123  ") == "fathom-abc123"


def test_new_correlation_id_caps_hostile_header() -> None:
    cid = observability.new_correlation_id("x" * 500)
    assert len(cid) == 64


def test_filter_defaults_cid_to_dash() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", (), None)
    assert observability.CorrelationIdFilter().filter(record) is True
    assert record.cid == "-"


def test_filter_stamps_active_cid() -> None:
    observability.new_correlation_id("abc")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", (), None)
    observability.CorrelationIdFilter().filter(record)
    assert record.cid == "abc"


# ──────────────────────────────────────────────────────────────────────
#  Sentry: strict no-op without DSN
# ──────────────────────────────────────────────────────────────────────


def test_init_sentry_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(observability, "_sentry_enabled", False)
    assert observability.init_sentry(release="0.0.0") is False
    assert observability.sentry_enabled() is False


def test_capture_never_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_sentry_enabled", False)
    observability.capture(RuntimeError("boom"), area="test")  # must not raise


# ──────────────────────────────────────────────────────────────────────
#  Middleware header round-trip
# ──────────────────────────────────────────────────────────────────────


def test_health_response_carries_correlation_id(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    cid = resp.headers.get("x-correlation-id")
    assert cid and len(cid) == 12


def test_incoming_correlation_id_is_echoed(client: TestClient) -> None:
    resp = client.get("/health", headers={"X-Correlation-ID": "fathom-42"})
    assert resp.headers.get("x-correlation-id") == "fathom-42"


# ──────────────────────────────────────────────────────────────────────
#  Commit-message trailer
# ──────────────────────────────────────────────────────────────────────


def test_commit_message_carries_correlation_trailer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def fake_push(tenant_root: Path, message: str) -> str:
        captured["message"] = message
        return "deadbeef"

    monkeypatch.setattr(git_ops, "_commit_with_message_and_push", fake_push)
    observability.new_correlation_id("cid-777")
    sha = webhook_main._commit_and_push(
        tenant_root=tmp_path,
        meeting_id="12345678-abcd",
        ingested=[{"code": "ggl-5168", "files_written": ["f"], "plan_summary": {}}],
    )
    assert sha == "deadbeef"
    assert "Correlation-Id: cid-777" in captured["message"]


def test_commit_message_no_trailer_outside_request_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}
    monkeypatch.setattr(git_ops, "_commit_with_message_and_push",
        lambda root, message: captured.setdefault("message", message) and "sha" or "sha",
    )
    webhook_main._commit_and_push(
        tenant_root=tmp_path,
        meeting_id="12345678-abcd",
        ingested=[{"code": "ggl-5168", "files_written": ["f"], "plan_summary": {}}],
    )
    assert "Correlation-Id" not in captured["message"]


# ──────────────────────────────────────────────────────────────────────
#  auto_ingest_runs row: cid included, column-tolerant pre-migration
# ──────────────────────────────────────────────────────────────────────


def _fake_supabase(insert_effects: list) -> tuple[MagicMock, list]:
    """Client whose .table().insert(row).execute() pops insert_effects:
    an Exception instance raises, anything else succeeds. Returns
    (client, rows) where rows collects the payload of every attempt."""
    rows: list = []
    client = MagicMock()

    def _insert(row):
        rows.append(dict(row))  # copy — the retry path mutates the dict in place
        m = MagicMock()

        def _execute():
            effect = insert_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return MagicMock()

        m.execute = _execute
        return m

    client.table.return_value.insert.side_effect = _insert
    return client, rows


def test_run_row_includes_correlation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, rows = _fake_supabase([None])
    monkeypatch.setattr(webhook_main.mc2_db, "get_client", lambda **kw: client)
    observability.new_correlation_id("cid-abc")
    webhook_main._log_run_to_supabase(
        meeting_id="m1", project_codes=["ggl-5168"], status="success",
        ingested=[], commit_sha="sha",
    )
    assert rows[0]["correlation_id"] == "cid-abc"


def test_run_row_retries_without_unknown_column(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Until the correlation_id migration lands, a PostgREST unknown-column
    error must not cost the run row: retry without the field."""
    client, rows = _fake_supabase(
        [Exception("column \"correlation_id\" of relation does not exist"), None]
    )
    monkeypatch.setattr(webhook_main.mc2_db, "get_client", lambda **kw: client)
    observability.new_correlation_id("cid-abc")
    webhook_main._log_run_to_supabase(
        meeting_id="m1", project_codes=["ggl-5168"], status="success",
        ingested=[], commit_sha="sha",
    )
    assert len(rows) == 2
    assert "correlation_id" in rows[0]
    assert "correlation_id" not in rows[1]


def test_run_row_other_errors_still_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, rows = _fake_supabase([Exception("network down")])
    monkeypatch.setattr(webhook_main.mc2_db, "get_client", lambda **kw: client)
    with caplog.at_level(logging.WARNING):
        webhook_main._log_run_to_supabase(
            meeting_id="m1", project_codes=["x"], status="failed",
            ingested=[], commit_sha=None,
        )  # must not raise
    assert any("auto_ingest_runs insert failed" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────
#  Self-heal consolidation: plugin hook defers to the tenant pin
# ──────────────────────────────────────────────────────────────────────

_PLUGIN_HOOK = (
    Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "sync-cli-version.sh"
)


def _run_hook(cwd: Path, plugin_root: Path, extra_path: Path | None = None):
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin_root))
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(_PLUGIN_HOOK)],
        cwd=cwd, env=env, capture_output=True, text=True, input="{}", timeout=30,
    )


def _plugin_root(tmp_path: Path, version: str) -> Path:
    root = tmp_path / "plugin-root"
    root.mkdir(exist_ok=True)
    (root / "plugin.json").write_text(json.dumps({"version": version}))
    return root


def test_plugin_hook_defers_inside_tenant(tmp_path: Path) -> None:
    """Inside a tenant (.cp-engine.toml anywhere up the tree), the plugin
    hook must exit 0 SILENTLY without touching the CLI — the tenant pin
    hook owns the decision. Plugin version deliberately absurd: any
    version comparison would print/attempt an install."""
    tenant = tmp_path / "tenant" / "sub" / "dir"
    tenant.mkdir(parents=True)
    (tmp_path / "tenant" / ".cp-engine.toml").write_text("[engine]\n")
    out = _run_hook(tenant, _plugin_root(tmp_path, "99.99.99"))
    assert out.returncode == 0
    assert out.stdout.strip() == ""
    assert out.stderr.strip() == ""


def test_plugin_hook_still_checks_outside_tenant(tmp_path: Path) -> None:
    """Outside a tenant the plugin-version check must still run: with a
    stubbed `cxp` matching plugin.json it exits 0 silently (happy path)."""
    workdir = tmp_path / "not-a-tenant"
    workdir.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "cxp"
    stub.write_text("#!/usr/bin/env bash\necho 'cxp, version 1.2.3'\n")
    stub.chmod(0o755)
    out = _run_hook(workdir, _plugin_root(tmp_path, "1.2.3"), extra_path=bindir)
    assert out.returncode == 0
    assert out.stdout.strip() == ""


def test_plugin_hook_reports_drift_outside_tenant(tmp_path: Path) -> None:
    """Outside a tenant with drifted versions it must announce the update
    attempt (we don't let it actually install: PATH has no uv)."""
    workdir = tmp_path / "not-a-tenant"
    workdir.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "cxp"
    stub.write_text("#!/usr/bin/env bash\necho 'cxp, version 0.0.1'\n")
    stub.chmod(0o755)
    # Constrain PATH so `uv` is absent — the hook prints its manual-install
    # advice and exits 0 rather than mutating the real uv tool env.
    env = dict(
        os.environ,
        CLAUDE_PLUGIN_ROOT=str(_plugin_root(tmp_path, "9.9.9")),
        PATH=f"{bindir}:/usr/bin:/bin",
    )
    out = subprocess.run(
        ["bash", str(_PLUGIN_HOOK)],
        cwd=workdir, env=env, capture_output=True, text=True, input="{}", timeout=30,
    )
    assert out.returncode == 0
    assert "version drift" in out.stdout
