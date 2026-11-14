"""`set_effort`, and `get_map_state` / `set_map_mark` on a workstream with no
estimate.

A workstream with no estimate (an initiative, e.g. 1pi-9005-mission-control)
has no deliverables. Its Map lines are its efforts: rows of mc-2's
`project_milestones`. Before this, `get_map_state` told every such workstream
"this job has no estimate, so the Map has no lines", which was false as soon
as one effort existed, and nothing in cp could name, rename or close one.
What these tests pin:

- `get_map_state` lists the efforts with a count of the live, non-archived
  cards that name each in `serves`, and says how a card is placed on one;
- with no effort named it says the Map shows one default line, and how to
  name an effort;
- `set_effort` creates after the last effort, changes only what differs,
  and goes through Mission Control under the caller's identity;
- every refusal is plain and sends nothing: a workstream that has an
  estimate, an effort of another workstream, a name already taken, a bad
  status, nothing to change;
- a status mark lands on an effort by id or name when there is no estimate.

    python -m pytest prototypes/hosted-mcp/test_effort_verbs.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_map_verbs import ESTIMATE, _Audit, _Store  # noqa: E402

PID = "p-init"
PATH = f"/api/jobs/{PID}/milestones"


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


def _effort(id_, name, position, status="open", overview=None):
    return {"id": id_, "project_id": PID, "name": name, "overview": overview,
            "position": position, "due_date": None, "status": status,
            "created_at": "2026-11-13T00:00:00+00:00", "updated_at": "2026-11-13T00:00:00+00:00"}


EFFORTS = [
    # Out of order on purpose: the verbs sort by position.
    _effort("ef-map", "Spine Map", 3),
    _effort("ef-learn", "Learning from projects", 1, overview="Lessons and lookouts"),
]
SUBSTANCE = [
    # Two versions of one card: counted once, by its live row.
    {"id": "s1", "project_id": PID, "est_item_id": "_authored/plan", "status": "superseded",
     "archived": False, "serves": ["ef-map"]},
    {"id": "s2", "project_id": PID, "est_item_id": "_authored/plan", "status": "live",
     "archived": False, "serves": ["ef-map"]},
    {"id": "s3", "project_id": PID, "est_item_id": "_authored/tracker", "status": "live",
     "archived": False, "serves": ["ef-map", "ef-learn"]},
    # Archived, superseded, routed elsewhere, routed nowhere: none count.
    {"id": "s4", "project_id": PID, "est_item_id": "_authored/old", "status": "live",
     "archived": True, "serves": ["ef-map"]},
    {"id": "s5", "project_id": PID, "est_item_id": "_authored/gone", "status": "superseded",
     "archived": False, "serves": ["ef-learn"]},
    {"id": "s6", "project_id": PID, "est_item_id": "_authored/other", "status": "live",
     "archived": False, "serves": ["_authored/plan"]},
    {"id": "s7", "project_id": PID, "est_item_id": "_authored/loose", "status": "live",
     "archived": False, "serves": None},
    # Another project's card naming the same id is not this project's.
    {"id": "s8", "project_id": "p-ibx", "est_item_id": "_authored/elsewhere", "status": "live",
     "archived": False, "serves": ["ef-map"]},
]


@pytest.fixture
def env(server, monkeypatch):
    store = _Store({"spine_substance": SUBSTANCE})
    audit = _Audit()
    gets: list[str] = []
    sends: list[tuple[str, str, dict | None]] = []
    state = SimpleNamespace(efforts=[dict(e) for e in EFFORTS], fail_get=None, fail_send=None)

    def fake_get(path, what):
        gets.append(path)
        if state.fail_get:
            return {"ok": False, "reason": state.fail_get}
        if path == PATH:
            return {"ok": True, "data": [dict(e) for e in state.efforts]}
        return {"ok": False, "reason": f"unexpected GET {path}"}

    def fake_send(method, path, body, what):
        sends.append((method, path, body))
        if state.fail_send:
            return {"ok": False, "reason": state.fail_send}
        if method == "POST" and path == PATH:
            return {"ok": True, "data": {**_effort("ef-new", body["name"], body["position"]), **body}}
        if method == "PATCH" and path.startswith(PATH + "/"):
            row = next(e for e in state.efforts if e["id"] == path.rsplit("/", 1)[1])
            return {"ok": True, "data": {**row, **body}}
        return {"ok": False, "reason": f"unexpected {method} {path}"}

    import cp_engine.estimate as estimate_mod

    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: {"1pi-9005": PID, "ibx-5153": "p-ibx"}.get(code))
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "1pi")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code, "label": "initiative"})
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "call_mc2_get", fake_get)
    monkeypatch.setattr(server, "call_mc2_send", fake_send)
    # Only the job has an estimate; the initiative has none.
    monkeypatch.setattr(estimate_mod, "fetch_estimate",
                        lambda c, pid: ESTIMATE if pid == "p-ibx" else None)
    return SimpleNamespace(store=store, audit=audit, gets=gets, sends=sends, state=state)


def test_registration(server):
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    assert "set_effort" in main and "set_effort" in server.MAIN_ONLY_TOOLS
    assert "set_effort" not in read
    assert "get_map_state" in read


def test_a_non_member_is_refused_and_nothing_is_sent(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access is for the team"))
    assert "error" in pt.set_effort("1pi-9005", name="Hosted skills")
    assert env.gets == [] and env.sends == []


# ── get_map_state on a workstream with no estimate ───────────────────


def test_the_map_state_lists_the_efforts_and_their_cards(pt, env):
    """Fails on the code before this change: it returned no `efforts` and the
    note "this job has no estimate, so the Map has no lines"."""
    out = pt.get_map_state("1pi-9005")
    assert "error" not in out
    assert [(e["id"], e["name"], e["position"], e["cards"]) for e in out["efforts"]] == [
        ("ef-learn", "Learning from projects", 1, 1),
        ("ef-map", "Spine Map", 3, 2),
    ]
    first = out["efforts"][0]
    assert first["overview"] == "Lessons and lookouts" and first["status"] == "open"
    assert "due_date" in first
    assert "no lines" not in out["note"]
    assert "efforts" in out["note"] and "set_spine_element(serves=[" in out["note"]
    # The keys every other reader relies on are still there.
    assert out["phases"] == [] and "meetings" in out and "marks" in out
    assert env.gets == [PATH] and env.sends == [] and env.store.writes == []


def test_with_no_effort_the_map_state_names_the_default_line(pt, env):
    env.state.efforts = []
    out = pt.get_map_state("1pi-9005")
    assert out["efforts"] == []
    assert "no lines" not in out["note"]
    assert "one default line" in out["note"] and "set_effort" in out["note"]
    assert env.sends == []


def test_a_job_with_an_estimate_gets_no_efforts_and_reads_no_milestones(pt, env):
    out = pt.get_map_state("ibx-5153")
    assert "efforts" not in out and "note" not in out
    assert len(out["phases"]) == 2
    assert env.gets == []


def test_efforts_that_cannot_be_read_are_reported_not_hidden(pt, env):
    env.state.fail_get = "mc-2 did not respond within 20s reading the efforts"
    out = pt.get_map_state("1pi-9005")
    assert out["efforts"] == [] and "did not respond" in out["efforts_error"]
    assert "could not be read" in out["note"] and "default line" not in out["note"]


# ── set_effort: a new effort ─────────────────────────────────────────


def test_a_new_effort_goes_after_the_last_one(pt, env):
    out = pt.set_effort("1pi-9005", name="  Hosted   skills ", overview=" What a hosted session can load ")
    assert out["created"] is True
    assert env.sends == [("POST", PATH, {
        "name": "Hosted skills", "position": 4, "status": "open",
        "overview": "What a hosted session can load"})]
    assert out["effort"]["id"] == "ef-new" and out["effort"]["position"] == 4
    assert out["caller"] == "drew@example.com"
    assert "set_spine_element(serves=[" in out["note"]
    assert env.audit[-1][0] == "set_effort" and env.audit[-1][2] == 1


def test_the_first_effort_starts_at_zero_and_a_position_can_be_named(pt, env):
    env.state.efforts = []
    pt.set_effort("1pi-9005", name="First")
    pt.set_effort("1pi-9005", name="Second", position=7, status="done")
    assert [s[2] for s in env.sends] == [
        {"name": "First", "position": 0, "status": "open"},
        {"name": "Second", "position": 7, "status": "done"},
    ]


# ── set_effort: changing one ─────────────────────────────────────────


def test_an_update_sends_only_what_changes(pt, env):
    out = pt.set_effort("1pi-9005", effort_id="ef-map", status="done")
    assert env.sends == [("PATCH", f"{PATH}/ef-map", {"status": "done"})]
    assert out["created"] is False and out["changed"] == ["status"]
    assert out["effort"]["status"] == "done" and out["effort"]["name"] == "Spine Map"


def test_rename_reorder_and_clearing_the_overview(pt, env):
    pt.set_effort("1pi-9005", effort_id="ef-learn", name="Learning", position=0, overview="")
    assert env.sends == [("PATCH", f"{PATH}/ef-learn",
                          {"name": "Learning", "position": 0, "overview": None})]


def test_an_update_that_changes_nothing_writes_nothing(pt, env):
    out = pt.set_effort("1pi-9005", effort_id="ef-map", name="Spine Map", status="open", position=3)
    assert "error" not in out and out["changed"] == []
    assert env.sends == [] and env.audit[-1][2] == 0


# ── set_effort: refusals ─────────────────────────────────────────────


@pytest.mark.parametrize("code,kwargs,needle", [
    ("ibx-5153", {"name": "An effort"}, "has an estimate"),                 # lines come from the scope
    ("ibx-5153", {"effort_id": "ef-map", "status": "done"}, "has an estimate"),
    ("1pi-9005", {"effort_id": "ef-elsewhere", "status": "done"}, "no effort"),  # not on this project
    ("1pi-9005", {"effort_id": "ef-map"}, "say what to change"),            # nothing passed
    ("1pi-9005", {}, "needs a name"),                                       # new, unnamed
    ("1pi-9005", {"status": "done"}, "needs a name"),
    ("1pi-9005", {"name": "spine  MAP"}, "already on this workstream"),     # name taken
    ("1pi-9005", {"name": "X", "status": "closed"}, "open, done"),          # not a status
    ("1pi-9005", {"effort_id": "ef-map", "position": -1}, "position"),
    ("1pi-9005", {"name": "n" * 201}, "200"),
    ("ghost", {"name": "An effort"}, ""),                                   # no such project
])
def test_a_refusal_sends_nothing(pt, env, code, kwargs, needle):
    out = pt.set_effort(code, **kwargs)
    assert "error" in out and needle in out["error"]
    assert env.sends == [] and env.store.writes == []


def test_a_failed_send_is_an_error_and_not_a_created_effort(pt, env):
    env.state.fail_send = "mc-2 answered 500"
    out = pt.set_effort("1pi-9005", name="Hosted skills")
    assert out["error"] == "mc-2 answered 500" and "created" not in out


def test_efforts_that_cannot_be_read_stop_the_write(pt, env):
    env.state.fail_get = "could not reach mc-2"
    out = pt.set_effort("1pi-9005", name="Hosted skills")
    assert "could not reach" in out["error"] and env.sends == []


# ── set_map_mark: a status on an effort ──────────────────────────────


def test_a_status_mark_lands_on_an_effort_by_name_or_id(pt, env):
    out = pt.set_map_mark("1pi-9005", "spine map", status="In progress", note="Drew, 11-13")
    assert "error" not in out
    assert out["item"] == "ef-map" and out["name"] == "Spine Map"
    assert out["mark"]["status"] == "In progress"
    table, op, row = env.store.writes[-1]
    assert (table, op) == ("spine_map_marks", "insert")
    assert row["project_id"] == PID and row["item_key"] == "ef-map" and row["status_key"]
    again = pt.set_map_mark("1pi-9005", "ef-learn", status="Done")
    assert again["item"] == "ef-learn"


@pytest.mark.parametrize("item,needle", [
    ("Nothing like this", "no effort"),
    ("ef-elsewhere", "no effort"),
    ("", "name the effort"),
])
def test_a_status_mark_on_no_effort_is_refused(pt, env, item, needle):
    out = pt.set_map_mark("1pi-9005", item, status="Done")
    assert needle in out["error"] and env.store.writes == []


def test_a_status_mark_before_any_effort_says_to_name_one(pt, env):
    env.state.efforts = []
    out = pt.set_map_mark("1pi-9005", "Mission Control", status="Done")
    assert "set_effort" in out["error"] and env.store.writes == []


def test_a_deliverable_status_on_a_job_still_reads_the_estimate(pt, env):
    out = pt.set_map_mark("ibx-5153", "Campaign System", status="Done")
    assert out["item"] == "del-campaign" and env.gets == []
