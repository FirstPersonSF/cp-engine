"""`list_unrouted_emails`, `route_unrouted_email`, `list_inbox_cards`,
`resolve_inbox_card`, `resolve_spine_proposals`, `restore_spine_element`, and
`list_spine_relations(status=...)`.

mc-2 #475 retires the Inputs, Suggestions and Archive tabs. Four things were
reachable only there. Drew, 2026-11-13, before merging it: "let's build the
cp tools first to be safe." What these tests pin:

- the two lists are on `/mcp/read`; the four deciders are on `/mcp` only;
- every decision is a call to mc-2 (it owns the rule), and a read tool never
  reaches the sender;
- a refusal sends nothing: a non-member, an unknown project, a card that is
  not waiting on this project, a missing framing, both route and dismiss;
- routing passes the resolved project id as well as the code;
- a framed card is filed on a work item that is on this job's estimate, the
  guessed one by default;
- a retired card (no live version) is never "restored";
- an mc-2 failure comes back as an error, not as success.

    python -m pytest prototypes/hosted-mcp/test_retired_tab_verbs.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_map_verbs import ESTIMATE, _Audit, _Store  # noqa: E402


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


EMAILS = [
    {"id": "em-1", "thread_id": "t1", "from_addr": "janet@client.example", "to_addr": "cp@example.com",
     "subject": "Brief v10", "delta": "Attached is the brief.", "received_at": "2026-11-12T10:00:00+00:00",
     "reason": "no code", "attempted_code": None, "status": "unrouted", "created_at": "2026-11-12T10:00:01+00:00"},
]
CARDS = [
    {"id": "ibx-5153/meeting-9/card-1", "project_id": "p-ibx", "project_code": "ibx-5153",
     "source_ref": "meeting 9", "raw_distillation": "x" * 900, "guessed_est_item_id": "act-dev",
     "guessed_type": "activity", "status": "proposed", "framing": None,
     "created_at": "2026-11-10T00:00:00+00:00", "updated_at": "2026-11-10T00:00:00+00:00"},
    {"id": "ibx-5153/meeting-9/card-2", "project_id": "p-ibx", "project_code": "ibx-5153",
     "source_ref": "meeting 9", "raw_distillation": "short", "guessed_est_item_id": None,
     "guessed_type": None, "status": "proposed", "framing": None,
     "created_at": "2026-11-09T00:00:00+00:00", "updated_at": "2026-11-09T00:00:00+00:00"},
]
SUBSTANCE = [
    {"id": "s1", "project_id": "p-ibx", "est_item_id": "_authored/old-note", "framing": "Old note",
     "layer": "Note", "status": "superseded", "archived": True},
    {"id": "s2", "project_id": "p-ibx", "est_item_id": "_authored/old-note", "framing": "Old note v2",
     "layer": "Note", "status": "live", "archived": True},
    {"id": "s3", "project_id": "p-ibx", "est_item_id": "_authored/retired-dup", "framing": "Retired duplicate",
     "layer": "Source material", "status": "superseded", "archived": True},
    {"id": "s4", "project_id": "p-ibx", "est_item_id": "_authored/live-card", "framing": "Old live card",
     "layer": "Note", "status": "live", "archived": False},
]


@pytest.fixture
def env(server, monkeypatch):
    store = _Store({"spine_substance": SUBSTANCE})
    audit = _Audit()
    gets: list[str] = []
    sends: list[tuple[str, str, dict | None]] = []
    state = SimpleNamespace(fail_send=None, send_data={})

    def fake_get(path, what):
        gets.append(path)
        if path == "/api/unrouted-emails":
            return {"ok": True, "data": [dict(r) for r in EMAILS]}
        if path.endswith("/inbox"):
            return {"ok": True, "data": [dict(r) for r in CARDS]}
        return {"ok": False, "reason": f"unexpected GET {path}"}

    def fake_send(method, path, body, what):
        sends.append((method, path, body))
        if state.fail_send:
            return {"ok": False, "reason": state.fail_send}
        if path.endswith("/relations/confirm"):
            return {"ok": True, "data": {"confirmed": [i for i in body["ids"] if i != "gone"]}}
        if path.endswith("/relations/dismiss"):
            return {"ok": True, "data": {"dismissed": list(body["ids"])}}
        if path.endswith("/frame-promote"):
            return {"ok": True, "data": {"run_id": "run-1", "status": "running"}}
        if path.endswith("/dismiss") and "unrouted" in path:
            return {"ok": True, "data": {"status": "dismissed", "id": "em-1"}}
        return {"ok": True, "data": dict(state.send_data)}

    import cp_engine.estimate as estimate_mod

    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p-ibx")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "ibx")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code, "label": "job"})
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "call_mc2_get", fake_get)
    monkeypatch.setattr(server, "call_mc2_send", fake_send)
    monkeypatch.setattr(estimate_mod, "fetch_estimate", lambda c, pid: ESTIMATE)
    return SimpleNamespace(store=store, audit=audit, gets=gets, sends=sends, state=state)


READERS = ("list_unrouted_emails", "list_inbox_cards")
WRITERS = ("route_unrouted_email", "resolve_inbox_card", "resolve_spine_proposals",
           "restore_spine_element")


def test_registration(server):
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    for verb in WRITERS:
        assert verb in main and verb in server.MAIN_ONLY_TOOLS and verb not in read
    for verb in READERS:
        assert verb in read and verb in server.READ_ONLY_TOOLS


def test_a_non_member_is_refused_and_nothing_is_sent(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access is for the team"))
    for call in (
        lambda: pt.list_unrouted_emails(),
        lambda: pt.route_unrouted_email("em-1", "ibx-5153"),
        lambda: pt.list_inbox_cards("ibx-5153"),
        lambda: pt.resolve_inbox_card("ibx-5153", CARDS[0]["id"], "dismiss"),
        lambda: pt.resolve_spine_proposals("ibx-5153", ["r1"], "confirm"),
        lambda: pt.restore_spine_element("ibx-5153", "old note"),
    ):
        assert "error" in call()
    assert env.gets == [] and env.sends == []


# ── unrouted emails ──────────────────────────────────────────────────


def test_the_email_list_reads_and_never_sends(pt, env):
    out = pt.list_unrouted_emails()
    assert out["count"] == 1
    row = out["emails"][0]
    assert (row["email_id"], row["from"], row["subject"]) == ("em-1", "janet@client.example", "Brief v10")
    assert row["preview"] == "Attached is the brief." and row["why_unrouted"] == "no code"
    assert env.sends == []


def test_routing_sends_the_code_and_the_resolved_project_id(pt, env):
    out = pt.route_unrouted_email("em-1", "ibx-5153")
    assert out["outcome"] == "routed" and out["caller"] == "drew@example.com"
    assert env.sends == [("POST", "/api/unrouted-emails/em-1/route",
                          {"code": "ibx-5153", "project_id": "p-ibx"})]


def test_dismissing_an_email_names_no_project(pt, env):
    out = pt.route_unrouted_email("em-1", dismiss=True)
    assert out["outcome"] == "dismissed" and out["already_handled"] is False
    assert env.sends == [("PATCH", "/api/unrouted-emails/em-1/dismiss", None)]


@pytest.mark.parametrize("kwargs", [
    {"email_id": "em-1"},                                       # neither
    {"email_id": "em-1", "project_code": "ibx-5153", "dismiss": True},  # both
    {"email_id": "", "project_code": "ibx-5153"},               # no email
])
def test_route_refuses_an_unclear_call(pt, env, kwargs):
    assert "error" in pt.route_unrouted_email(**kwargs)
    assert env.sends == []


def test_routing_to_an_unknown_project_sends_nothing(pt, env):
    out = pt.route_unrouted_email("em-1", "ghost")
    assert "no project or initiative resolves" in out["error"]
    assert env.sends == []


def test_an_mc2_failure_is_an_error_not_a_success(pt, env):
    env.state.fail_send = "mc-2 refused routing the email (422): no such project"
    out = pt.route_unrouted_email("em-1", "ibx-5153")
    assert "422" in out["error"] and "outcome" not in out


# ── to-frame cards ───────────────────────────────────────────────────


def test_the_card_list_previews_and_one_card_reads_in_full(pt, env):
    out = pt.list_inbox_cards("ibx-5153")
    assert out["count"] == 2
    first = out["cards"][0]
    assert len(first["preview"]) == 600 and first["text_len"] == 900 and "text" not in first
    assert first["guessed_work_item"] == "act-dev"
    one = pt.list_inbox_cards("ibx-5153", card_id=CARDS[0]["id"])
    assert one["count"] == 1 and len(one["cards"][0]["text"]) == 900
    assert env.sends == []


def test_framing_files_the_card_on_the_guessed_work_item(pt, env):
    out = pt.resolve_inbox_card("ibx-5153", CARDS[0]["id"], "frame", framing="  What Janet asked for.  ")
    assert out["outcome"] == "framing" and out["run_id"] == "run-1"
    assert out["work_item"] == {"id": "act-dev", "name": "Campaign System Development", "kind": "activity"}
    assert env.sends == [("POST", "/api/spine/inbox/ibx-5153/meeting-9/card-1/frame-promote",
                          {"framing": "What Janet asked for.", "est_item_id": "act-dev", "kind": "activity"})]


def test_framing_on_a_named_deliverable_sends_its_kind(pt, env):
    out = pt.resolve_inbox_card("ibx-5153", CARDS[1]["id"], "frame", framing="The ask.",
                                work_item="Campaign System")
    assert out["work_item"]["id"] == "del-campaign"
    assert env.sends[0][2]["kind"] == "deliverable"


@pytest.mark.parametrize("args,kwargs,needle", [
    ((CARDS[0]["id"], "frame"), {}, "framing"),                      # no framing
    ((CARDS[1]["id"], "frame"), {"framing": "x y"}, "work_item"),     # no guess, none named
    ((CARDS[0]["id"], "frame"), {"framing": "x y", "work_item": "Nothing Like This"}, ""),
    ((CARDS[0]["id"], "promote"), {}, "frame or dismiss"),            # bad outcome
    (("other/card", "dismiss"), {}, "no waiting card"),               # not on this project
])
def test_a_card_refusal_sends_nothing(pt, env, args, kwargs, needle):
    out = pt.resolve_inbox_card("ibx-5153", *args, **kwargs)
    assert "error" in out and needle in out["error"]
    assert env.sends == []


def test_dismissing_a_card_writes_nothing_to_the_spine(pt, env):
    out = pt.resolve_inbox_card("ibx-5153", CARDS[1]["id"], "dismiss")
    assert out["outcome"] == "dismissed"
    assert env.sends == [("POST", "/api/spine/inbox/ibx-5153/meeting-9/card-2/dismiss", None)]


# ── suggested links ──────────────────────────────────────────────────


def test_confirming_reports_what_was_not_waiting(pt, env):
    out = pt.resolve_spine_proposals("ibx-5153", ["r1", " gone ", ""], "confirm")
    assert out["outcome"] == "confirmed" and out["ids"] == ["r1"] and out["not_waiting"] == ["gone"]
    assert env.sends == [("POST", "/api/spine/ibx-5153/relations/confirm", {"ids": ["r1", "gone"]})]


def test_dismissing_goes_to_the_dismiss_route(pt, env):
    out = pt.resolve_spine_proposals("ibx-5153", ["r1"], "dismiss")
    assert out["outcome"] == "dismissed" and out["ids"] == ["r1"]
    assert env.sends[0][1] == "/api/spine/ibx-5153/relations/dismiss"


@pytest.mark.parametrize("ids,outcome", [(["r1"], "accept"), ([], "confirm"), (["  "], "dismiss")])
def test_a_proposal_refusal_sends_nothing(pt, env, ids, outcome):
    assert "error" in pt.resolve_spine_proposals("ibx-5153", ids, outcome)
    assert env.sends == []


def test_list_spine_relations_filters_by_the_status_asked_for(server, env, monkeypatch):
    rels = [
        {"id": "r-a", "project_id": "p-ibx", "kind": "informs", "from_item_id": "a", "to_item_id": "b",
         "note": None, "status": "active", "source": "human", "created_by": "x", "created_at": "1"},
        {"id": "r-p", "project_id": "p-ibx", "kind": "informs", "from_item_id": "c", "to_item_id": "b",
         "note": None, "status": "proposed", "source": "auto", "created_by": "x", "created_at": "2"},
    ]
    seen: list[tuple[str, str]] = []

    class _Q:
        def __init__(self, rows):
            self.rows = rows

        def select(self, cols):
            return self

        def eq(self, c, v):
            seen.append((c, v))
            self.rows = [r for r in self.rows if r.get(c) == v]
            return self

        def in_(self, c, vals):
            self.rows = [r for r in self.rows if r.get(c) in vals]
            return self

        def order(self, c):
            return self

        def execute(self):
            return SimpleNamespace(data=[dict(r) for r in self.rows])

    client = SimpleNamespace(table=lambda name: _Q(rels if name == "spine_relations" else []))
    monkeypatch.setattr(server, "user_client", lambda: client)
    fn = server.mcp_server._tool_manager.get_tool("list_spine_relations").fn
    assert [r["relation_id"] for r in fn(project_code="ibx-5153")["relations"]] == ["r-a"]
    proposed = fn(project_code="ibx-5153", status="proposed")["relations"]
    assert [(r["relation_id"], r["status"]) for r in proposed] == [("r-p", "proposed")]
    assert "error" in fn(project_code="ibx-5153", status="pending")


# ── archived cards ───────────────────────────────────────────────────


def test_no_key_lists_what_is_archived_and_changes_nothing(pt, env):
    out = pt.restore_spine_element("ibx-5153")
    assert out["restored"] is False
    assert [(c["key"], c["restorable"]) for c in out["archived"]] == [
        ("_authored/old-note", True), ("_authored/retired-dup", False)]
    # the live version's title, not an older one's
    assert out["archived"][0]["framing"] == "Old note v2"
    assert env.sends == []


def test_restoring_unarchives_the_whole_card_through_mc2(pt, env):
    out = pt.restore_spine_element("ibx-5153", "old note")
    assert out["restored"] is True and out["card"]["key"] == "_authored/old-note"
    assert env.sends == [("PATCH", "/api/spine/ibx-5153/substance",
                          {"ids": ["_authored/old-note"], "archived": False})]


def test_a_retired_card_is_not_restored(pt, env):
    out = pt.restore_spine_element("ibx-5153", "_authored/retired-dup")
    assert out["restored"] is False and "retired, not archived" in out["error"]
    assert env.sends == []


def test_a_live_card_is_not_an_archived_one(pt, env):
    out = pt.restore_spine_element("ibx-5153", "Old live card")
    assert out["restored"] is False and "no archived card matches" in out["error"]
    assert env.sends == []


def test_an_ambiguous_title_is_refused_with_the_candidates(pt, env):
    env.store.tables["spine_substance"].append(
        {"id": "s5", "project_id": "p-ibx", "est_item_id": "_authored/old-note-b", "framing": "Old note b",
         "layer": "Note", "status": "live", "archived": True})
    out = pt.restore_spine_element("ibx-5153", "old note")
    assert out["restored"] is False and len(out["archived"]) == 2
    assert env.sends == []


# ── the senders themselves ───────────────────────────────────────────


def test_the_read_helper_cannot_send_and_the_send_helper_refuses_other_methods(server, monkeypatch):
    monkeypatch.setattr(server, "MC2_API_BASE", "https://mc2.invalid")
    monkeypatch.setattr(server, "caller_jwt", lambda: "tok")
    calls: list[tuple[str, str]] = []

    def record(method):
        def go(url, **kw):
            calls.append((method, url))
            assert kw["headers"] == {"Authorization": "Bearer tok"}
            return SimpleNamespace(status_code=200, text="", json=lambda: {"ok": 1})
        return go

    for m in ("get", "post", "patch", "delete", "put"):
        monkeypatch.setattr(server.httpx, m, record(m))
    assert server.call_mc2_get("/api/x", "x") == {"ok": True, "data": {"ok": 1}}
    assert server.call_mc2_send("POST", "/api/y", {"a": 1}, "y")["ok"] is True
    assert server.call_mc2_send("PATCH", "/api/z", None, "z")["ok"] is True
    assert server.call_mc2_send("DELETE", "/api/z", None, "z")["ok"] is False
    assert calls == [("get", "https://mc2.invalid/api/x"), ("post", "https://mc2.invalid/api/y"),
                     ("patch", "https://mc2.invalid/api/z")]


def test_an_mc2_error_status_is_reported(server, monkeypatch):
    monkeypatch.setattr(server, "MC2_API_BASE", "https://mc2.invalid")
    monkeypatch.setattr(server, "caller_jwt", lambda: "tok")
    monkeypatch.setattr(server.httpx, "post",
                        lambda url, **kw: SimpleNamespace(status_code=404, text="gone", json=lambda: {}))
    out = server.call_mc2_send("POST", "/api/y", {}, "the thing")
    assert out["ok"] is False and out["status"] == 404 and "the thing" in out["reason"]
