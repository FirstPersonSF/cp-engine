"""POST /api/auto-ingest-account routes to the PARENT NODE (#305, plan §3.7).

The payload names the workstream the meeting was tagged to (`code`); a
legacy fathom-meeting-sync row may send only `company_code`, which means
that company's account node. Neither resolving is a 400 — the level is
named, never guessed. The plan is generated against the node's ACTIVE
SUBTREE and the summary entry is keyed `account:<node code>`.

Everything below the endpoint (tenant clone, transcript fetch, plan
generation, execution, commits, run logging, artifacts) is stubbed on the
router module — this pins the routing decision, not the pipeline.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main
from routers import ingest as router_mod

from cp_engine.plan_from_account_meeting import GeneratedAccountPlan
from cp_engine.state import ProjectState


def _ws(code, name, *, label=None, parent_code=None, has_agreement=True,
        company_code="GGL", company_name="Google", status="Open"):
    return ProjectState(
        code=code, name=name, company_kind="client", company_code=company_code,
        company_name=company_name, status=status, is_internal=False, owner="Drew",
        last_touched=datetime(2026, 9, 24, tzinfo=timezone.utc), deadline=None,
        parent_code=parent_code, has_agreement=has_agreement, label=label,
    )


ROSTER = [
    _ws("ggl-5216-google", "Google", label="account", has_agreement=False),
    _ws("ggl-5300-go-safety", "Go Safety", label="program", parent_code="ggl-5216-google"),
    _ws("ggl-5136-go-safety-website", "Website", parent_code="ggl-5300-go-safety"),
    _ws("ggl-5168-activation", "Activation", parent_code="ggl-5216-google"),
    _ws("ggl-5188-calendar", "Calendar", parent_code="ggl-5216-google", status="Holding"),
    _ws("ibx-5217-infoblox", "Infoblox", label="account", has_agreement=False,
        company_code="IBX", company_name="Infoblox"),
]


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Stub the pipeline; record what the endpoint asked the generator for."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    seen: dict = {}

    @contextlib.contextmanager
    def fake_clone():
        yield tmp_path

    monkeypatch.setattr(router_mod.git_ops, "_cloned_tenant", fake_clone)
    monkeypatch.setattr(router_mod.pipeline, "_load_tenant_config", lambda root: object())
    monkeypatch.setattr(router_mod.pipeline, "_fetch_transcript", lambda mid: "transcript")
    monkeypatch.setattr(router_mod.pipeline, "_stage_transcript", lambda *a, **k: tmp_path / "t.md")
    monkeypatch.setattr(router_mod.pipeline, "_fetch_meeting", lambda mid: {})
    monkeypatch.setattr(router_mod.pipeline, "_create_supabase_client", lambda: None)
    monkeypatch.setattr(router_mod.pipeline, "_log_run_to_supabase", lambda **k: seen.setdefault("runs", []).append(k))
    monkeypatch.setattr(router_mod.pipeline, "_generate_meeting_artifacts", lambda **k: {})
    monkeypatch.setattr(router_mod.pipeline, "_append_retrospective", lambda **k: "skipped")
    monkeypatch.setattr(router_mod.git_ops, "_commit_and_push", lambda **k: "sha-1")
    monkeypatch.setattr(router_mod, "load_roster", lambda config: list(ROSTER))

    def fake_generate(*, config, code, meeting_id, transcript_text, active_projects, node=None, **kw):
        seen["code"] = code
        seen["node"] = node.code if node else None
        seen["active"] = [p.code for p in active_projects]
        return GeneratedAccountPlan(
            plan={
                "transcript": {"source": "fathom"},
                "projects": {},
                "account_summary": {"text": "Sync.", "code": code, "week": "2026-W39"},
            },
            raw_response="", code=code, meeting_id=meeting_id,
            project_codes=tuple(p.code for p in active_projects), model="m",
        )

    monkeypatch.setattr(router_mod, "generate_account_plan", fake_generate)

    def fake_execute(plan, **kw):
        seen.setdefault("executed", []).append(plan)
        from cp_engine.ingest import IngestPlanResult
        return IngestPlanResult(files_written=[tmp_path / "x.md"])

    monkeypatch.setattr(router_mod, "execute_plan", fake_execute)
    return seen


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/auto-ingest-account",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


def test_code_names_the_parent_node_and_fans_out_to_its_active_subtree(client, stubbed):
    resp = _post(client, {"meeting_id": "m-1", "code": "ggl-5216-google"})
    assert resp.status_code == 200, resp.text
    assert stubbed["code"] == "ggl-5216-google"
    assert stubbed["node"] == "ggl-5216-google"
    # Grandchild included, the node excluded, the Holding job out.
    assert stubbed["active"] == [
        "ggl-5136-go-safety-website", "ggl-5168-activation", "ggl-5300-go-safety",
    ]
    codes = [e["code"] for e in resp.json()["ingested"]]
    assert codes == ["account:ggl-5216-google"]


def test_a_program_code_fans_out_to_its_own_children(client, stubbed):
    resp = _post(client, {"meeting_id": "m-1", "code": "ggl-5300"})  # short form
    assert resp.status_code == 200, resp.text
    assert stubbed["code"] == "ggl-5300-go-safety"
    assert stubbed["active"] == ["ggl-5136-go-safety-website"]


def test_legacy_company_code_resolves_to_the_account_node(client, stubbed):
    resp = _post(client, {"meeting_id": "m-1", "company_code": "GGL"})
    assert resp.status_code == 200, resp.text
    assert stubbed["code"] == "ggl-5216-google"
    assert stubbed["node"] == "ggl-5216-google"


def test_400_when_neither_resolves(client, stubbed):
    assert _post(client, {"meeting_id": "m-1"}).status_code == 400
    resp = _post(client, {"meeting_id": "m-1", "code": "zzz-9999-nothing"})
    assert resp.status_code == 400
    assert "unknown workstream code" in resp.json()["detail"]
    resp = _post(client, {"meeting_id": "m-1", "company_code": "SAP"})
    assert resp.status_code == 400
    assert "no account node" in resp.json()["detail"]
    assert "code" not in stubbed  # the generator was never reached


def test_a_leaf_with_no_children_is_a_no_op_not_an_error(client, stubbed):
    resp = _post(client, {"meeting_id": "m-1", "code": "ggl-5168-activation"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped_no_op"] is True
    assert "no active workstreams under 'ggl-5168-activation'" in resp.json()["reason"]
