"""The Lens serves-active term resolves on the LIVE (substance) path (#342).

`score = recency × serves_active × layer_importance × status`. On the path
that actually runs — `load_spine_from_mc2` → `substance_row_to_element` — an
element's id is its row id (``<code>/<item>/vN``) but `serves` holds the
TARGET's `est_item_id`. The term looked links up by row id, so 0 of 163 live
links resolved and serves-active was a constant 0.35 for every non-framing,
non-deliverable element.

Two defects, two fixes, both pinned here:

  1. KEYING. `active_deliverable_ids` and `rank_elements`' index now carry the
     element's slot (`est_item_id`) beside its id.
  2. MEANING. Measured 2026-09-30: of 30 distinct live targets, 17 are estimate
     work items with no spine row at all and none is a Deliverables-layer row.
     So keying alone moves nothing; on MC-2 "serves an active item" reads as
     spec v04's "serves an OPEN work item" — an estimate item with no done
     bar, or a live unsealed spine card.

The rows below are copied from production (`ibx-5153-ai-campaign`, `ibx-5192-
platform-sales-readiness-summit`, 2026-09-30) with bodies shortened. The fake
client applies the `select` column list and the eq/in_ filters, so a column
the real query stops asking for fails here too.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import cp_engine.spine as spine
from cp_engine.spine import (
    _serves_active_term,
    active_deliverable_ids,
    load_spine_from_mc2,
    rank_elements,
    substance_row_to_element,
)

TODAY = date(2026, 9, 30)
MC_PID = "7d3c1f9e-5a1b-4c7e-9b1a-5192aa000001"   # public.projects.id
EST_ID = "e5192000-0000-4000-8000-000000000001"   # estimator.projects.id
PHASE = "a5192000-0000-4000-8000-0000000000p1"
CODE = "ibx-5192-platform-sales-readiness-summit"

# Real est_item_ids from production.
ACT_OPEN = "5fca0b9c-b1ae-4622-9b39-e187e63ff9ab"      # estimate activity, has a card
DLV_BARE = "336b6557-1ad8-4c88-b576-608a3c22603d"      # estimate deliverable, NO spine row
ACT_DONE = "0669796f-cf9e-41df-b0a5-c69ac3cb6daf"      # estimate activity, bar done
LEGACY = "ibx-5153/deliverable/five-concepts"          # markdown-era id: matches nothing


def _ctx(slug, *, serves, layer="Synthesis", card_kind="reference", v="v1"):
    """A `placement='context'` authored row, exactly as production shapes it."""
    return {
        "id": f"{CODE}/_authored/{slug}/{v}", "project_id": MC_PID,
        "project_code": CODE, "est_item_id": f"_authored/{slug}",
        "layer": layer, "placement": "context", "card_kind": card_kind,
        "status": "live", "version_label": v, "version_date": "2026-09-28",
        "framing": slug.replace("-", " "), "body": "b", "serves": serves,
        "sources": [], "rel_path": None, "actor": "inferred",
    }


def _card(est_item_id, framing, *, layer="Activity", card_kind="activity"):
    """A `placement='item'` row keyed by an estimate slot (a work-item card)."""
    return {
        "id": f"{CODE}/{est_item_id}/v1", "project_id": MC_PID,
        "project_code": CODE, "est_item_id": est_item_id, "layer": layer,
        "placement": "item", "card_kind": card_kind, "status": "live",
        "version_label": "v1", "version_date": "2026-09-28",
        "framing": framing, "body": "b", "serves": [], "sources": [],
        "rel_path": None, "actor": "inferred",
    }


def _estimate_tables(*, done_bar_for=(ACT_DONE,)):
    item = dict(phase_id=PHASE, short_description=None, library_item_id=None)
    return {
        ("estimator", "projects"): [{
            "id": EST_ID, "mc_project_id": MC_PID, "name": "SRS",
            "status": "approved", "on_schedule": True,
            "created_at": "2026-06-01T00:00:00Z",
        }],
        ("public", "projects"): [{"id": MC_PID, "start_date": "2026-06-08"}],
        ("estimator", "phases"): [{
            "id": PHASE, "project_id": EST_ID, "name": "Build",
            "overview": None, "position": 1,
        }],
        ("estimator", "phase_activities"): [
            {"id": ACT_OPEN, "name": "Kickoff", "position": 1, **item},
            {"id": ACT_DONE, "name": "Interviews", "position": 2, **item},
        ],
        ("estimator", "phase_deliverables"): [
            {"id": DLV_BARE, "name": "Main stage deck", "position": 1, **item},
        ],
        ("estimator", "schedule_items"): [
            {"id": f"bar-{w}", "project_id": EST_ID, "phase_id": PHASE,
             "label": w, "start_week": 1, "duration": 1, "position": 1,
             "item_type": "activity", "emphasis": None, "work_item_id": w,
             "work_item_kind": "activity", "done": w in done_bar_for}
            for w in (ACT_OPEN, ACT_DONE)
        ],
    }


class _Q:
    def __init__(self, client, schema, table):
        self.c, self.key = client, (schema, table)
        self.cols: list[str] = []
        self.filters: list = []

    def select(self, cols):
        self.c.selects[self.key] = cols
        self.cols = [x.strip() for x in cols.split(",")]
        return self

    def eq(self, col, val):
        self.filters.append((col, {val}))
        return self

    def in_(self, col, vals):
        self.filters.append((col, set(vals)))
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        if self.key == ("public", "spine_relations"):
            return SimpleNamespace(data=list(self.c.edges))
        if self.key[0] == "estimator" and self.c.estimate_fails:
            raise RuntimeError("42501 permission denied for schema estimator")
        self.c.reads.append(self.key)
        rows = self.c.tables.get(self.key, [])
        out = []
        for r in rows:
            if all(r.get(col) in vals for col, vals in self.filters):
                missing = [k for k in self.cols if k not in r]
                assert not missing, f"{self.key} row lacks selected {missing}"
                out.append({k: r[k] for k in self.cols})
        return SimpleNamespace(data=out)


class _Client:
    def __init__(self, rows, *, tables=None, edges=(), estimate_fails=False,
                 _schema="public"):
        self.tables = dict(tables or {})
        self.tables[("public", "spine_substance")] = rows
        self.edges = list(edges)
        self.estimate_fails = estimate_fails
        self.selects: dict = {}
        self.reads: list = []
        self._schema = _schema

    def schema(self, name):
        view = _Client.__new__(_Client)
        view.__dict__ = dict(self.__dict__)
        view._schema = name
        return view

    def table(self, name):
        return _Q(self, self._schema, name)


def _load(rows, **kw):
    return {e.title: e for e in load_spine_from_mc2(
        _Client(rows, tables=_estimate_tables(), **kw), CODE)}


def _term(els):
    t = tuple(els.values())
    active = active_deliverable_ids(t)
    return {title: _serves_active_term(e, active) for title, e in els.items()}


# --- the control ------------------------------------------------------------


def test_serving_an_open_work_item_is_warm_on_the_live_path():
    """CONTROL: against the unfixed code every one of these is 0.35 — the row
    id never equals an est_item_id, and nothing read the estimate."""
    rows = [
        _ctx("routed-to-a-bare-estimate-deliverable", serves=[DLV_BARE]),
        _ctx("routed-to-an-activity-card", serves=[ACT_OPEN]),
        _card(ACT_OPEN, "Kickoff and direction for Geoff"),
    ]
    term = _term(_load(rows))
    assert term["routed to a bare estimate deliverable"] == 1.0
    assert term["routed to an activity card"] == 1.0


def test_the_warm_link_moves_the_ranking():
    rows = [
        _ctx("routed-input", serves=[DLV_BARE]),
        _ctx("unrouted-input", serves=[]),
    ]
    scored, _ = rank_elements(tuple(_load(rows).values()), today=TODAY)
    score = {e.title: s for s, e in scored}
    assert score["routed input"] > score["unrouted input"]


# --- what is NOT open -------------------------------------------------------


def test_a_done_work_item_and_a_legacy_id_stay_cold():
    rows = [
        _ctx("routed-to-done-work", serves=[ACT_DONE]),
        _ctx("routed-to-a-markdown-era-id", serves=[LEGACY]),
    ]
    term = _term(_load(rows))
    assert term["routed to done work"] == 0.35
    assert term["routed to a markdown era id"] == 0.35


def test_a_sealed_card_is_not_open_work():
    """A card with an active `absorbed_by` edge is historical (spec v04 §3)."""
    rows = [
        _ctx("sealed-note", layer="Note", card_kind="deliverable", serves=[]),
        _ctx("routed-to-it", serves=["_authored/sealed-note"]),
    ]
    edge = {"kind": "absorbed_by", "from_item_id": "_authored/sealed-note",
            "to_item_id": "_authored/deck"}
    assert _term(_load(rows, edges=[edge]))["routed to it"] == 0.35
    assert _term(_load(rows))["routed to it"] == 1.0  # unsealed twin: open


# --- the keying fix, on its own ---------------------------------------------


def test_active_deliverables_are_keyed_by_slot_as_well_as_id():
    """No estimate needed: a live Deliverables-layer row is found by the
    `est_item_id` a sibling's `serves` names (the pure-graph half of #342)."""
    deck = substance_row_to_element(_ctx(
        "carol-framework-map", layer="Deliverables", card_kind="deliverable",
        serves=[], v="v2"))
    note = substance_row_to_element(_ctx(
        "janet-note", serves=["_authored/carol-framework-map"]))
    active = active_deliverable_ids((deck, note))
    assert deck.id in active and "_authored/carol-framework-map" in active
    assert _serves_active_term(note, active) == 1.0


