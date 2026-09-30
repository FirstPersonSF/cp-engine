"""Routing proposes the feeds edge (#174) — review-gated, date-guarded.

The rule: a source routed to a deliverable's slot, or to an activity with an
ACTIVE edge to a deliverable, gets a PROPOSED `informs` edge to that
deliverable. Never when the document arrived after the work (#270), never
when the pair was already asked (including a dismissal), never onto a card
whose layer disagrees with its deliverable kind (#177).
"""

from __future__ import annotations

from types import SimpleNamespace

import cp_engine
from cp_engine.feeds_propose import (
    CONFIDENCE_CHAIN,
    CONFIDENCE_DIRECT,
    FEEDS_PROPOSE_COLUMNS,
    propose_feeds,
    propose_on_route,
    render_report,
    write_proposals,
)
from cp_engine.stub_sweep import Stub, postdates


def test_the_worktree_code_is_what_runs():
    # pyproject's pythonpath=src must win over an editable install elsewhere.
    assert "/src/cp_engine/" in cp_engine.__file__


def _dlv(eid="dlv", date="2026-08-16", layer="Deliverables"):
    return {"id": f"p/{eid}/v2", "est_item_id": eid, "framing": "SRS deck",
            "layer": layer, "card_kind": "deliverable", "placement": "item",
            "status": "live", "archived": False, "version_label": "v2",
            "version_date": date, "serves": [], "sources": []}


def _act(eid="act", date="2026-07-10"):
    return {"id": f"p/{eid}/v1", "est_item_id": eid, "framing": "Kickoff",
            "layer": "Activity", "card_kind": "activity", "placement": "item",
            "status": "live", "archived": False, "version_label": "v1",
            "version_date": date, "serves": [], "sources": []}


def _src(eid="src", serves=("act",), date="2026-07-05", asset=None, **kw):
    row = {"id": f"p/{eid}/v1", "est_item_id": eid, "framing": f"Doc {eid}",
           "layer": "Source material", "card_kind": "attachment",
           "placement": "context", "status": "live", "archived": False,
           "version_label": "v1", "version_date": date,
           "serves": list(serves),
           "sources": [{"id": asset, "type": "rag_asset"}] if asset else []}
    row.update(kw)
    return row


def _edge(src, dst, kind="informs", status="active"):
    return {"kind": kind, "from_item_id": src, "to_item_id": dst, "status": status}


A2D = [_edge("act", "dlv")]


# ── what gets proposed ─────────────────────────────────────────────────────


def test_chain_routing_proposes_informs_to_the_deliverable():
    props, skipped = propose_feeds([_src(), _act(), _dlv()], A2D)
    assert [(p.from_item_id, p.to_item_id, p.via) for p in props] == [
        ("src", "dlv", "act")]
    assert props[0].confidence == CONFIDENCE_CHAIN
    assert "Kickoff" in props[0].note
    assert skipped == []


def test_direct_routing_to_a_deliverable_slot_proposes():
    props, _ = propose_feeds([_src(serves=("dlv",)), _dlv()], [])
    assert [(p.from_item_id, p.to_item_id, p.via) for p in props] == [
        ("src", "dlv", "")]
    assert props[0].confidence == CONFIDENCE_DIRECT


def test_confidence_stays_below_the_inbox_batch_confirm_threshold():
    # mc-2 SuggestionsView "Confirm all >=80%" must not sweep these in.
    assert CONFIDENCE_DIRECT < 0.8 and CONFIDENCE_CHAIN < 0.8


def test_bare_estimate_slot_proposes_nothing():
    props, skipped = propose_feeds([_src(serves=("uuid-slot",)), _dlv()], [])
    assert props == [] and skipped == []


def test_activity_with_no_active_edge_proposes_nothing():
    # A PROPOSED activity->deliverable edge is not a human judgement yet.
    rows = [_src(), _act(), _dlv()]
    assert propose_feeds(rows, [])[0] == []
    assert propose_feeds(rows, [_edge("act", "dlv", status="proposed")])[0] == []


