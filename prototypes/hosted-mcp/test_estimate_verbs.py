"""`get_estimate` and `build_estimate`: Scope Studio from a chat.

Tony, 2026-11-19: a partner in Claude Desktop should be able to have Scope
Studio draft an estimate, not only read one off the Spine Map. What these
tests pin:

- `get_estimate` is on `/mcp/read` and only GETs; `build_estimate` is on
  `/mcp` only;
- every call goes to mc-2 under the caller's sign-in, and a refusal sends
  nothing: a non-member, an unknown project, an empty brief, an estimate on
  another job, a non-pending estimate, a build already running, and a job
  that already has estimates when neither `estimate_id` nor `new_estimate`
  says which;
- a job with no estimates gets its first; `new_estimate` creates one;
- the session is opened with a POST (never the creating GET) before the
  autonomous run starts with the brief as its goal;
- an estimate's content comes back as phases, activities and role subtotals,
  with an unpriced activity marked.

    python -m pytest prototypes/hosted-mcp/test_estimate_verbs.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_map_verbs import _Audit, _Store  # noqa: E402

JOB = "p-ggl"
SOURCE = {
    "description": "Munich pilot, template, two site videos.",
    "scopeIncluded": ["AI voiceover"], "scopeExcluded": ["Translation"], "scopeLimits": [],
    "estimateLines": [],
    "phases": [{
        "name": "Munich Pilot", "position": 0, "duration_weeks": 7, "overview": "o",
        "deliverables": [{"name": "Munich video"}],
        "activities": [
            {"name": "Discovery", "description": "d",
             "serviceLines": [{"display_name": "Narrative Strategy", "qty": 20, "rate": 300}],
             "hardCosts": []},
            {"name": "Design", "description": "d", "serviceLines": [], "hardCosts": []},
        ],
    }],
}


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    mp.delenv("TENANT_REPO", raising=False)
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def pt(server):
    return server.ported_tools


@pytest.fixture
def env(server, monkeypatch):
    state = SimpleNamespace(estimates=[], fail=None)
    gets: list[str] = []
    sends: list[tuple[str, str, dict | None]] = []

    def fake_get(path, what):
        gets.append(path)
        if path == f"/api/estimates/for-job/{JOB}":
            return {"ok": True, "data": {"job_id": JOB, "estimates": [dict(e) for e in state.estimates]}}
        if path.startswith("/api/documents/by-estimate/"):
            return {"ok": True, "data": SOURCE}
        return {"ok": False, "reason": f"unexpected GET {path}"}

    def fake_send(method, path, body, what):
        sends.append((method, path, body))
        if state.fail:
            return {"ok": False, "reason": state.fail}
        if path == "/api/estimates":
            return {"ok": True, "data": {"estimate": {"id": "est-new", "name": body.get("name") or "Estimate 1"}}}
        if path.startswith("/api/scope-studio/sessions/by-estimate/"):
            return {"ok": True, "data": {"id": "sess-1"}}
        if path.endswith("/autonomous/start"):
            return {"ok": True, "data": {"state": "running"}}
        return {"ok": False, "reason": f"unexpected {method} {path}"}

    monkeypatch.setattr(server, "user_client", lambda: _Store({}))
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "brandon@example.com")
    monkeypatch.setattr(server, "resolve_project_id", lambda c, code: None if code == "ghost" else JOB)
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "ggl")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code, "label": "job"})
    monkeypatch.setattr(server, "audit", _Audit())
    monkeypatch.setattr(server, "call_mc2_get", fake_get)
    monkeypatch.setattr(server, "call_mc2_send", fake_send)
    return SimpleNamespace(state=state, gets=gets, sends=sends)


def _est(eid, status="pending", build=None, total=0):
    return {"id": eid, "name": f"Estimate {eid}", "status": status, "on_schedule": True,
            "total": total, "build": build}


def test_registration(server):
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    assert "build_estimate" in main and "build_estimate" in server.MAIN_ONLY_TOOLS
    assert "build_estimate" not in read
    assert "get_estimate" in read and "get_estimate" in server.READ_ONLY_TOOLS


def test_refusals_send_nothing(server, pt, env, monkeypatch):
    env.state.estimates = [_est("e2")]
    assert "error" in pt.build_estimate("ggl-5241", "   ")
    assert "error" in pt.build_estimate("ghost", "brief")
    assert "error" in pt.build_estimate("ggl-5241", "brief", estimate_id="e2", new_estimate=True)
    assert "error" in pt.build_estimate("ggl-5241", "brief", estimate_id="elsewhere")
    # A job with estimates and no say in which one is refused, naming them.
    out = pt.build_estimate("ggl-5241", "brief")
    assert "error" in out and "e2" in out["error"]
    env.state.estimates = [_est("e2", status="approved")]
    assert "pending" in pt.build_estimate("ggl-5241", "brief", estimate_id="e2")["error"]
    env.state.estimates = [_est("e2", build={"state": "running"})]
    assert "running" in pt.build_estimate("ggl-5241", "brief", estimate_id="e2")["error"]
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access is for the team"))
    assert "error" in pt.build_estimate("ggl-5241", "brief", new_estimate=True)
    assert "error" in pt.get_estimate("ggl-5241")
    assert env.sends == []


def test_a_job_with_no_estimates_gets_its_first_and_the_build_starts(pt, env):
    out = pt.build_estimate("ggl-5241", "Munich pilot, template, two site videos")
    assert out["estimate_id"] == "est-new" and out["build"]["state"] == "running"
    assert [(m, p) for m, p, _ in env.sends] == [
        ("POST", "/api/estimates"),
        ("POST", "/api/scope-studio/sessions/by-estimate/est-new"),
        ("POST", "/api/scope-studio/sessions/sess-1/autonomous/start"),
    ]
    assert env.sends[0][2]["job_id"] == JOB
    assert env.sends[2][2] == {"goal": "Munich pilot, template, two site videos"}
    assert out["open_in_mission_control"].endswith(f"/jobs/{JOB}/scope?estimate=est-new")


def test_new_estimate_creates_another_even_when_some_exist(pt, env):
    env.state.estimates = [_est("e2")]
    out = pt.build_estimate("ggl-5241", "Paris site video", new_estimate=True, estimate_name="Paris")
    assert out["estimate_id"] == "est-new"
    assert env.sends[0] == ("POST", "/api/estimates", {"job_id": JOB, "name": "Paris"})


def test_building_on_an_existing_pending_estimate_creates_nothing(pt, env):
    env.state.estimates = [_est("e2")]
    pt.build_estimate("ggl-5241", "add a third site video", estimate_id="e2")
    assert [p for _, p, _ in env.sends][0] == "/api/scope-studio/sessions/by-estimate/e2"


def test_an_mc2_failure_is_an_error_not_success(pt, env):
    env.state.fail = "mc-2 refused starting the build (409): already running"
    out = pt.build_estimate("ggl-5241", "brief")
    assert "error" in out and "409" in out["error"]


def test_get_estimate_lists_then_reads_one(pt, env):
    env.state.estimates = [_est("e1", status="abandoned", total=348700),
                           _est("e2", total=348700, build={"state": "done", "turns_used": 3})]
    listing = pt.get_estimate("ggl-5241")
    assert [e["status"] for e in listing["estimates"]] == ["abandoned", "pending"]
    assert listing["estimates"][1]["build"]["state"] == "done"
    one = pt.get_estimate("ggl-5241", "e2")
    (phase,) = one["phases"]
    discovery, design = phase["activities"]
    assert discovery["roles"][0]["subtotal"] == 6000 and discovery["subtotal"] == 6000
    assert design["unpriced"] is True and phase["subtotal"] == 6000
    assert one["total"] == 348700
    assert all(not p.startswith("/api/scope-studio") for p in env.gets)
    assert env.sends == []
    assert "error" in pt.get_estimate("ggl-5241", "not-on-this-job")
