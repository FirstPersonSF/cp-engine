"""Tests for POST /clickup-task-closed (Task 1.7, v0.12.1 refactor).

ClickUp's actual webhook payload doesn't include the task description.
It carries `task_id` + `history_items` (with before/after status). We
look up the cp_ask_hash + project code via clickup_task_proposals
(populated by the dashboard at task-creation time).

Round-trip is idempotent: running the close twice flips an open ask once
or no-ops (already closed).
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main  # the module is `main.py`


# ─────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _signed_post(body: dict, *, secret: bytes = b"test-secret") -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return raw, sig


def _closed_payload(task_id: str = "abc123") -> dict:
    """A `taskStatusUpdated` event where the new status is in the
    closed family (`after.type == "closed"`)."""
    return {
        "event": "taskStatusUpdated",
        "task_id": task_id,
        "webhook_id": "wh1",
        "history_items": [
            {
                "field": "status",
                "before": {"status": "in progress", "type": "open"},
                "after": {"status": "complete", "type": "closed"},
            }
        ],
    }


def _open_payload(task_id: str = "abc123") -> dict:
    """Status changed but the new status is NOT closed."""
    return {
        "event": "taskStatusUpdated",
        "task_id": task_id,
        "webhook_id": "wh1",
        "history_items": [
            {
                "field": "status",
                "before": {"status": "open", "type": "open"},
                "after": {"status": "in progress", "type": "open"},
            }
        ],
    }


# ─────────────────────────────────────────────────────────────────────
#  Test A — happy path: valid signed payload → lookup → close-ask → 200
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_with_valid_payload_runs_close_ask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    """End-to-end: ClickUp closes a known task. Webhook looks up the
    cp_ask_hash + code via the proposal row, plans+ingests close-ask,
    commits, returns 200 with code+commit_sha+ingested=True."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main,
        "_lookup_proposal_by_clickup_task_id",
        lambda tid: ("abc12345", "ggl-5168"),
    )

    captured: dict = {}

    def _fake_commit(*, tenant_root: Path, code: str, cp_hash: str) -> str:
        captured.update(code=code, cp_hash=cp_hash, tenant_root=tenant_root)
        return "deadbeef"

    monkeypatch.setattr(webhook_main, "_commit_clickup_close", _fake_commit)

    captured_plan: dict = {}

    def _fake_execute(plan, **kw):
        captured_plan["plan"] = plan
        return type(
            "R", (), {"files_written": [tmp_path / "fake.md"], "errors": []}
        )()

    monkeypatch.setattr(webhook_main, "execute_plan", _fake_execute)

    @contextlib.contextmanager
    def _fake_clone():
        yield tmp_path

    monkeypatch.setattr(webhook_main, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(
        webhook_main, "_load_tenant_config", lambda root: MagicMock(root=root)
    )

    body, sig = _signed_post(_closed_payload(task_id="abc123"))
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "abc123"
    assert data["matched_hash"] == "abc12345"
    assert data["code"] == "ggl-5168"
    assert data["commit_sha"] == "deadbeef"
    assert data["ingested"] is True
    assert captured["code"] == "ggl-5168"
    assert captured["cp_hash"] == "abc12345"
    # Plan shape: close-ask under the right project, with closed_by=clickup
    # and a match on the hash marker.
    proj_block = captured_plan["plan"]["projects"]["ggl-5168"]
    assert "close-ask" in proj_block
    item = proj_block["close-ask"][0]
    assert item["closed_by"] == "clickup"
    assert item["match"] == "<!-- cp:hash=abc12345 -->"


# ─────────────────────────────────────────────────────────────────────
#  Test B — non-closed status change: 204 (no work done)
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_non_closed_status_returns_204(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`taskStatusUpdated` fires on ANY status change. If the new status
    isn't in the `closed` family, silently ignore — return 204, no lookup,
    no ingest."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    body, sig = _signed_post(_open_payload())
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 204


# ─────────────────────────────────────────────────────────────────────
#  Test C — missing task_id: 204
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_missing_task_id_returns_204(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A malformed payload without `task_id` is silently ignored."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    body, sig = _signed_post({"event": "x", "history_items": []})
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 204


# ─────────────────────────────────────────────────────────────────────
#  Test D — invalid HMAC: 401
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_invalid_signature_returns_401(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A bad signature aborts the request before any parsing or lookup."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    body = json.dumps(_closed_payload()).encode()
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": "0000", "content-type": "application/json"},
    )
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────
#  Test E — missing CLICKUP_WEBHOOK_SECRET: 500
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_missing_secret_returns_500(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The webhook must refuse to verify signatures when its dedicated
    secret env var is not configured. Distinct from the Fathom webhook's
    `WEBHOOK_HMAC_SECRET` so they can rotate independently."""
    monkeypatch.delenv("CLICKUP_WEBHOOK_SECRET", raising=False)
    resp = client.post(
        "/clickup-task-closed",
        content=b'{"x":1}',
        headers={"x-signature": "x"},
    )
    assert resp.status_code == 500


# ─────────────────────────────────────────────────────────────────────
#  Test F — task_id present but no proposal row (orphan): 200, not ingested
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_orphan_task_id_returns_200_not_ingested(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Valid closed payload, but no `clickup_task_proposals` row owns
    this `task_id` (non-cp-sourced task, or row was hand-deleted).
    Don't crash; return 200 with ingested=False and a reason. Lookup
    must be best-effort."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main,
        "_lookup_proposal_by_clickup_task_id",
        lambda tid: None,
    )
    body, sig = _signed_post(_closed_payload(task_id="orphan"))
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "orphan"
    assert data["ingested"] is False
    assert data["reason"] == "no_proposal_row"


# ─────────────────────────────────────────────────────────────────────
#  Test G — execute_plan writes no files (already closed): 200 not ingested
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_no_changes_returns_200_not_ingested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    """When the cp ask was already closed (or the hash marker no longer
    matches any open bullet), execute_plan writes no files. Don't crash;
    return 200 with ingested=False + reason=no_change so the ClickUp end
    can stop retrying."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main,
        "_lookup_proposal_by_clickup_task_id",
        lambda tid: ("feedface", "ggl-5168"),
    )
    monkeypatch.setattr(
        webhook_main,
        "execute_plan",
        lambda plan, **kw: type(
            "R", (), {"files_written": [], "errors": []}
        )(),
    )

    @contextlib.contextmanager
    def _fake_clone():
        yield tmp_path

    monkeypatch.setattr(webhook_main, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(
        webhook_main, "_load_tenant_config", lambda root: MagicMock(root=root)
    )

    body, sig = _signed_post(_closed_payload())
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ingested"] is False
    assert data["reason"] == "no_change"