def test_open_work_item_slots_is_pure_and_follows_the_done_bar():
    rows = [_card(ACT_OPEN, "k"), _card(ACT_DONE, "i"),
            _ctx("a-reference", serves=[])]
    got = spine.open_work_item_slots(
        rows, sealed=set(), estimate_item_ids={ACT_OPEN, ACT_DONE, DLV_BARE},
        done_map={ACT_OPEN: False, ACT_DONE: True})
    assert got == {ACT_OPEN, DLV_BARE}   # reference rows are not work


# --- the read ---------------------------------------------------------------


def test_no_serves_means_no_estimate_read_and_select_is_explicit():
    client = _Client([_ctx("a", serves=[])], tables=_estimate_tables())
    load_spine_from_mc2(client, CODE)
    assert not [k for k in client.reads if k[0] == "estimator"]
    cols = client.selects[("public", "spine_substance")]
    assert "*" not in cols and "card_kind" in cols and "est_item_id" in cols


def test_a_failed_estimate_read_degrades_loudly(capsys):
    """Cards still count; bare estimate slots cannot — and stderr says so."""
    rows = [
        _ctx("to-bare", serves=[DLV_BARE]),
        _ctx("to-card", serves=[ACT_OPEN]),
        _card(ACT_OPEN, "Kickoff"),
    ]
    term = _term(_load(rows, estimate_fails=True))
    assert term["to bare"] == 0.35 and term["to card"] == 1.0
    assert "estimate unreadable" in capsys.readouterr().err
