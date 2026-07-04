"""Tests for /clickup-task-closed history_items iteration (v0.15.2 Fix 5 — I7).

ClickUp batches multiple changes per `taskStatusUpdated` event: assignee
change + status flip can arrive in the same payload, with `history_items`
ordered arbitrarily. Pre-fix, we only looked at `history_items[0]`; if the
assignee change happened to come first we silently 204'd even though the
close transition was in `history_items[1]`.

Regression: when status IS in `[0]`, behavior is unchanged.
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

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main
from routers import integrations as integrations_router
import git_ops
import pipeline


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _signed(body: dict, *, secret: bytes = b"test-secret") -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return raw, sig


def _wire_close_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub the lookup + clone + execute_plan + commit path so we can
    drive the endpoint with minimal scaffolding."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(
        integrations_router,
        "_lookup_proposal_by_clickup_task_id",
        lambda tid: ("abc12345", "ggl-5168"),
    )
    monkeypatch.setattr(git_ops, "_commit_clickup_close",
        lambda **kw: "deadbeef",
    )
    monkeypatch.setattr(integrations_router, "execute_plan",
        lambda plan, **kw: type(
            "R", (), {"files_written": [tmp_path / "fake.md"], "errors": []}
        )(),
    )

    @contextlib.contextmanager
    def _fake_clone():
        yield tmp_path

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(pipeline, "_load_tenant_config", lambda root: MagicMock(root=root)
    )


def test_status_close_in_second_history_item_is_processed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    """ClickUp packs assignee change FIRST and status close SECOND;
    pre-fix we silently 204'd. Now we iterate and process the close."""
    _wire_close_pipeline(monkeypatch, tmp_path)
    payload = {
        "event": "taskStatusUpdated",
        "task_id": "abc123",
        "history_items": [
            {
                "field": "assignee_add",
                "before": None,
                "after": {"id": "user-1"},
            },
            {
                "field": "status",
                "before": {"status": "in progress", "type": "open"},
                "after": {"status": "complete", "type": "closed"},
            },
        ],
    }
    body, sig = _signed(payload)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ingested"] is True
    assert data["commit_sha"] == "deadbeef"
    assert data["code"] == "ggl-5168"


def test_status_close_in_first_history_item_still_processed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    """Regression: the original 1-item shape (status in [0]) still works."""
    _wire_close_pipeline(monkeypatch, tmp_path)
    payload = {
        "event": "taskStatusUpdated",
        "task_id": "abc123",
        "history_items": [
            {
                "field": "status",
                "before": {"status": "in progress", "type": "open"},
                "after": {"status": "complete", "type": "closed"},
            }
        ],
    }
    body, sig = _signed(payload)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ingested"] is True


def test_only_non_status_history_items_returns_204(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """If the payload only contains non-status changes (just assignee /
    priority / etc.), we shouldn't fire the close path — 204."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    payload = {
        "event": "taskStatusUpdated",
        "task_id": "abc123",
        "history_items": [
            {"field": "assignee_add", "before": None, "after": {"id": "user-1"}},
            {"field": "priority", "before": {"priority": "normal"}, "after": {"priority": "high"}},
        ],
    }
    body, sig = _signed(payload)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 204


def test_status_change_to_non_closed_returns_204(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A status change that lands in an `open` (not `closed`) bucket
    must NOT trigger a close — 204."""
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "test-secret")
    payload = {
        "event": "taskStatusUpdated",
        "task_id": "abc123",
        "history_items": [
            {
                "field": "status",
                "before": {"status": "in progress", "type": "open"},
                "after": {"status": "review", "type": "open"},
            }
        ],
    }
    body, sig = _signed(payload)
    resp = client.post(
        "/clickup-task-closed",
        content=body,
        headers={"x-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 204
