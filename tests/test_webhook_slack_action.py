"""Tests for Slack-action endpoint surface on cp-engine-webhook (v0.14.0).

This file will grow across Tasks 3.1, 3.2, and 3.3. For now: signature
verifier (Task 3.1) + endpoint dispatch (Task 3.2).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# webhook/ is a sibling of src/; mirror the import shim from test_webhook_rerun.py.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))


@pytest.fixture
def client() -> TestClient:
    import main as webhook_main
    return TestClient(webhook_main.app)


def _signed_slack_request(
    payload: dict, secret: bytes = b"test-slack-secret"
) -> tuple[bytes, dict]:
    """Build a properly-signed Slack interactive payload + headers."""
    import urllib.parse
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(
        secret, f"v0:{ts}:".encode() + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Signature": sig,
        "X-Slack-Request-Timestamp": ts,
    }
    return body, headers


def test_slack_signature_valid(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    from main import _verify_slack_signature
    body = b'{"type":"block_actions"}'
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(
        b"test-slack-secret",
        f"v0:{ts}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    # Should not raise.
    _verify_slack_signature(body, ts, sig)


def test_slack_signature_rejects_replay(monkeypatch):
    """Slack timestamps older than 5 minutes are rejected to prevent replay."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    from main import _verify_slack_signature
    from fastapi import HTTPException
    body = b"{}"
    old_ts = str(int(time.time()) - 600)  # 10 min ago
    sig = "v0=" + hmac.new(
        b"test-slack-secret",
        f"v0:{old_ts}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(HTTPException) as exc:
        _verify_slack_signature(body, old_ts, sig)
    assert exc.value.status_code == 401


def test_slack_signature_rejects_bad_sig(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    from main import _verify_slack_signature
    from fastapi import HTTPException
    body = b"{}"
    ts = str(int(time.time()))
    with pytest.raises(HTTPException):
        _verify_slack_signature(body, ts, "v0=0000000000000000")


# ─────────────────────────────────────────────────────────────
#  Task 3.2: /slack-action endpoint dispatch
# ─────────────────────────────────────────────────────────────


def test_slack_action_resolve_risk_acks_immediately_and_queues_work(
    monkeypatch, client
):
    """A button click with value 'resolve-risk|ibx-5167|09e3d0c7':
    - Returns 200 to Slack synchronously (the 3-second ack window).
    - Schedules the actual plan run as a background asyncio task.
    - That background task calls _run_plan_for_one_item with the right args.
    """
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}

    def fake_run(*, verb, code, cp_hash, week_iso=None, **extras):
        called["verb"] = verb
        called["code"] = code
        called["hash"] = cp_hash
        called["week_iso"] = week_iso
        called["extras"] = extras
        return {"committed": True, "commit_sha": "abc123", "errors": []}

    update_calls = []

    def fake_update(*, response_url, original_message, confirmation, clicked_action_id=""):
        update_calls.append({"url": response_url, "confirmation": confirmation})

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(webhook_main, "_post_response_url_update", fake_update)

    payload = {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "actions": [{
            "action_id": "resolve-risk_ibx-5167_09e3d0c7",
            "value": "resolve-risk|ibx-5167|09e3d0c7",
            "type": "button",
        }],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {"ts": "1234.5678", "blocks": []},
        "trigger_id": "trg-abc",
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json().get("queued") is True

    # Background task should have run by now.
    for _ in range(50):
        if called:
            break
        time.sleep(0.01)

    assert called == {
        "verb": "resolve-risk",
        "code": "ibx-5167",
        "hash": "09e3d0c7",
        # 3-part payload → no week embedded → threaded as None (execute_plan
        # defaults to the current week; unchanged behavior).
        "week_iso": None,
        "extras": {"closed_by": "slack", "user": "U02CF0L7M"},
    }
    assert len(update_calls) == 1
    assert "Resolved" in update_calls[0]["confirmation"]


def test_slack_action_snooze_7d_passes_until_date(monkeypatch, client):
    """Snooze-7d buttons compute today+7 in the handler before dispatching."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}

    def fake_run(*, verb, code, cp_hash, **extras):
        called["verb"] = verb
        called["code"] = code
        called["extras"] = extras
        return {"committed": True, "commit_sha": "def456", "errors": []}

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(
        webhook_main, "_post_response_url_update", lambda **_kw: None
    )

    payload = {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "actions": [{
            "action_id": "snooze-risk-7d_ibx-5167_09e3d0c7",
            "value": "snooze-risk-7d|ibx-5167|09e3d0c7",
            "type": "button",
        }],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {"blocks": []},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200

    for _ in range(50):
        if called:
            break
        time.sleep(0.01)

    from datetime import date as _date, timedelta as _td
    expected_until = (_date.today() + _td(days=7)).isoformat()
    assert called["verb"] == "snooze-risk"  # the -7d suffix was stripped
    assert called["extras"]["until"] == expected_until


def test_slack_action_rejects_invalid_value_format(monkeypatch, client):
    """Malformed value (only 2 pipe segments) returns 400 — NOT a
    background-task crash. Validation must happen BEFORE create_task."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    payload = {
        "type": "block_actions",
        "actions": [
            {"action_id": "x", "value": "only-two|parts", "type": "button"}
        ],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 400


def test_confirmation_text_surfaces_no_match_as_silent_dedupe() -> None:
    """When _run_plan_for_one_item returns committed=False, no errors, no sha
    (silent dedupe path: hash not in file because already resolved or moved
    sprint), _confirmation_text must NOT render '✅ Resolved' — that misleads
    the user. Render an explicit 'No matching item' message instead."""
    from main import _confirmation_text
    result = {"committed": False, "commit_sha": None, "errors": []}
    msg = _confirmation_text(verb="resolve-risk", extras={}, result=result)
    assert "No matching item" in msg
    assert "✅ Resolved" not in msg


def test_confirmation_text_renders_success_when_committed() -> None:
    """When committed=True with a SHA, render the success label + short SHA."""
    from main import _confirmation_text
    result = {"committed": True, "commit_sha": "abc12345def67890", "errors": []}
    msg = _confirmation_text(verb="resolve-risk", extras={}, result=result)
    assert "✅ Resolved" in msg
    assert "abc12345" in msg


def test_confirmation_text_renders_failure_when_errors_present() -> None:
    """When errors are present and no SHA, render the failure label with
    the first error text (truncated)."""
    from main import _confirmation_text
    result = {"committed": False, "commit_sha": None, "errors": ["hash not found"]}
    msg = _confirmation_text(verb="resolve-risk", extras={}, result=result)
    assert "⚠️ Action failed" in msg
    assert "hash not found" in msg


def test_run_plan_for_one_item_dispatches_close_ask_with_hash(monkeypatch, tmp_path):
    """End-to-end: a slack-action 'close-ask' payload with a hash flows
    through _run_plan_for_one_item → _write_close_ask (hash branch) → commit.

    Stubs _cloned_tenant + _commit_and_push so we don't hit network/git.
    Freezes `date.today()` to a W20 weekday so execute_plan targets the
    seeded W20 sprint file without auto-scaffolding forward to W23+.
    """
    from main import _run_plan_for_one_item
    import main as webhook_main

    # Build a minimal tenant with one open ask.
    sprint_dir = tmp_path / "sprints" / "2026-W20"
    sprint_dir.mkdir(parents=True)
    ask_hash = "abc12345"
    (sprint_dir / "ggl-5168.md").write_text(
        "---\nProject: ggl-5168 — Test\nSprint: 2026-W20\n---\n"
        "# ggl-5168 — Test · Sprint W19 (May 11 – May 17, 2026)\n"
        "## Client communication\n### Open asks\n"
        f"- [open · 2026-05-12 · Rena] Approve Round 3 <!-- cp:hash={ask_hash} -->\n"
    )

    from contextlib import contextmanager
    @contextmanager
    def fake_cloned():
        yield tmp_path

    captured = {}
    def fake_commit(*, tenant_root, meeting_id, ingested):
        captured["meeting_id"] = meeting_id
        captured["ingested"] = ingested
        return "fakesha1"

    monkeypatch.setattr(webhook_main, "_cloned_tenant", fake_cloned)
    monkeypatch.setattr(webhook_main, "_commit_and_push", fake_commit)

    # Freeze today to a W20 weekday so execute_plan writes into the seeded
    # 2026-W20 sprint file (not a future week that would need scaffolding).
    from datetime import date as _date

    class _FrozenDate(_date):
        @classmethod
        def today(cls):
            return _date(2026, 5, 12)

    monkeypatch.setattr(webhook_main, "date", _FrozenDate)

    result = _run_plan_for_one_item(
        verb="close-ask", code="ggl-5168", cp_hash=ask_hash, closed_by="slack",
    )
    assert result["committed"] is True
    assert result["commit_sha"] == "fakesha1"
    body = (sprint_dir / "ggl-5168.md").read_text()
    assert "[closed · 2026-05-12 · Rena]" in body
    assert "cp:closed-by=slack" in body
    assert captured["meeting_id"] == f"slack-close-ask-{ask_hash}"


def test_post_response_url_update_logs_non_ok_status(monkeypatch, caplog):
    """If Slack's response_url returns non-2xx, log a warning with the status
    code and response body (truncated). Don't raise — fire-and-forget UX."""
    from main import _post_response_url_update
    import logging as _logging

    class FakeResp:
        status_code = 410
        text = "expired response_url"
        ok = False

    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)

    with caplog.at_level(_logging.WARNING):
        _post_response_url_update(
            response_url="https://hooks.slack.com/expired",
            original_message={"blocks": []},
            confirmation="✅ Done",
            clicked_action_id="",
        )
    assert any("410" in rec.message for rec in caplog.records)
    assert captured["url"] == "https://hooks.slack.com/expired"


def test_post_response_url_update_replaces_only_clicked_action_block(monkeypatch):
    """REGRESSION GUARD (v0.14.2, Bug 1 fix): When a daily digest has N items
    each with their own actions block, clicking ONE button must only replace
    that ONE actions block — not all of them.

    Before this fix, _post_response_url_update walked every actions block in
    the message and replaced each with the same confirmation, so one click
    visually closed every item in the digest.

    The fix: thread the clicked action_id through and only replace the actions
    block whose element matches.
    """
    from main import _post_response_url_update

    captured = {}
    def fake_post(url, **kw):
        captured["json"] = kw.get("json")
        class _R:
            status_code = 200
            text = "ok"
            ok = True
        return _R()
    monkeypatch.setattr("requests.post", fake_post)

    # Three items, each with their own actions block. The user clicked the
    # SECOND item's Resolve button.
    original_message = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "scan"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "item 1"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "resolve-risk_a-1_aaaa1111"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": "item 2"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "resolve-risk_b-2_bbbb2222"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": "item 3"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "resolve-risk_c-3_cccc3333"},
            ]},
        ],
    }
    _post_response_url_update(
        response_url="https://hooks.slack.com/fake",
        original_message=original_message,
        confirmation="✅ Resolved",
        clicked_action_id="resolve-risk_b-2_bbbb2222",
    )

    new_blocks = captured["json"]["blocks"]
    # Item 1's actions block: unchanged (still type=actions)
    assert new_blocks[2]["type"] == "actions"
    assert new_blocks[2]["elements"][0]["action_id"] == "resolve-risk_a-1_aaaa1111"
    # Item 2's actions block: replaced with context carrying the confirmation
    assert new_blocks[4]["type"] == "context"
    assert "Resolved" in new_blocks[4]["elements"][0]["text"]
    # Item 3's actions block: unchanged
    assert new_blocks[6]["type"] == "actions"
    assert new_blocks[6]["elements"][0]["action_id"] == "resolve-risk_c-3_cccc3333"


