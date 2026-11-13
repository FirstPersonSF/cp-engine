"""`get_map_state`, `set_work_item_deliverable`, `set_map_mark`,
`set_phase_agreement`, `set_meeting_work_item` — the Spine Map's verbs.

Drew, 2026-11-13: "Let's build the cp tools for the map." The day before,
about 80 rows on ibx-5153 went into three stores by direct SQL from one
session. What these tests pin:

- the four writers are on `/mcp` only and the reader is on `/mcp/read` too;
- every write goes through the CALLER's client and names the caller;
- a pairing is suggested unless a person confirmed it, changes status in
  place, and is deleted on `removed`;
- a mark keeps what was not mentioned and is removed when nothing is left;
- a status targets a deliverable and a shared date targets a card or a
  meeting, and the two cannot be mixed in one call;
- a meeting is attached through Mission Control, never by a column write;
- refusals are plain and write nothing.

    python -m pytest prototypes/hosted-mcp/test_map_verbs.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase  # noqa: E402


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


def _item(id_, phase, kind, name):
    return EstimateItem(id=id_, phase_id=phase, kind=kind, name=name,
                        short_description=None, position=0, library_item_id=None)


ESTIMATE = Estimate(
    id="est-1", mc_project_id="p-ibx", name="Estimate 1",
    phases=(
        EstimatePhase(id="ph-1", name="Message Strategy", overview=None, position=0, estimate_id="est-1",
                      items=(_item("act-arch", "ph-1", "activity", "Narrative Messaging Architecture"),
                             _item("del-system-1", "ph-1", "deliverable", "Messaging System"))),
        EstimatePhase(id="ph-2", name="Campaign Reset - Phase 2", overview=None, position=1, estimate_id="est-1",
                      items=(_item("act-dev", "ph-2", "activity", "Campaign System Development"),
                             _item("del-system-2", "ph-2", "deliverable", "Messaging System"),
                             _item("del-campaign", "ph-2", "deliverable", "Campaign System"))),
    ),
)


class _Store:
    """PostgREST stand-in: filtered SELECTs, recorded INSERT/UPDATE/DELETE,
    and a `schema()` hop so `estimator.phases` is its own table."""

    def __init__(self, tables, schema="public", root=None):
        self._schema = schema
        self._root = root or self
        if root is None:
            self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
            self.writes: list[tuple[str, str, dict]] = []
            self.n = 0

    def schema(self, name):
        return _Store(None, schema=name, root=self._root)

    def table(self, name):
        root = self._root
        key = name if self._schema == "public" else f"{self._schema}.{name}"
        state = {"rows": list(root.tables.get(key, [])), "write": None}

        class _Q:
            def select(self, cols):
                assert "*" not in cols, f"select('*') on {key}"
                return self

            def eq(self, c, v):
                state["rows"] = [r for r in state["rows"] if r.get(c) == v]
                return self

            def in_(self, c, vals):
                state["rows"] = [r for r in state["rows"] if r.get(c) in vals]
                return self

            def insert(self, row, **kw):
                root.n += 1
                saved = {"id": f"R{root.n}", **row}
                root.writes.append((key, "insert", dict(row)))
                root.tables.setdefault(key, []).append(saved)
                state["rows"], state["write"] = [saved], "insert"
                return self

            def update(self, patch):
                state["write"] = ("update", dict(patch))
                return self

            def delete(self):
                state["write"] = ("delete", {})
                return self

            def execute(self):
                w = state["write"]
                if isinstance(w, tuple) and w[0] == "update":
                    for r in state["rows"]:
                        r.update(w[1])
                        root.writes.append((key, "update", {"id": r.get("id"), **w[1]}))
                if isinstance(w, tuple) and w[0] == "delete":
                    for r in state["rows"]:
                        root.writes.append((key, "delete", {"id": r.get("id")}))
                        root.tables[key].remove(r)
                return SimpleNamespace(data=[dict(r) for r in state["rows"]])

        return _Q()


class _Audit(list):
    def __call__(self, client, tool, args, rows):
        self.append((tool, dict(args), rows))


AGREEMENTS = [
    {"id": "ag-4", "project_id": "p-ibx", "kind": "sow", "number": "SOW-4", "title": None,
     "signed_date": "2026-03-13", "estimate_id": "est-1", "parent_agreement_id": None},
    {"id": "ag-6", "project_id": "p-ibx", "kind": "sow", "number": "SOW-6", "title": "Phase 2 reset",
     "signed_date": "2026-10-22", "estimate_id": None, "parent_agreement_id": None},
]
MEETINGS = [
    {"id": "m-review", "project_id": "p-ibx", "recording_id": 111, "title": "AI Campaign Review ",
     "meeting_date": "2026-11-12T21:00:00+00:00", "work_item_id": None, "ignored": False, "duplicate_of": None},
    {"id": "m-team-a", "project_id": "p-ibx", "recording_id": 222, "title": "ibx_5153_ai_campaign_team",
     "meeting_date": "2026-11-06T17:00:00+00:00", "work_item_id": "act-dev", "ignored": False, "duplicate_of": None},
    {"id": "m-team-b", "project_id": "p-ibx", "recording_id": 333, "title": "ibx_5153_ai_campaign_team",
     "meeting_date": "2026-11-10T17:00:00+00:00", "work_item_id": None, "ignored": False, "duplicate_of": None},
    {"id": "m-ignored", "project_id": "p-ibx", "recording_id": 444, "title": "ibx_5153_ai_campaign_team",
     "meeting_date": "2026-11-11T17:00:00+00:00", "work_item_id": None, "ignored": True, "duplicate_of": None},
]


@pytest.fixture
def env(server, monkeypatch):
    store = _Store({
        "agreements": AGREEMENTS,
        "estimator.phases": [{"id": "ph-1", "agreement_id": None}, {"id": "ph-2", "agreement_id": None}],
        "work_item_deliverables": [],
        "spine_map_marks": [],
        "fathom_meetings": MEETINGS,
    })
    audit = _Audit()
    patches: list[tuple[int, str | None]] = []

    def fake_patch(recording_id, work_item_id):
        patches.append((recording_id, work_item_id))
        return {"ok": True, "auto_promote": None}

    import cp_engine.estimate as estimate_mod

    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p-ibx")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "ibx")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code, "label": "job"})
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "resolve_live_element_id",
                        lambda c, pid, key: ("_authored/recap", None) if "recap" in key.lower()
                        else (None, None))
    monkeypatch.setattr(server, "call_mc2_set_meeting_work_item", fake_patch)
    monkeypatch.setattr(estimate_mod, "fetch_estimate",
                        lambda c, pid: None if pid == "p-none" else ESTIMATE)
    return SimpleNamespace(store=store, audit=audit, patches=patches)


def _writes(env, table):
    return [(op, row) for t, op, row in env.store.writes if t == table]


# ── registration and gating ──────────────────────────────────────────

WRITERS = ("set_work_item_deliverable", "set_map_mark", "set_phase_agreement",
           "set_meeting_work_item")


def test_registration(server):
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    for verb in WRITERS:
        assert verb in main and verb in server.MAIN_ONLY_TOOLS and verb not in read
    assert "get_map_state" in read and "get_map_state" in server.READ_ONLY_TOOLS


def test_a_non_member_is_refused_and_nothing_is_written(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access is for the team"))
    calls = [
        lambda: pt.get_map_state("ibx-5153"),
        lambda: pt.set_work_item_deliverable("ibx-5153", "act-arch", "del-system-1"),
        lambda: pt.set_map_mark("ibx-5153", "recap", shared_on="2026-11-03"),
        lambda: pt.set_phase_agreement("ibx-5153", "reset", "SOW-6"),
        lambda: pt.set_meeting_work_item("ibx-5153", "review", "act-dev"),
    ]
    for call in calls:
        assert "error" in call()
    assert env.store.writes == [] and env.patches == []


def test_an_unknown_project_is_refused(pt, env):
    for out in (pt.get_map_state("ghost"),
                pt.set_work_item_deliverable("ghost", "a", "b"),
                pt.set_map_mark("ghost", "recap", shared_on="2026-11-03"),
                pt.set_phase_agreement("ghost", "reset", "SOW-6"),
                pt.set_meeting_work_item("ghost", "review", "act-dev")):
        assert "no project or initiative resolves" in out["error"]
    assert env.store.writes == []


# ── pairings ─────────────────────────────────────────────────────────


def test_a_pairing_is_suggested_by_default_and_names_nobody(pt, env):
    out = pt.set_work_item_deliverable("ibx-5153", "Campaign System Development", "Campaign System")
    assert out["status"] == "suggested" and out["was"] is None and out["changed"] is True
    (op, row), = _writes(env, "work_item_deliverables")
    assert op == "insert"
    assert (row["project_id"], row["work_item_id"], row["deliverable_id"]) == ("p-ibx", "act-dev", "del-campaign")
    assert row["status"] == "suggested" and row["confirmed_by"] is None


def test_confirming_changes_the_row_in_place_and_names_the_caller(pt, env):
    pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign")
    out = pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign", status="confirmed")
    assert (out["was"], out["status"]) == ("suggested", "confirmed")
    ops = _writes(env, "work_item_deliverables")
    assert [op for op, _ in ops] == ["insert", "update"]
    assert ops[1][1]["confirmed_by"] == "drew@example.com" and ops[1][1]["confirmed_at"]
    assert len(env.store.tables["work_item_deliverables"]) == 1


def test_one_work_item_may_serve_two_deliverables(pt, env):
    pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign")
    pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-system-2")
    assert {r["deliverable_id"] for r in env.store.tables["work_item_deliverables"]} == {
        "del-campaign", "del-system-2"}


def test_removed_deletes_the_pairing_and_is_quiet_when_there_is_none(pt, env):
    pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign")
    out = pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign", status="removed")
    assert out["changed"] is True and env.store.tables["work_item_deliverables"] == []
    again = pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign", status="removed")
    assert again["changed"] is False
    assert [op for op, _ in _writes(env, "work_item_deliverables")] == ["insert", "delete"]


def test_a_name_on_two_phases_is_refused_and_nothing_is_written(pt, env):
    out = pt.set_work_item_deliverable("ibx-5153", "act-dev", "Messaging System")
    assert "2 deliverables match" in out["error"] and "del-system-2" in out["error"]
    ok = pt.set_work_item_deliverable("ibx-5153", "act-dev", "Campaign Reset - Phase 2 / Messaging System")
    assert ok["deliverable"]["id"] == "del-system-2"
    assert len(_writes(env, "work_item_deliverables")) == 1


def test_a_bad_status_or_a_swapped_pair_is_refused(pt, env):
    assert "status is one of" in pt.set_work_item_deliverable(
        "ibx-5153", "act-dev", "del-campaign", status="sure")["error"]
    assert "not a work item" in pt.set_work_item_deliverable(
        "ibx-5153", "Campaign System", "act-dev")["error"]
    assert env.store.writes == []


# ── marks ────────────────────────────────────────────────────────────


def test_a_shared_date_marks_a_card_and_names_the_caller(pt, env):
    out = pt.set_map_mark("ibx-5153", "the recap", shared_on="2026-11-03", shared_to="Janet Noe")
    assert out["item"] == "_authored/recap" and out["mark"]["shared_on"] == "2026-11-03"
    (op, row), = _writes(env, "spine_map_marks")
    assert op == "insert" and row["project_id"] == "p-ibx" and row["item_key"] == "_authored/recap"
    assert row["set_by"] == "drew@example.com" and row["status_key"] is None


def test_a_later_call_keeps_what_it_does_not_mention(pt, env):
    pt.set_map_mark("ibx-5153", "recap", shared_on="2026-11-04", shared_to="Janet Noe")
    out = pt.set_map_mark("ibx-5153", "recap", shared_on="2026-11-03")
    assert out["mark"] == {"shared_on": "2026-11-03", "shared_to": "Janet Noe",
                           "column": None, "status": None, "note": None}
    assert [op for op, _ in _writes(env, "spine_map_marks")] == ["insert", "update"]


def test_clearing_the_last_thing_removes_the_mark(pt, env):
    pt.set_map_mark("ibx-5153", "recap", column="Background")
    out = pt.set_map_mark("ibx-5153", "recap", column="")
    assert out["removed"] is True and out["mark"] is None
    assert env.store.tables["spine_map_marks"] == []


def test_a_status_marks_a_deliverable_in_the_maps_words(pt, env):
    out = pt.set_map_mark("ibx-5153", "Campaign System", status="Replaced")
    assert out["item"] == "del-campaign" and out["mark"]["status"] == "Replaced"
    (_, row), = _writes(env, "spine_map_marks")
    assert row["item_key"] == "del-campaign" and row["status_key"] == "replaced"


def test_a_meeting_can_be_marked_by_its_key(pt, env):
    out = pt.set_map_mark("ibx-5153", "meeting:m-review", shared_on="2026-11-12")
    assert out["item"] == "meeting:m-review" and out["name"] == "AI Campaign Review"


def test_mark_refusals_write_nothing(pt, env):
    mixed = pt.set_map_mark("ibx-5153", "Campaign System", status="Done", shared_on="2026-11-03")
    assert "Make two calls" in mixed["error"]
    assert "say what to record" in pt.set_map_mark("ibx-5153", "recap")["error"]
    assert "YYYY-MM-DD" in pt.set_map_mark("ibx-5153", "recap", shared_on="last week")["error"]
    assert "no live card" in pt.set_map_mark("ibx-5153", "nothing like this", column="Shared")["error"]
    assert "Done, Replaced, Not approved" in pt.set_map_mark(
        "ibx-5153", "Campaign System", status="finished")["error"]
    assert env.store.writes == []


# ── a phase's agreement ──────────────────────────────────────────────


def test_a_phase_names_its_agreement_and_can_follow_its_estimate_again(pt, env):
    out = pt.set_phase_agreement("ibx-5153", "reset", "sow-6")
    assert out["phase"]["id"] == "ph-2" and out["grouped_under"] == "SOW-6"
    assert _writes(env, "estimator.phases") == [("update", {"id": "ph-2", "agreement_id": "ag-6"})]
    back = pt.set_phase_agreement("ibx-5153", "reset", "")
    assert back["follows_its_estimate"] is True
    assert _writes(env, "estimator.phases")[-1] == ("update", {"id": "ph-2", "agreement_id": None})


def test_an_unknown_phase_or_agreement_is_refused(pt, env):
    assert "no phase on this job" in pt.set_phase_agreement("ibx-5153", "nope", "SOW-6")["error"]
    assert "it has: SOW-4, SOW-6" in pt.set_phase_agreement("ibx-5153", "reset", "SOW-9")["error"]
    assert env.store.writes == []


# ── meetings ─────────────────────────────────────────────────────────


def test_a_meeting_is_attached_through_mission_control_never_by_a_column_write(pt, env):
    out = pt.set_meeting_work_item("ibx-5153", "campaign review", "Campaign System Development")
    assert out["meeting"] == {"id": "m-review", "title": "AI Campaign Review", "date": "2026-11-12"}
    assert out["work_item"]["id"] == "act-dev"
    assert env.patches == [(111, "act-dev")]
    assert _writes(env, "fathom_meetings") == []


def test_a_repeated_title_needs_its_date_and_ignored_meetings_do_not_count(pt, env):
    out = pt.set_meeting_work_item("ibx-5153", "ai_campaign_team", "act-dev")
    assert "2 meetings match" in out["error"] and "m-ignored" not in out["error"]
    ok = pt.set_meeting_work_item("ibx-5153", "2026-11-10 team", "act-dev")
    assert ok["meeting"]["id"] == "m-team-b" and env.patches == [(333, "act-dev")]


def test_a_meeting_may_sit_on_a_deliverable_and_can_be_detached(pt, env):
    on = pt.set_meeting_work_item("ibx-5153", "m-review", "Campaign System")
    assert on["work_item"]["kind"] == "deliverable"
    off = pt.set_meeting_work_item("ibx-5153", "222", "")
    assert off["work_item"] is None and off["was"] == "act-dev"
    assert env.patches == [(111, "del-campaign"), (222, None)]


def test_a_refused_patch_is_reported_not_swallowed(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "call_mc2_set_meeting_work_item",
                        lambda rid, wid: {"ok": False, "reason": "mc-2 refused the update (404)"})
    out = pt.set_meeting_work_item("ibx-5153", "m-review", "act-dev")
    assert out["ok"] is False and "404" in out["reason"]
    assert env.audit[-1] == ("set_meeting_work_item",
                             {"project_code": "ibx-5153", "meeting": "m-review",
                              "work_item": "Campaign System Development"}, 0)


# ── the read ─────────────────────────────────────────────────────────


def test_state_reads_back_what_the_verbs_wrote(pt, env):
    pt.set_work_item_deliverable("ibx-5153", "act-dev", "del-campaign", status="confirmed")
    pt.set_map_mark("ibx-5153", "Campaign System", status="Done")
    pt.set_map_mark("ibx-5153", "recap", shared_on="2026-11-03")
    pt.set_phase_agreement("ibx-5153", "reset", "SOW-6")
    out = pt.get_map_state("ibx-5153")
    reset = next(p for p in out["phases"] if p["id"] == "ph-2")
    assert reset["agreement"] == "SOW-6" and reset["agreement_named_on_phase"] is True
    campaign = next(d for d in reset["deliverables"] if d["id"] == "del-campaign")
    assert campaign["status"] == "Done"
    assert campaign["work_items"] == [{"work_item": "Campaign System Development",
                                       "work_item_id": "act-dev", "status": "confirmed"}]
    assert [m["item"] for m in out["marks"]] == ["_authored/recap"]
    assert out["work_items_serving_nothing"] == [
        {"id": "act-arch", "name": "Narrative Messaging Architecture", "phase": "Message Strategy"}]
    assert out["meetings"] == {"on_a_work_item": 1, "on_no_work_item": 2}


def test_state_on_a_job_with_no_estimate_says_so(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "resolve_project_id", lambda c, code: "p-none")
    out = pt.get_map_state("init-1")
    assert out["phases"] == [] and "no estimate" in out["note"]
