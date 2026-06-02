"""Tests for Slack-action spawn + completion structured logs (v0.15.2 Fix 4 — I6).

The /slack-action endpoint dispatches the slow path (clone + plan +
push + Slack update) to a background asyncio task. If Railway
restarts mid-task (deploy/OOM), the user sees the click ack'd but
the tenant never updates. There's no automatic recovery in v0.15.2;
the lighter-touch fix is structured logging so postmortem can find
orphaned spawns and replay them by hand.

v0.16 may move to a slack_action_intents table + recovery sweep.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main


def test_block_action_logs_spawn_with_correlation_fields(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A button click logs `slack_action_spawn` with code, verb, hash,
    action_id, user — the postmortem-replay primary key."""
    payload = {
        "type": "block_actions",
        "user": {"id": "U999"},
        "trigger_id": "trig",
        "response_url": "https://hooks.slack/x",
        "actions": [{
            "value": "resolve-risk|ggl-5168|abc12345",
            "action_id": "resolve-risk_ggl-5168_abc12345",
        }],
        "message": {},
    }
    # Don't actually spawn the background coroutine.
    monkeypatch.setattr(
        webhook_main, "_spawn_background", lambda coro: coro.close()
    )

    with caplog.at_level(logging.INFO, logger="cp-engine-webhook"):
        asyncio.run(webhook_main._handle_block_action(payload))

    matching = [r for r in caplog.records if "slack_action_spawn" in r.message]
    assert matching, "expected slack_action_spawn log"
    msg = matching[0].message
    assert "code=ggl-5168" in msg
    assert "verb=resolve-risk" in msg
    assert "hash=abc12345" in msg
    assert "action_id=resolve-risk_ggl-5168_abc12345" in msg
    assert "user=U999" in msg


def test_view_submission_logs_spawn(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The date-picker modal submission path also emits slack_action_spawn
    so we can find it in postmortem logs without remembering which code
    path the click took."""
    import json
    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "snooze_until_modal",
            "private_metadata": json.dumps({
                "verb": "snooze-ask",
                "code": "ggl-5168",
                "hash": "deadbeef",
                "response_url": "https://hooks.slack/y",
            }),
            "state": {"values": {
                "date_block": {"until_date": {"selected_date": "2026-06-10"}}
            }},
        },
    }
    monkeypatch.setattr(
        webhook_main, "_spawn_background", lambda coro: coro.close()
    )

    with caplog.at_level(logging.INFO, logger="cp-engine-webhook"):
        asyncio.run(webhook_main._handle_view_submission(payload))

    matching = [r for r in caplog.records if "slack_action_spawn" in r.message]
    assert matching, "expected slack_action_spawn log from view_submission"
    msg = matching[0].message
    assert "code=ggl-5168" in msg
    assert "verb=snooze-ask" in msg
    assert "hash=deadbeef" in msg
    assert "until=2026-06-10" in msg


def test_run_action_logs_completion_success(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """After the plan runs, log `slack_action_complete` with committed,
    commit_sha (short), and errors count — enough to correlate with the
    spawn line and decide whether to replay."""
    monkeypatch.setattr(
        webhook_main,
        "_run_plan_for_one_item",
        lambda **kw: {
            "committed": True,
            "commit_sha": "deadbeef12345678",
            "errors": [],
        },
    )
    # _post_response_url_update writes to Slack; stub it out.
    monkeypatch.setattr(
        webhook_main, "_post_response_url_update", lambda **kw: None
    )

    with caplog.at_level(logging.INFO, logger="cp-engine-webhook"):
        asyncio.run(webhook_main._run_action_in_background(
            verb="close-ask",
            code="ggl-5168",
            cp_hash="abc12345",
            extras={"closed_by": "slack", "user": "U1"},
            response_url="https://x/y",
            original_message={},
            clicked_action_id="close-ask_ggl-5168_abc12345",
        ))

    matching = [r for r in caplog.records if "slack_action_complete" in r.message]
    assert matching, "expected slack_action_complete log"
    msg = matching[0].message
    assert "code=ggl-5168" in msg
    assert "verb=close-ask" in msg
    assert "hash=abc12345" in msg
    assert "committed=True" in msg
    assert "commit_sha=deadbeef" in msg
    assert "errors=0" in msg


def test_run_action_logs_completion_failure(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """When the plan run raises, the completion log still fires (with
    committed=False and a non-zero error count) so the spawn isn't
    silently orphaned."""

    def boom(**_kw):
        raise RuntimeError("git push failed")

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", boom)
    monkeypatch.setattr(
        webhook_main, "_post_response_url_update", lambda **kw: None
    )

    with caplog.at_level(logging.INFO, logger="cp-engine-webhook"):
        asyncio.run(webhook_main._run_action_in_background(
            verb="resolve-risk",
            code="ibx-5167",
            cp_hash="cafef00d",
            extras={"closed_by": "slack", "user": "U2"},
            response_url="https://x/y",
            original_message={},
            clicked_action_id="resolve-risk_ibx-5167_cafef00d",
        ))

    matching = [r for r in caplog.records if "slack_action_complete" in r.message]
    assert matching
    msg = matching[0].message
    assert "committed=False" in msg
    assert "errors=1" in msg