def test_post_response_url_update_logs_when_action_id_not_found(monkeypatch, caplog):
    """If the clicked action_id can't be found in original_message blocks
    (e.g. message was edited mid-click), log a warning. Don't silently
    swallow the confirmation by collapsing every actions block (the v0.14.1
    bug we're fixing)."""
    from main import _post_response_url_update
    import logging as _logging

    monkeypatch.setattr("requests.post", lambda *a, **kw: type(
        "R", (), {"status_code": 200, "text": "", "ok": True}
    )())

    original_message = {
        "blocks": [
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "real-id_a_aaaa"},
            ]},
        ],
    }
    with caplog.at_level(_logging.WARNING):
        _post_response_url_update(
            response_url="https://hooks.slack.com/fake",
            original_message=original_message,
            confirmation="✅ Done",
            clicked_action_id="bogus_id_that_does_not_exist",
        )
    assert any("not found" in rec.message for rec in caplog.records)


# ─────────────────────────────────────────────────────────────
#  Task 3.3: views.open modal + view_submission handler
# ─────────────────────────────────────────────────────────────


def test_view_submission_dispatches_snooze_with_picked_date(monkeypatch, client):
    """A view_submission for the snooze modal: validates date, queues background
    work with `until` from the picker. Returns response_action: clear so Slack
    closes the modal."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}
    def fake_run(*, verb, code, cp_hash, **extras):
        called["verb"] = verb
        called["until"] = extras.get("until")
        return {"committed": True, "commit_sha": "ghi789", "errors": []}

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(webhook_main, "_post_response_url_update", lambda **_kw: None)

    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "snooze_until_modal",
            "private_metadata": json.dumps({
                "verb": "snooze-risk", "code": "ibx-5167", "hash": "09e3d0c7",
                "response_url": "https://hooks.slack.com/actions/.../fake",
            }),
            "state": {"values": {
                "date_block": {"until_date": {"selected_date": "2026-07-15"}},
            }},
        },
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"response_action": "clear"}

    import time as _time
    for _ in range(50):
        if called:
            break
        _time.sleep(0.01)

    assert called["verb"] == "snooze-risk"
    assert called["until"] == "2026-07-15"


def test_view_submission_returns_field_error_when_no_date_picked(monkeypatch, client):
    """Empty selected_date → inline modal error, no background dispatch."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}
    def fake_run(*, verb, code, cp_hash, **extras):
        called["fired"] = True
        return {}
    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)

    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "snooze_until_modal",
            "private_metadata": json.dumps({
                "verb": "snooze-risk", "code": "ibx-5167", "hash": "09e3d0c7",
            }),
            "state": {"values": {
                "date_block": {"until_date": {"selected_date": ""}},
            }},
        },
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["response_action"] == "errors"
    assert "date_block" in body_json["errors"]
    assert called == {}


