"""Architecture plan step 3 — the webhook fails loudly.

Each test pins one behavior change and FAILS on the pre-step-3 code:

- every POST writes a ``webhook_runs`` row on every path (rejected, failed,
  partial, ok, accepted) — thirteen of seventeen routes wrote none;
- background tails (slack click, meeting promote) write their own row;
- the account / sprint-planning routes derive status instead of hardcoding
  "success" over per-project errors (the #194 shape);
- auto-ingest side-step failures reach the response and the run row;
- a DB error is no longer read as "not found" (clickup lookup, meeting fetch);
- route-email's failed status flip reaches the response.
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
import pipeline
import run_ledger
from routers import ingest as ingest_router
from routers import integrations as integrations_router
from routers import meetings as meetings_router
from routers import slack as slack_router


class _FakeTable:
    def __init__(self, store, name, fail=None):
        self.store, self.name, self.fail = store, name, fail
        self._row = None

    def insert(self, row):
        self._row = row
        return self

    def execute(self):
        if self.fail and self.fail(self.name, self._row):
            raise Exception(self.fail(self.name, self._row))
        self.store.setdefault(self.name, []).append(self._row)
        return type("R", (), {"data": [self._row]})()


class _FakeClient:
    def __init__(self, fail=None):
        self.rows: dict[str, list[dict]] = {}
        self.fail = fail

    def table(self, name):
        return _FakeTable(self.rows, name, self.fail)


@pytest.fixture
def ledger(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(run_ledger.mc2_db, "get_client", lambda **kw: fake)
    return fake


def _signed(body: bytes, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


# ── every path writes a ledger row ────────────────────────────────────────


def test_a_rejected_request_writes_a_run_row(ledger, monkeypatch):
    """Bad signature on a route that had NO run table: 401 AND a row."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    resp = TestClient(webhook_main.app).post(
        "/api/sessions/capture", content=b"{}",
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.status_code == 401
    rows = ledger.rows.get("webhook_runs") or []
    assert len(rows) == 1
    assert rows[0]["route"] == "/api/sessions/capture"
    assert rows[0]["status"] == "rejected"
    assert rows[0]["http_status"] == 401


