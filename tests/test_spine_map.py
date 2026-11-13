"""`cp_engine.spine_map`: what a person says about the Spine Map.

The validation behind the hosted Map verbs. What these pin:

- a name that fits two phases is refused with both, never resolved to the
  first (ibx-5153 has two "Messaging System" deliverables);
- a confirmed pairing always names who confirmed it and when;
- a mark keeps what was not mentioned, clears what was passed as "", and
  goes away when nothing is left;
- the state read attaches names and reports rows that point nowhere.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cp_engine import spine_map
from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine_map import MapError


def _item(id_, phase, kind, name, pos=0):
    return EstimateItem(id=id_, phase_id=phase, kind=kind, name=name,
                        short_description=None, position=pos, library_item_id=None)


def _estimate():
    p1 = EstimatePhase(id="ph-1", name="Message Strategy", overview=None, position=0, estimate_id="est-1",
                       items=(
                           _item("act-arch", "ph-1", "activity", "Narrative Messaging Architecture"),
                           _item("act-refined", "ph-1", "activity", "Refined Campaign Directions"),
                           _item("del-system-1", "ph-1", "deliverable", "Messaging System"),
                           _item("del-workshop", "ph-1", "deliverable", "Messaging Workshop"),
                       ))
    p2 = EstimatePhase(id="ph-2", name="Campaign Reset - Phase 2", overview=None, position=1, estimate_id="est-1",
                       items=(
                           _item("act-framework", "ph-2", "activity", "Messaging framework"),
                           _item("del-system-2", "ph-2", "deliverable", "Messaging System"),
                           _item("del-campaign", "ph-2", "deliverable", "Campaign System"),
                       ))
    return Estimate(id="est-1", mc_project_id="p-1", name="Estimate 1", phases=(p1, p2))


AGREEMENTS = [
    {"id": "ag-4", "kind": "sow", "number": "SOW-4", "title": None, "signed_date": "2026-03-13",
     "estimate_id": "est-1", "parent_agreement_id": None},
    {"id": "ag-co", "kind": "change_order", "number": "CO-1", "title": "Change Order 1",
     "signed_date": "2026-04-20", "estimate_id": None, "parent_agreement_id": "ag-4"},
    {"id": "ag-6", "kind": "sow", "number": "SOW-6", "title": "Phase 2 reset",
     "signed_date": "2026-10-22", "estimate_id": None, "parent_agreement_id": None},
]


# ── naming a work item or a deliverable ──────────────────────────────


def test_an_item_is_found_by_id_exact_name_or_a_distinct_part():
    est = _estimate()
    assert spine_map.find_item(est, "act-arch", "activity").id == "act-arch"
    assert spine_map.find_item(est, "messaging WORKSHOP", "deliverable").id == "del-workshop"
    assert spine_map.find_item(est, "Refined", "activity").id == "act-refined"


def test_a_name_on_two_phases_is_refused_with_both_candidates():
    with pytest.raises(MapError) as exc:
        spine_map.find_item(_estimate(), "Messaging System", "deliverable")
    said = str(exc.value)
    assert "2 deliverables match" in said
    assert "del-system-1" in said and "del-system-2" in said
    assert "Message Strategy" in said and "Campaign Reset - Phase 2" in said


def test_phase_slash_name_settles_a_repeated_name():
    est = _estimate()
    got = spine_map.find_item(est, "Campaign Reset - Phase 2 / Messaging System", "deliverable")
    assert got.id == "del-system-2"


def test_an_exact_name_beats_a_longer_name_that_contains_it():
    est = _estimate()
    # "Messaging framework" contains nothing ambiguous; "Messaging" alone does.
    assert spine_map.find_item(est, "Messaging framework", "activity").id == "act-framework"
    with pytest.raises(MapError, match="2 work items match"):
        spine_map.find_item(est, "Messaging", "activity")


def test_the_wrong_kind_is_named_not_reported_as_missing():
    with pytest.raises(MapError, match="is a deliverable on this job, not a work item"):
        spine_map.find_item(_estimate(), "Campaign System", "activity")
    with pytest.raises(MapError, match="no work item on this job matches"):
        spine_map.find_item(_estimate(), "Nope", "activity")


def test_an_exact_name_of_the_other_kind_beats_a_partial_match():
    est = Estimate(id="e", mc_project_id="p", name="E", phases=(
        EstimatePhase(id="ph", name="Reset", overview=None, position=0, estimate_id="e", items=(
            _item("act-dev", "ph", "activity", "Campaign System Development"),
            _item("del-campaign", "ph", "deliverable", "Campaign System"),
        )),
    ))
    with pytest.raises(MapError, match="is a deliverable on this job, not a work item"):
        spine_map.find_item(est, "Campaign System", "activity")
    assert spine_map.find_item(est, "Campaign System", "deliverable").id == "del-campaign"
    assert spine_map.find_item(est, "System Dev", "activity").id == "act-dev"


def test_no_estimate_is_said_plainly():
    with pytest.raises(MapError, match="no estimate"):
        spine_map.find_item(None, "anything", "deliverable")
    with pytest.raises(MapError, match="no estimate"):
        spine_map.find_phase(None, "anything")


def test_a_phase_and_an_agreement_are_found_by_name_number_or_id():
    est = _estimate()
    assert spine_map.find_phase(est, "reset").id == "ph-2"
    assert spine_map.find_phase(est, "ph-1").id == "ph-1"
    assert spine_map.find_agreement(AGREEMENTS, "sow-6")["id"] == "ag-6"
    assert spine_map.find_agreement(AGREEMENTS, "Change Order")["id"] == "ag-co"
    with pytest.raises(MapError, match="it has: SOW-4, CO-1, SOW-6"):
        spine_map.find_agreement(AGREEMENTS, "SOW-9")


def test_a_change_order_resolves_to_its_parent_and_a_cycle_does_not_hang():
    assert spine_map.root_agreement(AGREEMENTS, "ag-co")["id"] == "ag-4"
    assert spine_map.root_agreement(AGREEMENTS, "ag-6")["id"] == "ag-6"
    assert spine_map.root_agreement(AGREEMENTS, None) is None
    loop = [{"id": "a", "parent_agreement_id": "b"}, {"id": "b", "parent_agreement_id": "a"}]
    assert spine_map.root_agreement(loop, "a")["id"] in ("a", "b")


# ── pairings ─────────────────────────────────────────────────────────


def test_a_suggested_pairing_names_nobody():
    row = spine_map.pairing_row(project_id="p-1", work_item_id="a", deliverable_id="d",
                                status="suggested", confirmed_by="drew@x", now_iso="2026-11-13T00:00:00Z")
    assert row["status"] == "suggested"
    assert row["confirmed_by"] is None and row["confirmed_at"] is None


def test_a_confirmed_pairing_names_who_and_when():
    row = spine_map.pairing_row(project_id="p-1", work_item_id="a", deliverable_id="d",
                                status="confirmed", confirmed_by="drew@x",
                                now_iso="2026-11-13T00:00:00Z", note=" said in chat ")
    assert (row["confirmed_by"], row["confirmed_at"]) == ("drew@x", "2026-11-13T00:00:00Z")
    assert row["note"] == "said in chat"
    with pytest.raises(MapError, match="names who confirmed it"):
        spine_map.pairing_row(project_id="p-1", work_item_id="a", deliverable_id="d",
                              status="confirmed", now_iso="2026-11-13T00:00:00Z")
    with pytest.raises(MapError, match="suggested or confirmed"):
        spine_map.pairing_row(project_id="p-1", work_item_id="a", deliverable_id="d", status="sure")


# ── marks ────────────────────────────────────────────────────────────


def test_a_mark_keeps_what_was_not_mentioned():
    before = {"shared_on": "2026-11-04", "shared_to": "Janet", "column_key": None,
              "status_key": None, "note": "sign-off date"}
    after = spine_map.merge_mark(before, shared_on="2026-11-03", set_by="drew@x")
    assert after == {"shared_on": "2026-11-03", "shared_to": "Janet", "column_key": None,
                     "status_key": None, "note": "sign-off date", "set_by": "drew@x"}


def test_a_column_and_a_status_are_said_in_the_maps_own_words():
    assert spine_map.merge_mark(None, column="Background")["column_key"] == "bg"
    assert spine_map.merge_mark(None, column="work sessions")["column_key"] == "work"
    assert spine_map.merge_mark(None, status="Not approved")["status_key"] == "not_approved"
    assert spine_map.merge_mark(None, status="replaced")["status_key"] == "replaced"
    with pytest.raises(MapError, match="Background, Work sessions, Shared, Feedback"):
        spine_map.merge_mark(None, column="middle")
    with pytest.raises(MapError, match="Done, Replaced, Not approved"):
        spine_map.merge_mark(None, status="finished")


def test_a_guessed_date_is_refused():
    with pytest.raises(MapError, match="YYYY-MM-DD"):
        spine_map.merge_mark(None, shared_on="early November")
    with pytest.raises(MapError, match="YYYY-MM-DD"):
        spine_map.merge_mark(None, shared_on="11/03/2026")


def test_an_empty_string_clears_one_thing_and_an_empty_mark_goes():
    before = {"shared_on": "2026-11-03", "shared_to": "Janet", "column_key": "bg",
              "status_key": None, "note": None}
    after = spine_map.merge_mark(before, shared_on="")
    assert after["shared_on"] is None and after["shared_to"] is None
    assert after["column_key"] == "bg"
    assert spine_map.merge_mark(after, column="") is None
    assert spine_map.merge_mark(None, note="only a note") is None


# ── the state read ───────────────────────────────────────────────────


class _Client:
    def __init__(self, tables):
        self.tables = tables
        self._schema = "public"

    def schema(self, name):
        c = _Client(self.tables)
        c._schema = name
        return c

    def table(self, name):
        key = name if self._schema == "public" else f"{self._schema}.{name}"
        rows = [dict(r) for r in self.tables.get(key, [])]

        class _Q:
            def select(self, cols):
                assert "*" not in cols
                return self

            def eq(self, c, v):
                rows[:] = [r for r in rows if r.get(c) == v]
                return self

            def in_(self, c, vals):
                rows[:] = [r for r in rows if r.get(c) in vals]
                return self

            def execute(self):
                return SimpleNamespace(data=rows)

        return _Q()


def _state(**over):
    tables = {
        "agreements": [dict(a, project_id="p-1") for a in AGREEMENTS],
        "estimator.phases": [{"id": "ph-1", "agreement_id": None}, {"id": "ph-2", "agreement_id": "ag-6"}],
        "work_item_deliverables": [
            {"id": "w1", "project_id": "p-1", "work_item_id": "act-arch", "deliverable_id": "del-system-1",
             "status": "confirmed", "confirmed_by": "drew@x", "confirmed_at": "t", "note": None},
            {"id": "w2", "project_id": "p-1", "work_item_id": "act-refined", "deliverable_id": "del-system-1",
             "status": "suggested", "confirmed_by": None, "confirmed_at": None, "note": None},
            {"id": "w3", "project_id": "p-1", "work_item_id": "act-gone", "deliverable_id": "del-system-1",
             "status": "suggested", "confirmed_by": None, "confirmed_at": None, "note": None},
        ],
        "spine_map_marks": [
            {"id": "m1", "project_id": "p-1", "item_key": "del-workshop", "shared_on": None, "shared_to": None,
             "column_key": None, "status_key": "done", "note": None, "set_by": "drew@x"},
            {"id": "m2", "project_id": "p-1", "item_key": "_authored/recap", "shared_on": "2026-11-03",
             "shared_to": "Janet", "column_key": None, "status_key": None, "note": None, "set_by": "drew@x"},
            {"id": "m3", "project_id": "p-1", "item_key": "_authored/scope", "shared_on": None, "shared_to": None,
             "column_key": "bg", "status_key": None, "note": None, "set_by": "drew@x"},
            {"id": "m4", "project_id": "p-1", "item_key": "del-removed", "shared_on": None, "shared_to": None,
             "column_key": None, "status_key": "replaced", "note": None, "set_by": "drew@x"},
        ],
    }
    tables.update(over)
    return spine_map.read_state(_Client(tables), "p-1", _estimate())


def test_state_groups_each_phase_under_the_agreement_it_names_or_follows():
    by_name = {p["name"]: p for p in _state()["phases"]}
    assert by_name["Message Strategy"]["agreement"] == "SOW-4"
    assert by_name["Message Strategy"]["agreement_named_on_phase"] is False
    assert by_name["Campaign Reset - Phase 2"]["agreement"] == "SOW-6"
    assert by_name["Campaign Reset - Phase 2"]["agreement_named_on_phase"] is True


def test_state_attaches_names_to_pairings_and_statuses():
    p1 = _state()["phases"][0]
    arch = next(w for w in p1["work_items"] if w["id"] == "act-arch")
    assert arch["serves"] == [{"deliverable": "Messaging System", "deliverable_id": "del-system-1",
                               "status": "confirmed"}]
    system = next(d for d in p1["deliverables"] if d["id"] == "del-system-1")
    assert [w["work_item"] for w in system["work_items"]] == [
        "Narrative Messaging Architecture", "Refined Campaign Directions"]
    workshop = next(d for d in p1["deliverables"] if d["id"] == "del-workshop")
    assert workshop["status"] == "Done" and system["status"] is None


def test_state_lists_what_is_missing_and_what_points_nowhere():
    out = _state()
    assert out["work_items_serving_nothing"] == [
        {"id": "act-framework", "name": "Messaging framework", "phase": "Campaign Reset - Phase 2"}]
    kinds = sorted((s["kind"], s.get("item") or s.get("work_item_id")) for s in out["stale"])
    assert kinds == [("mark", "del-removed"), ("pairing", "act-gone")]


def test_state_reports_card_marks_in_the_maps_words_oldest_first():
    assert _state()["marks"] == [
        {"item": "_authored/recap", "shared_on": "2026-11-03", "shared_to": "Janet",
         "column": None, "note": None, "set_by": "drew@x"},
        {"item": "_authored/scope", "shared_on": None, "shared_to": None,
         "column": "Background", "note": None, "set_by": "drew@x"},
    ]


def test_state_with_no_estimate_is_empty_not_an_error():
    out = spine_map.read_state(_Client({"agreements": [dict(a, project_id="p-1") for a in AGREEMENTS]}),
                               "p-1", None)
    assert out["phases"] == [] and len(out["agreements"]) == 3