def test_view_submission_ignores_unknown_callback_id(monkeypatch, client):
    """Foreign modal callback_id: return ok+ignored, NOT response_action: clear
    (which would close a modal we don't own)."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    payload = {
        "type": "view_submission",
        "view": {"callback_id": "some_other_modal", "private_metadata": "{}"},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["ok"] is True
    assert body_json["ignored"] == "some_other_modal"
    assert body_json.get("response_action") is None


def test_view_submission_handles_malformed_private_metadata(monkeypatch, client):
    """Malformed private_metadata JSON → return ignored, NOT a 500.

    Defensive: Slack echoes private_metadata verbatim, but version skew or
    test traffic could feed garbage. We should not return 500 (which Slack
    retries 3x) or response_action: clear (which closes the modal silently)."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "snooze_until_modal",
            "private_metadata": "{not valid json",
            "state": {"values": {"date_block": {"until_date": {"selected_date": "2026-07-15"}}}},
        },
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["ok"] is True
    assert "malformed" in body_json.get("ignored", "").lower()


def test_view_submission_returns_field_error_when_metadata_missing_fields(monkeypatch, client):
    """If private_metadata is valid JSON but missing verb/code/hash (schema
    drift or replayed old payload), return inline modal error rather than
    silently dispatching a nonsense plan."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}
    def fake_run(*, verb, code, cp_hash, **extras):
        called["fired"] = True
        return {}
    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)

    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "snooze_until_modal",
            "private_metadata": json.dumps({"verb": "snooze-risk"}),  # missing code, hash
            "state": {"values": {"date_block": {"until_date": {"selected_date": "2026-07-15"}}}},
        },
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["response_action"] == "errors"
    assert "date_block" in body_json["errors"]
    assert "expired" in body_json["errors"]["date_block"].lower()
    assert called == {}


def test_view_submission_does_not_set_closed_by_on_snooze_extras(monkeypatch, client):
    """Snooze items should not carry a `closed_by` field — it's only meaningful
    for close-ask / resolve-risk. Snoozes are time-shifts, not closures."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    captured = {}
    def fake_run(*, verb, code, cp_hash, **extras):
        captured["extras"] = extras
        return {"committed": True, "commit_sha": "abc", "errors": []}
    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(webhook_main, "_post_response_url_update", lambda **_: None)

    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "snooze_until_modal",
            "private_metadata": json.dumps({
                "verb": "snooze-risk", "code": "ibx-5167", "hash": "09e3d0c7",
            }),
            "state": {"values": {"date_block": {"until_date": {"selected_date": "2026-07-15"}}}},
        },
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200

    import time as _time
    for _ in range(50):
        if captured:
            break
        _time.sleep(0.01)

    assert captured["extras"].get("until") == "2026-07-15"
    assert "closed_by" not in captured["extras"]