def test_a_failed_request_writes_a_failed_row(ledger, monkeypatch):
    """A route raising inside its tenant clone → 500 AND a failed row with the
    error. (Was /dates-loop until that route was retired in step 5a.)"""
    import routers.improvements as dl

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    @contextlib.contextmanager
    def boom():
        raise RuntimeError("clone exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(dl.git_ops, "_cloned_tenant", boom)
    body = json.dumps({"user": "drew", "area": "x", "observation": "y"}).encode()
    resp = TestClient(webhook_main.app, raise_server_exceptions=False).post(
        "/api/improvements/append", content=body,
        headers={"x-webhook-signature": _signed(body)},
    )
    assert resp.status_code >= 500
    rows = ledger.rows.get("webhook_runs") or []
    assert [r["status"] for r in rows] == ["failed"]
    assert "clone exploded" in (rows[0]["error"] or "")


def test_the_response_is_unchanged_by_the_ledger(ledger, monkeypatch):
    """The middleware re-emits the body byte-for-byte (status + JSON)."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    resp = TestClient(webhook_main.app).post(
        "/api/sessions/capture", content=b"{}",
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.headers["content-type"].startswith("application/json")
    assert "detail" in resp.json()


@pytest.mark.parametrize(
    "http_status,body,expected",
    [
        (200, {"ok": True}, "ok"),
        (200, {"ok": False, "error": "x"}, "failed"),
        (200, {"status": "unrouted", "warnings": ["lost"]}, "partial"),
        (200, {"ingested": [{"code": "a", "errors": ["e"]}]}, "partial"),
        (200, {"ingested": False, "reason": "no proposal"}, "ok"),
        (202, {"status": "running"}, "accepted"),
        (401, {"detail": "bad sig"}, "rejected"),
        (502, {"detail": "push failed"}, "failed"),
    ],
)
def test_derive_status(http_status, body, expected):
    assert run_ledger.derive_status(http_status, body)[0] == expected


def test_a_ledger_insert_failure_is_loud_not_raised(monkeypatch, caplog):
    fake = _FakeClient(fail=lambda name, row: "relation webhook_runs does not exist")
    monkeypatch.setattr(run_ledger.mc2_db, "get_client", lambda **kw: fake)
    import logging

    with caplog.at_level(logging.ERROR):
        ok = run_ledger.record_run(route="/x", status="ok")
    assert ok is False
    assert any(r.levelno >= logging.ERROR and "webhook_runs" in r.message
               for r in caplog.records)


# ── background tails write their own row ──────────────────────────────────


def test_slack_click_whose_plan_raised_writes_a_failed_row(ledger, monkeypatch):
    def boom(**kw):
        raise RuntimeError("push lost the race")

    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", boom)
    monkeypatch.setattr(slack_router, "_post_response_url_update", lambda **kw: None)
    asyncio.run(slack_router._run_action_in_background(
        verb="resolve", code="ggl-5168", cp_hash="abc123", extras={},
        response_url="https://hooks.slack.com/x", original_message={},
    ))
    rows = ledger.rows.get("webhook_runs") or []
    assert len(rows) == 1
    assert rows[0]["route"] == "/slack-action#background"
    assert rows[0]["status"] == "failed"
    assert "push lost the race" in rows[0]["error"]


def test_slack_click_whose_confirmation_failed_is_partial(ledger, monkeypatch):
    monkeypatch.setattr(
        slack_router, "_run_plan_for_one_item",
        lambda **kw: {"committed": True, "commit_sha": "s1", "errors": []},
    )

    def gone(**kw):
        raise RuntimeError("response_url update returned 410")

    monkeypatch.setattr(slack_router, "_post_response_url_update", gone)
    asyncio.run(slack_router._run_action_in_background(
        verb="resolve", code="ggl-5168", cp_hash="abc123", extras={},
        response_url="https://hooks.slack.com/x", original_message={},
    ))
    rows = ledger.rows["webhook_runs"]
    assert rows[0]["status"] == "partial"
    assert "410" in rows[0]["error"]


def test_meeting_promote_background_failure_writes_a_row(ledger, monkeypatch):
    import cp_engine.meetings as meetings_mod

    def boom(*a, **kw):
        raise RuntimeError("embed service down")

    monkeypatch.setattr(meetings_mod, "promote_meeting_transcript", boom)
    asyncio.run(meetings_router._run_meeting_promote(
        object(), {"recording_id": 42}, "pid", None,
    ))
    rows = ledger.rows.get("webhook_runs") or []
    assert [(r["route"], r["status"]) for r in rows] == [
        ("/api/meetings/promote-transcript#background", "failed")
    ]
    assert "embed service down" in rows[0]["error"]


# ── account route: status derived, not hardcoded ──────────────────────────


@pytest.fixture
def account_stubs(monkeypatch, tmp_path):
    """Account route with one project whose plan execution reports an error."""
    from datetime import datetime, timezone

    from cp_engine.ingest import IngestPlanResult
    from cp_engine.plan_from_account_meeting import GeneratedAccountPlan
    from cp_engine.state import ProjectState

    def ws(code, label=None, parent=None, agreement=True):
        return ProjectState(
            code=code, name=code, company_kind="client", company_code="GGL",
            company_name="Google", status="Open", owner="Drew",
            last_touched=datetime(2026, 9, 24, tzinfo=timezone.utc), deadline=None,
            parent_code=parent, has_agreement=agreement, label=label,
        )

    roster = [
        ws("ggl-5216-google", label="account", agreement=False),
        ws("ggl-5168-activation", parent="ggl-5216-google"),
        ws("ggl-5136-website", parent="ggl-5216-google"),
    ]
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    runs: list[dict] = []

    @contextlib.contextmanager
    def fake_clone():
        yield tmp_path

    r = ingest_router
    monkeypatch.setattr(r.git_ops, "_cloned_tenant", fake_clone)
    monkeypatch.setattr(r.pipeline, "_load_tenant_config", lambda root: object())
    monkeypatch.setattr(r.pipeline, "_fetch_transcript", lambda mid: "transcript")
    monkeypatch.setattr(r.pipeline, "_stage_transcript", lambda *a, **k: tmp_path / "t.md")
    monkeypatch.setattr(r.pipeline, "_fetch_meeting", lambda mid: {})
    monkeypatch.setattr(r.pipeline, "_create_supabase_client", lambda: None)
    monkeypatch.setattr(r.pipeline, "_log_run_to_supabase", lambda **k: runs.append(k))
    monkeypatch.setattr(r.pipeline, "_generate_meeting_artifacts", lambda **k: {})
    monkeypatch.setattr(r.pipeline, "_append_retrospective", lambda **k: "skipped")
    monkeypatch.setattr(r.git_ops, "_commit_and_push", lambda **k: "sha-1")
    monkeypatch.setattr(r, "load_roster", lambda config: list(roster))

    def fake_generate(*, config, code, meeting_id, transcript_text, active_projects, node=None, **kw):
        return GeneratedAccountPlan(
            plan={"transcript": {"source": "fathom"}, "projects": {
                "ggl-5168-activation": {"record-ask": [{"text": "a"}]},
                "ggl-5136-website": {"record-ask": [{"text": "b"}]},
            }},
            raw_response="", code=code, meeting_id=meeting_id,
            project_codes=tuple(p.code for p in active_projects), model="m",
        )

    monkeypatch.setattr(r, "generate_account_plan", fake_generate)

    def fake_execute(plan, **kw):
        code = next(iter(plan.get("projects") or {}), None)
        if code == "ggl-5136-website":
            return IngestPlanResult(errors=["sprint file missing for ggl-5136"])
        return IngestPlanResult(files_written=[tmp_path / "x.md"])

    monkeypatch.setattr(r, "execute_plan", fake_execute)
    return runs


def test_account_route_does_not_stamp_success_over_a_project_error(account_stubs):
    body = json.dumps({"meeting_id": "m1", "code": "ggl-5216-google"}).encode()
    resp = TestClient(webhook_main.app).post(
        "/api/auto-ingest-account", content=body,
        headers={"x-webhook-signature": _signed(body)},
    )
    assert resp.status_code == 200
    assert account_stubs[-1]["status"] == "failed"  # was hardcoded "success"


def test_account_route_carries_a_missing_meeting_row_as_a_warning(account_stubs, monkeypatch):
    monkeypatch.setattr(ingest_router.pipeline, "_fetch_meeting", lambda mid: None)
    body = json.dumps({"meeting_id": "m1", "code": "ggl-5216-google"}).encode()
    resp = TestClient(webhook_main.app).post(
        "/api/auto-ingest-account", content=body,
        headers={"x-webhook-signature": _signed(body)},
    )
    assert resp.json()["warnings"] == [pipeline.MEETING_ROW_MISSING]
    assert account_stubs[-1]["warnings"] == [pipeline.MEETING_ROW_MISSING]


# ── auto-ingest side steps ────────────────────────────────────────────────


def test_run_row_records_warnings_in_their_column(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(pipeline.mc2_db, "get_client", lambda **kw: fake)
    pipeline._log_run_to_supabase(
        meeting_id="m1", project_codes=["a"], status="success",
        ingested=[{"code": "a", "plan_summary": {"record-ask": 1}}],
        commit_sha="s", warnings=["a: transcript persist failed: OSError: disk"],
    )
    row = fake.rows["auto_ingest_runs"][0]
    assert row["warnings"] == ["a: transcript persist failed: OSError: disk"]
    assert row["errors"] is None  # warnings never flip status / replay selection


def test_run_row_folds_warnings_when_the_column_is_missing(monkeypatch):
    def fail(name, row):
        return "column \"warnings\" does not exist" if "warnings" in row else None

    fake = _FakeClient(fail=fail)
    monkeypatch.setattr(pipeline.mc2_db, "get_client", lambda **kw: fake)
    pipeline._log_run_to_supabase(
        meeting_id="m1", project_codes=["a"], status="success",
        ingested=[{"code": "a", "plan_summary": {"record-ask": 1}}],
        commit_sha="s", warnings=["w1"],
    )
    row = fake.rows["auto_ingest_runs"][0]
    assert row["plan_summary"]["_warnings"] == ["w1"]
    assert row["plan_summary"]["a"] == {"record-ask": 1}


def test_meeting_artifact_commit_miss_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "write_meeting_artifacts",
                        lambda **kw: [tmp_path / "m.md"])
    monkeypatch.setattr(pipeline.git_ops, "_commit_meeting_artifacts", lambda **kw: None)
    summary = pipeline._generate_meeting_artifacts(
        tenant_root=tmp_path, meeting_id="m1", transcript_text="t",
        project_codes=["a"], meeting={"id": "m1"},
    )
    assert "commit/push failed" in summary.get("error", "")


def test_commitments_propose_failure_is_in_the_summary(monkeypatch):
    import commitments_propose as cp_mod

    class Boom:
        def table(self, name):
            raise RuntimeError("supabase 503")

    monkeypatch.setattr(cp_mod.mc2_db, "get_client", lambda **kw: Boom())
    out = cp_mod.propose_commitments("m1", ["ggl-5168"], roster=None)
    assert "supabase 503" in out.get("error", "")


# ── a DB error is not "not found" ─────────────────────────────────────────


def test_clickup_lookup_raises_on_a_db_error(monkeypatch):
    class Boom:
        def table(self, name):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(integrations_router.mc2_db, "get_client", lambda **kw: Boom())
    monkeypatch.setattr(integrations_router.mc2_db, "owner_columns", lambda c: "project_id")
    with pytest.raises(RuntimeError, match="connection reset"):
        integrations_router._lookup_proposal_by_clickup_task_id("T1")


def test_meeting_fetch_raises_on_a_db_error_but_not_on_no_row():
    class Q:
        def __init__(self, exc):
            self.exc = exc

        def __getattr__(self, name):
            return lambda *a, **k: self

        def execute(self):
            raise self.exc

    class C:
        def __init__(self, exc):
            self.exc = exc

        def table(self, name):
            return Q(self.exc)

    assert meetings_router._fetch_meeting_by_recording_id(
        C(Exception("PGRST116: JSON object requested, multiple (or no) rows returned")), 1
    ) is None
    with pytest.raises(Exception, match="permission denied"):
        meetings_router._fetch_meeting_by_recording_id(
            C(Exception("42501 permission denied for table fathom_meetings")), 1
        )


def test_unrouted_email_not_recorded_is_a_warning():
    from routers.email import _with_unrecorded_warning

    out = _with_unrecorded_warning({"status": "unrouted", "recorded": False})
    assert out["warnings"]
    assert run_ledger.derive_status(200, out)[0] == "partial"
    assert "warnings" not in _with_unrecorded_warning({"status": "unrouted", "recorded": True})
