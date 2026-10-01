"""Architecture plan step 3 — caller wiring for failures the modules now return.

Each test fails on the pre-step-3 code: the module-level fix existed (or not)
but the caller dropped the signal on the floor.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

def test_missing_sprint_file_error_names_the_mc2_cause(tmp_path, monkeypatch):
    """A scaffold that failed on an MC-2 read must say so, not just
    'no prior sprint file' (the #194 error text hid exactly this)."""
    from cp_engine import ingest, sprints

    (tmp_path / "sprints").mkdir()
    (tmp_path / ".cp-engine.toml").write_text("[tenant]\nname='t'\n")

    def failing_scaffold(**kw):
        logging.getLogger("cp_engine.sprints").warning(
            "MC-2 sprint-stem resolution failed for ggl-5168: APIError: JWT expired")
        return None

    monkeypatch.setattr(sprints, "scaffold_from_prior", failing_scaffold)
    plan = {"projects": {"ggl-5168": {"inbound": [
        {"text": "note", "date": "2026-09-30", "who": "Rena"}]}}}
    res = ingest.execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 30))
    assert any("sprint file missing" in e and "JWT expired" in e for e in res.errors), res.errors


def test_resolve_tags_route_returns_index_errors(monkeypatch):
    import hashlib
    import hmac
    import json

    import main as webhook_main
    from fastapi.testclient import TestClient
    from routers import integrations

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    def fake_resolve(client, tags, errors=None):
        errors.append("MC-2 code index unreadable: timeout")
        return [{"tag": t, "code": None} for t in tags]

    monkeypatch.setattr("cp_engine.tag_resolve.resolve_tags", fake_resolve)
    monkeypatch.setattr(integrations.pipeline, "_create_supabase_client", lambda: None)
    body = json.dumps({"tags": ["Google"]}).encode()
    sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    resp = TestClient(webhook_main.app).post(
        "/api/resolve-tags", content=body, headers={"x-webhook-signature": sig})
    assert resp.status_code == 200
    assert resp.json()["errors"] == ["MC-2 code index unreadable: timeout"]