# ─────────────────────────────────────────────────────────────
#  Sprint-week threading (fix/slack-close-week-context)
# ─────────────────────────────────────────────────────────────


def test_block_action_parses_week_iso_from_4th_value_part(monkeypatch, client):
    """A close-ask value with a 4th pipe-part (the digest's sprint week) is
    parsed and threaded to _run_plan_for_one_item as week_iso."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}
    def fake_run(*, verb, code, cp_hash, **extras):
        called["verb"] = verb
        called["code"] = code
        called["hash"] = cp_hash
        called["extras"] = extras
        return {"committed": True, "commit_sha": "abc123", "errors": []}

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(webhook_main, "_post_response_url_update", lambda **_kw: None)

    payload = {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "actions": [{
            "action_id": "close-ask_storyos_2fcbd862",
            "value": "close-ask|storyos|2fcbd862|2026-W27",
            "type": "button",
        }],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {"blocks": []},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200, resp.text

    for _ in range(50):
        if called:
            break
        time.sleep(0.01)

    assert called["verb"] == "close-ask"
    assert called["code"] == "storyos"
    assert called["extras"].get("week_iso") == "2026-W27"


def test_block_action_three_part_value_still_valid_no_week(monkeypatch, client):
    """Backward-compat: an old 3-part value (no week) is still accepted and
    threads week_iso=None (execute_plan then defaults to current week)."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-slack-secret")
    import main as webhook_main

    called = {}
    def fake_run(*, verb, code, cp_hash, **extras):
        called["extras"] = extras
        return {"committed": True, "commit_sha": "abc123", "errors": []}

    monkeypatch.setattr(webhook_main, "_run_plan_for_one_item", fake_run)
    monkeypatch.setattr(webhook_main, "_post_response_url_update", lambda **_kw: None)

    payload = {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "actions": [{
            "action_id": "close-ask_storyos_2fcbd862",
            "value": "close-ask|storyos|2fcbd862",
            "type": "button",
        }],
        "response_url": "https://hooks.slack.com/actions/.../fake",
        "message": {"blocks": []},
    }
    body, headers = _signed_slack_request(payload)
    resp = client.post("/slack-action", content=body, headers=headers)
    assert resp.status_code == 200, resp.text

    for _ in range(50):
        if called:
            break
        time.sleep(0.01)

    assert called["extras"].get("week_iso") is None


