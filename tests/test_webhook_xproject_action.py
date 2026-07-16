"""Webhook handling of cross-project proposal decisions (#88):
`_run_xproject_action`'s accept/dismiss paths, with the MC-2 store and
git/tenant plumbing monkeypatched. Import shim mirrors
tests/test_webhook_slack_action.py."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from routers import slack as slack_router  # noqa: E402


def _proposal(**over) -> dict:
    base = {
        "id": "prop-1",
        "meeting_id": "0123456789abcdef",
        "source_code": "ibx-5192",
        "target_code": "sap-5174",
        "verb": "decisions",
        "text": "Customer sessions on the 28th",
        "payload": {"text": "Customer sessions on the 28th", "date": "2026-07-15"},
        "confidence": "high",
        "status": "pending",
        "cp_hash": "deadbeef",
    }
    base.update(over)
    return base


def _wire(monkeypatch, *, proposal, exec_result=None, commit_sha="c0ffee12"):
    """Stub MC-2 store, tenant clone, execute_plan, and commit+push."""
    import cp_engine.cross_project as xp

    decisions: list[tuple] = []
    monkeypatch.setattr(xp, "get_by_hash", lambda client, h: proposal)
    monkeypatch.setattr(
        xp, "decide",
        lambda client, pid, status, routed_commit_sha=None:
            decisions.append((pid, status, routed_commit_sha)),
    )
    monkeypatch.setattr(
        slack_router.pipeline, "_create_supabase_client", lambda: MagicMock()
    )

    @contextmanager
    def _fake_tenant():
        yield Path("/tmp/fake-tenant")

    monkeypatch.setattr(slack_router.git_ops, "_cloned_tenant", _fake_tenant)

    executed: list[dict] = []

    def _fake_execute(plan, **kwargs):
        executed.append(plan)
        if exec_result is not None:
            return exec_result
        result = MagicMock()
        result.files_written = [Path("sprints/2026-W29/sap-5174.md")]
        result.errors = []
        result.skipped_duplicate = 0
        return result

    monkeypatch.setattr(slack_router, "execute_plan", _fake_execute)
    monkeypatch.setattr(
        slack_router.git_ops, "_commit_and_push",
        lambda **kwargs: commit_sha,
    )
    return decisions, executed


def test_accept_routes_item_and_stamps_row(monkeypatch):
    decisions, executed = _wire(monkeypatch, proposal=_proposal())
    result = slack_router._run_xproject_action(
        verb="xproj-accept", target_code="sap-5174", cp_hash="deadbeef"
    )
    assert result["committed"] is True
    assert result["commit_sha"] == "c0ffee12"
    # The routed plan targets the proposal's project with provenance text.
    items = executed[0]["projects"]["sap-5174"]["decisions"]
    assert items[0]["text"] == (
        "Customer sessions on the 28th "
        "[cross-routed from ibx-5192 · meeting 01234567]"
    )
    assert decisions == [("prop-1", "accepted", "c0ffee12")]


def test_accept_duplicate_bullet_still_settles_row(monkeypatch):
    dup = MagicMock()
    dup.files_written = []
    dup.errors = []
    dup.skipped_duplicate = 1
    decisions, _ = _wire(monkeypatch, proposal=_proposal(), exec_result=dup)
    result = slack_router._run_xproject_action(
        verb="xproj-accept", target_code="sap-5174", cp_hash="deadbeef"
    )
    assert result["accepted"] is True
    assert result["committed"] is False
    assert decisions == [("prop-1", "accepted", None)]


def test_accept_failure_keeps_row_pending(monkeypatch):
    boom = MagicMock()
    boom.files_written = []
    boom.errors = ["unknown project code"]
    boom.skipped_duplicate = 0
    decisions, _ = _wire(monkeypatch, proposal=_proposal(), exec_result=boom)
    result = slack_router._run_xproject_action(
        verb="xproj-accept", target_code="sap-5174", cp_hash="deadbeef"
    )
    assert result["committed"] is False
    assert result["errors"] == ["unknown project code"]
    assert decisions == []  # still pending — retryable


def test_dismiss_stamps_without_cloning(monkeypatch):
    decisions, executed = _wire(monkeypatch, proposal=_proposal())
    result = slack_router._run_xproject_action(
        verb="xproj-dismiss", target_code="sap-5174", cp_hash="deadbeef"
    )
    assert result["dismissed"] is True
    assert executed == []  # no tenant work on dismiss
    assert decisions == [("prop-1", "dismissed", None)]


def test_already_decided_is_noop(monkeypatch):
    decisions, executed = _wire(
        monkeypatch, proposal=_proposal(status="accepted")
    )
    result = slack_router._run_xproject_action(
        verb="xproj-accept", target_code="sap-5174", cp_hash="deadbeef"
    )
    assert result["already"] == "accepted"
    assert decisions == []
    assert executed == []


def test_missing_proposal_reports_error(monkeypatch):
    import cp_engine.cross_project as xp

    monkeypatch.setattr(xp, "get_by_hash", lambda client, h: None)
    monkeypatch.setattr(
        slack_router.pipeline, "_create_supabase_client", lambda: MagicMock()
    )
    result = slack_router._run_xproject_action(
        verb="xproj-accept", target_code="sap-5174", cp_hash="deadbeef"
    )
    assert "not found" in result["errors"][0]


def test_confirmation_text_variants():
    f = slack_router._xproject_confirmation_text
    ok = f(verb="xproj-accept", target_code="sap-5174",
           result={"accepted": True, "committed": True,
                   "commit_sha": "c0ffee1234", "errors": []})
    assert "Routed to `sap-5174`" in ok and "c0ffee12" in ok
    dup = f(verb="xproj-accept", target_code="sap-5174",
            result={"accepted": True, "committed": False,
                    "commit_sha": None, "errors": []})
    assert "already in sprint file" in dup
    dis = f(verb="xproj-dismiss", target_code="sap-5174",
            result={"dismissed": True, "errors": []})
    assert "Dismissed" in dis
    already = f(verb="xproj-accept", target_code="sap-5174",
                result={"already": "dismissed", "errors": []})
    assert "Already dismissed" in already
    fail = f(verb="xproj-accept", target_code="sap-5174",
             result={"errors": ["boom"], "committed": False})
    assert "failed" in fail