def test_work_cards_and_stakeholders_are_not_sources():
    rows = [
        _act("act2", "2026-07-01") | {"serves": ["act"]},
        _src("person", layer="Stakeholders", card_kind="reference"),
        _act(), _dlv(),
    ]
    assert propose_feeds(rows, A2D)[0] == []


def test_only_scopes_to_the_elements_just_routed():
    rows = [_src("a"), _src("b"), _act(), _dlv()]
    props, _ = propose_feeds(rows, A2D, only=["b"])
    assert [p.from_item_id for p in props] == ["b"]


# ── the #270 guard ────────────────────────────────────────────────────────


def test_document_newer_than_the_activity_is_held_back():
    # The ibx-5192 shape: docs from 07-20 routed to a 06-27 debrief that feeds
    # the live deck. Proposing would turn a bulk route into plausible edges.
    rows = [_src(date="2026-07-21", asset="a1"), _act(date="2026-06-27"), _dlv()]
    props, skipped = propose_feeds(rows, A2D, source_dates={"a1": "2026-07-20"})
    assert props == []
    assert [(s.reason, s.via) for s in skipped] == [("postdates", "act")]
    assert "activity 2026-06-27" in skipped[0].detail


def test_document_newer_than_the_deliverable_is_held_back():
    rows = [_src(serves=("dlv",), date="2026-09-01"), _dlv(date="2026-08-16")]
    props, skipped = propose_feeds(rows, [])
    assert props == [] and [s.reason for s in skipped] == ["postdates"]


def test_document_arrival_beats_the_cards_backfilled_date():
    # #274: the card was minted 08-17 by a backfill; the document landed 07-01.
    rows = [_src(date="2026-08-17", asset="a1"), _act(date="2026-07-10"), _dlv()]
    props, _ = propose_feeds(rows, A2D, source_dates={"a1": "2026-07-01"})
    assert [p.evidence_date for p in props] == ["2026-07-01"]


def test_undated_source_is_held_back_not_guessed():
    rows = [_src(date=None), _act(), _dlv()]
    props, skipped = propose_feeds(rows, A2D)
    assert props == [] and [s.reason for s in skipped] == ["undated"]


def test_the_guard_is_the_one_stub_sweep_uses():
    assert postdates("2026-07-20", "2026-06-27") is True
    assert postdates("2026-06-27", "2026-06-27") is False
    assert postdates("", "2026-06-27") is False
    st = Stub("s", "S", 10, _doc_date="2026-07-20", _target_date="2026-06-27")
    assert st.postdates_target is True


# ── never re-asked, never onto the wrong card ─────────────────────────────


def test_a_dismissed_pair_is_never_reproposed():
    rows = [_src(), _act(), _dlv()]
    rels = A2D + [_edge("src", "dlv", status="dismissed")]
    props, skipped = propose_feeds(rows, rels)
    assert props == [] and [s.reason for s in skipped] == ["edge_exists"]


def test_an_existing_edge_of_another_feeds_kind_suppresses():
    rows = [_src(), _act(), _dlv()]
    props, _ = propose_feeds(rows, A2D + [_edge("src", "dlv", "derives_from")])
    assert props == []


def test_an_absorbed_source_is_held_back():
    rows = [_src(), _act(), _dlv(), _dlv("other")]
    props, skipped = propose_feeds(rows, A2D + [_edge("src", "other", "absorbed_by")])
    assert props == [] and [s.reason for s in skipped] == ["absorbed"]


def test_a_misfiled_deliverable_card_is_held_back():
    # "Clarification on the deliverable for Mehul": kind deliverable, layer Note.
    rows = [_src(serves=("dlv",)), _dlv(layer="Note")]
    props, skipped = propose_feeds(rows, [])
    assert props == [] and [s.reason for s in skipped] == ["target_misfiled"]