def test_run_plan_for_one_item_closes_ask_in_digest_week_not_today(monkeypatch, tmp_path):
    """THE BUG: the open ask lives ONLY in 2026-W27's sprint file, but the
    click happens in a later week. Threading week_iso=2026-W27 must route the
    close to the W27 file and SUCCEED (not 'No matching item').
    """
    from main import _run_plan_for_one_item
    import main as webhook_main

    # Seed the ask in W27 ONLY.
    w27 = tmp_path / "sprints" / "2026-W27"
    w27.mkdir(parents=True)
    ask_hash = "2fcbd862"
    (w27 / "storyos.md").write_text(
        "---\nProject: storyos\nSprint: 2026-W27\n---\n"
        "# storyos · Sprint W27\n"
        "## Team communication\n### Open asks\n"
        f"- [open · 2026-06-24 · Drew] Review the plan <!-- cp:hash={ask_hash} -->\n"
    )

    from contextlib import contextmanager
    @contextmanager
    def fake_cloned():
        yield tmp_path

    def fake_commit(*, tenant_root, meeting_id, ingested):
        return "fakesha1"

    monkeypatch.setattr(webhook_main, "_cloned_tenant", fake_cloned)
    monkeypatch.setattr(webhook_main, "_commit_and_push", fake_commit)

    # Freeze today into a LATER week (W28) so the default-current-week path
    # would search the wrong file. Only the threaded week_iso saves it.
    from datetime import date as _date

    class _FrozenDate(_date):
        @classmethod
        def today(cls):
            return _date(2026, 7, 8)  # ISO week 28

    monkeypatch.setattr(webhook_main, "date", _FrozenDate)

    result = _run_plan_for_one_item(
        verb="close-ask", code="storyos", cp_hash=ask_hash,
        closed_by="slack", week_iso="2026-W27",
    )
    assert result["committed"] is True, result
    body = (w27 / "storyos.md").read_text()
    assert "[closed" in body
    assert "cp:closed-by=slack" in body


