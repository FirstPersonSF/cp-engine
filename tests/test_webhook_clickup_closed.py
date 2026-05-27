"""Tests for POST /clickup-task-closed (Task 1.7, v0.12.0 Lever 1).

When ClickUp marks a task done, ClickUp fires this webhook with the task
description in the payload. The handler extracts the `[cp:hash=<8-hex>]`
trailer the dashboard stamps in (Task 1.6), looks up which project owns
that hash via `clickup_task_proposals.cp_ask_hash`, builds a `close-ask`
plan with `closed_by: clickup`, runs ingest against a fresh clone of the
cp tenant, commits + pushes.

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
#  Shared fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _sign(body: bytes, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────────────────────────────
#  Test A — happy path: valid hash, runs close-ask, returns 200
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_with_valid_hash_returns_200(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    """End-to-end: ClickUp closes a task carrying a known cp:hash. Webhook
    extracts the hash, looks up the project, plans+ingests close-ask, commits,
    returns 200 with code+commit_sha+ingested=True."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main, "_lookup_project_for_hash", lambda h: "ggl-5168"
    )

    captured: dict = {}

    def _fake_commit(*, tenant_root: Path, code: str, cp_hash: str) -> str:
        captured.update(code=code, cp_hash=cp_hash, tenant_root=tenant_root)
        return "abc12345"

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

    body = json.dumps(
        {
            "task_id": "abc",
            "description": "Confirm ISCI code\n\n[cp:hash=2f232868]",
        }
    ).encode()
    sig = _sign(body)

    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_hash"] == "2f232868"
    assert data["code"] == "ggl-5168"
    assert data["commit_sha"] == "abc12345"
    assert data["ingested"] is True
    assert captured["code"] == "ggl-5168"
    assert captured["cp_hash"] == "2f232868"
    # Plan shape: close-ask under the right project, with closed_by=clickup
    # and a match on the hash marker.
    proj_block = captured_plan["plan"]["projects"]["ggl-5168"]
    assert "close-ask" in proj_block
    item = proj_block["close-ask"][0]
    assert item["closed_by"] == "clickup"
    assert "2f232868" in item["match"]


# ─────────────────────────────────────────────────────────────────────
#  Test B — no hash in description: 204
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_without_hash_returns_204(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A ClickUp task that wasn't sourced from cp (no `[cp:hash=...]`
    trailer) is silently ignored — return 204 No Content, no work done."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    body = json.dumps(
        {"task_id": "abc", "description": "Some task with no hash"}
    ).encode()
    sig = _sign(body)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 204


# ─────────────────────────────────────────────────────────────────────
#  Test C — invalid HMAC: 401
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_invalid_signature_returns_401(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A bad signature aborts the request before any parsing or lookup."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    body = json.dumps({"description": "X [cp:hash=abc12345]"}).encode()
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": "0000", "content-type": "application/json"},
    )
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────
#  Test D — hash present but no proposal row (orphan)
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_with_orphan_hash_logs_and_returns_200(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Hash present in description but no `clickup_task_proposals` row
    owns it (e.g., row was hand-deleted, or the proposal predates the
    hash-stamping feature). Don't crash; return 200 with ingested=False
    and a reason. Lookup must be best-effort."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(webhook_main, "_lookup_project_for_hash", lambda h: None)
    body = json.dumps({"description": "X [cp:hash=deadbeef]"}).encode()
    sig = _sign(body)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_hash"] == "deadbeef"
    assert data["ingested"] is False
    assert "reason" in data


# ─────────────────────────────────────────────────────────────────────
#  Test E — missing `CLICKUP_WEBHOOK_SECRET`: 500
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_missing_secret_returns_500(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The webhook must refuse to verify signatures when its dedicated
    secret env var is not configured. Distinct from the Fathom webhook's
    `WEBHOOK_HMAC_SECRET` so they can rotate independently."""
    monkeypatch.delenv("CLICKUP_WEBHOOK_SECRET", raising=False)
    body = b'{"description": "X"}'
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": "x"},
    )
    assert resp.status_code == 500


# ─────────────────────────────────────────────────────────────────────
#  Test F — execute_plan returns errors but no files written: 200 with ingested=False
# ─────────────────────────────────────────────────────────────────────


def test_clickup_task_closed_logs_execute_plan_errors_but_returns_200(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    """When the cp ask was already closed (or the hash trailer no longer
    matches any open bullet), execute_plan writes no files and may surface
    a soft error. Don't crash; return 200 with ingested=False so the
    ClickUp end can stop retrying."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main, "_lookup_project_for_hash", lambda h: "ggl-5168"
    )
    # _commit_clickup_close must NOT be called when nothing wrote — but if
    # the implementation does call it, returning None keeps the contract
    # honest (no commit happened).
    monkeypatch.setattr(
        webhook_main, "_commit_clickup_close", lambda **kw: None
    )
    monkeypatch.setattr(
        webhook_main,
        "execute_plan",
        lambda plan, **kw: type(
            "R", (), {"files_written": [], "errors": ["close-ask: no matching open ask"]}
        )(),
    )

    @contextlib.contextmanager
    def _fake_clone():
        yield tmp_path

    monkeypatch.setattr(webhook_main, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(
        webhook_main, "_load_tenant_config", lambda root: MagicMock(root=root)
    )

    body = json.dumps({"description": "[cp:hash=feedface]"}).encode()
    sig = _sign(body)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_hash"] == "feedface"
    # When no files were written, ingested should be False (nothing actually
    # changed on disk, so we didn't push a commit).
    assert data["ingested"] is False


# ─────────────────────────────────────────────────────────────────────
#  Test G — malformed history_items shapes must not crash
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "history_items",
    [
        [{"data": None}],  # data key present but null
        [None],            # entire history entry is null
    ],
    ids=["data_is_null", "history_entry_is_null"],
)
def test_clickup_task_closed_handles_null_history_data_gracefully(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, history_items: list
) -> None:
    """A history_items shape with data=None (or the entry itself None) must
    not crash on `.get("data", {}).get(...)`. Falls through to no-hash 204."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    body = json.dumps(
        {"task_id": "abc", "history_items": history_items}
    ).encode()
    sig = _sign(body)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-webhook-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 204