def test_render_reports_held_back_reasons():
    rows = [_src(date="2026-07-21"), _act(date="2026-06-27"), _dlv()]
    text = render_report(*propose_feeds(rows, A2D), code="ibx-5192")
    assert "1 postdates" in text and "#270" in text


# ── the write: proposed, never active ─────────────────────────────────────


class _Q:
    def __init__(self, c, name):
        self.c, self.name, self.op, self.payload, self.f = c, name, "select", None, []

    def select(self, spec, *a, **k):
        assert spec != "*" and "*" not in spec, "NEVER select('*')"
        return self

    def eq(self, col, val):
        self.f.append(lambda r: str(r.get(col)) == str(val))
        return self

    def in_(self, col, vals):
        self.f.append(lambda r: r.get(col) in set(vals))
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def execute(self):
        rows = self.c.store.setdefault(self.name, [])
        if self.op == "insert":
            key = (self.payload["kind"], self.payload["from_item_id"],
                   self.payload["to_item_id"])
            if any((r["kind"], r["from_item_id"], r["to_item_id"]) == key for r in rows):
                raise RuntimeError("duplicate key value violates unique constraint (23505)")
            rows.append(dict(self.payload))
            return SimpleNamespace(data=[dict(self.payload)])
        return SimpleNamespace(data=[dict(r) for r in rows if all(f(r) for f in self.f)])


class _C:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Q(self, name)


def _seed(c, pid="P"):
    c.store["spine_substance"] = [
        dict(r, project_id=pid) for r in (_src(asset="a1"), _act(), _dlv())]
    c.store["spine_relations"] = [dict(e, project_id=pid) for e in A2D]
    c.store["rag_assets"] = [{"id": "a1", "created_at": "2026-07-01T10:00:00Z"}]


def test_propose_on_route_writes_a_proposed_edge_and_nothing_active():
    c = _C()
    _seed(c)
    out = propose_on_route(c, project_id="P", project_code="x-1",
                           est_item_ids=["src"], created_by="drew@firstperson.is")
    assert out["errors"] == []
    (row,) = [r for r in c.store["spine_relations"] if r["from_item_id"] == "src"]
    assert row["status"] == "proposed"
    assert row["source"] == "auto_ingest"
    assert row["kind"] == "informs" and row["to_item_id"] == "dlv"
    assert row["created_by"] == "drew@firstperson.is"
    assert row["confidence"] < 0.8
    # Re-routing the same element does not stack a second proposal.
    again = propose_on_route(c, project_id="P", project_code="x-1",
                             est_item_ids=["src"], created_by="drew@firstperson.is")
    assert again["proposed"] == []
    assert [s["reason"] for s in again["skipped"]] == ["edge_exists"]


def test_write_proposals_treats_a_unique_violation_as_already():
    c = _C()
    _seed(c)
    props, _ = propose_feeds(
        [dict(r) for r in c.store["spine_substance"]], A2D,
        source_dates={"a1": "2026-07-01"})
    first = write_proposals(c, props, project_id="P", project_code="x", created_by="e")
    second = write_proposals(c, props, project_id="P", project_code="x", created_by="e")
    assert len(first["proposed"]) == 1 and second["already"] == 1
    assert second["errors"] == []


def test_the_reader_selects_real_columns_only():
    live = {"id", "project_id", "project_code", "est_item_id", "est_item_kind",
            "phase", "binding", "version_label", "version_date", "status",
            "framing", "body", "sources", "rel_path", "field_states",
            "confirmed_by", "confirmed_at", "review_flags", "synced_at", "layer",
            "placement", "serves", "archived", "origin", "version_note",
            "important", "note", "company_id", "scope", "author_id", "actor",
            "card_kind", "lifetime", "agreement_id"}
    cols = {c.strip() for c in FEEDS_PROPOSE_COLUMNS.split(",")}
    assert cols <= live, cols - live
    assert "body" not in cols  # authored bodies run to ~100k chars