def test_run_plan_for_one_item_defaults_current_week_when_no_week_iso(monkeypatch, tmp_path):
    """Backward-compat: week_iso=None → execute_plan uses today's week
    (unchanged behavior). Item seeded in W20, today frozen to a W20 weekday."""
    from main import _run_plan_for_one_item
    import main as webhook_main

    w20 = tmp_path / "sprints" / "2026-W20"
    w20.mkdir(parents=True)
    ask_hash = "abc12345"
    (w20 / "ggl-5168.md").write_text(
        "---\nProject: ggl-5168\nSprint: 2026-W20\n---\n"
        "## Client communication\n### Open asks\n"
        f"- [open · 2026-05-12 · Rena] Approve Round 3 <!-- cp:hash={ask_hash} -->\n"
    )

    from contextlib import contextmanager
    @contextmanager
    def fake_cloned():
        yield tmp_path

    monkeypatch.setattr(webhook_main, "_cloned_tenant", fake_cloned)
    monkeypatch.setattr(webhook_main, "_commit_and_push",
                        lambda **kw: "fakesha1")

    from datetime import date as _date

    class _FrozenDate(_date):
        @classmethod
        def today(cls):
            return _date(2026, 5, 12)  # ISO week 20

    monkeypatch.setattr(webhook_main, "date", _FrozenDate)

    result = _run_plan_for_one_item(
        verb="close-ask", code="ggl-5168", cp_hash=ask_hash,
        closed_by="slack",  # note: no week_iso
    )
    assert result["committed"] is True, result
    body = (w20 / "ggl-5168.md").read_text()
    assert "[closed" in body
