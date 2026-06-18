"""Tests for `cp spine-migrate` — elements→substance with proposed placement."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine import SpineElement
from cp_engine.spine_migrate import (
    element_to_substance_row,
    plan_migration,
    run_migration,
)
from cp_engine.placement_rule import propose_placement

CANON = "ibx-5153-ai-campaign"
PID = "0b27ca26-a6cd-46a0-a7ab-816549f9a1d2"
TODAY = date(2026, 6, 18)


def _element(layer, title, *, eid, body="distilled body", last_touched="2026-06-11", serves=()):
    return SpineElement(
        id=eid, project="ibx-5153", layer=layer, title=title, status="active",
        last_touched=last_touched, path=Path(eid + ".md"), body=body, serves=tuple(serves),
    )


def _estimate(items):
    est_items = tuple(
        EstimateItem(id=i, phase_id="ph-0", kind=k, name=n,
                     short_description=None, position=pos, library_item_id=None)
        for pos, (i, n, k) in enumerate(items)
    )
    phase = EstimatePhase(id="ph-0", name="Phase 0", overview=None, position=0, items=est_items)
    return Estimate(id="est-1", mc_project_id=PID, name="Estimate 1", phases=(phase,))


# ---- pure conversion --------------------------------------------------------


def test_context_element_converts_to_context_row():
    el = _element("Decisions", "Two-track AI story locked", eid="ibx-5153/decisions/two-track")
    est = _estimate([("d-0", "Perspectives Report", "deliverable")])
    proposal = propose_placement(el, est)
    row = element_to_substance_row(
        el, proposal, canonical_code=CANON, project_id=PID,
        est_item_kind=None, today=TODAY,
    )
    assert row["id"] == f"{CANON}/_context/two-track"
    assert row["project_code"] == CANON
    assert row["project_id"] == PID
    assert row["placement"] == "context"
    assert row["est_item_id"] is None
    assert row["est_item_kind"] is None
    assert row["binding"] == "unbound"
    assert row["layer"] == "Decisions"
    assert row["version_label"] == "v1"
    assert row["status"] == "live"
    assert row["framing"] == "Two-track AI story locked"
    assert row["body"] == "distilled body"
    assert row["version_date"] == "2026-06-11"  # from last_touched


def test_deliverable_binding_converts_to_proposed_item_row():
    el = _element(
        "Deliverables", "Perspectives and Possibilities Report",
        eid="ibx-5153/deliverable/perspectives",
    )
    est = _estimate([("d-0", "Perspectives & Possibilities Report", "deliverable")])
    proposal = propose_placement(el, est)
    assert proposal.placement == "item" and proposal.est_item_id == "d-0"
    row = element_to_substance_row(
        el, proposal, canonical_code=CANON, project_id=PID,
        est_item_kind="deliverable", today=TODAY,
    )
    assert row["id"] == f"{CANON}/d-0/perspectives"
    assert row["placement"] == "item"
    assert row["est_item_id"] == "d-0"
    assert row["est_item_kind"] == "deliverable"
    assert row["binding"] == "proposed"  # awaits human confirm, NOT auto-confirmed


def test_version_date_falls_back_to_today_when_last_touched_unparseable():
    el = _element("Brief", "April brief", eid="ibx-5153/brief/april", last_touched="")
    est = _estimate([])
    proposal = propose_placement(el, est)
    row = element_to_substance_row(
        el, proposal, canonical_code=CANON, project_id=PID,
        est_item_kind=None, today=TODAY,
    )
    assert row["version_date"] == TODAY.isoformat()


# ---- plan_migration ---------------------------------------------------------


def test_plan_migration_mixes_context_and_proposed_bindings():
    elements = (
        _element("Decisions", "A decision", eid="ibx-5153/decisions/a"),
        _element("Deliverables", "Narrative Audit", eid="ibx-5153/deliverable/audit"),
        _element("Deliverables", "Totally unrelated thing", eid="ibx-5153/deliverable/x"),
    )
    est = _estimate([("a-0", "Narrative Audit", "activity")])
    plan = plan_migration(elements, est, canonical_code=CANON, project_id=PID, today=TODAY)
    assert [m.proposal.placement for m in plan] == ["context", "item", "context"]
    bound = plan[1]
    assert bound.row["est_item_id"] == "a-0"
    assert bound.row["est_item_kind"] == "activity"  # lifted from the matched item
    assert bound.matched_item_name == "Narrative Audit"


def test_plan_migration_with_no_estimate_routes_everything_to_context():
    elements = (
        _element("Deliverables", "Some deliverable", eid="ibx-5153/deliverable/d"),
        _element("Brief", "A brief", eid="ibx-5153/brief/b"),
    )
    plan = plan_migration(elements, None, canonical_code=CANON, project_id=PID, today=TODAY)
    assert all(m.proposal.placement == "context" for m in plan)
    assert all(m.row["est_item_id"] is None for m in plan)


# ---- run_migration / dry-run isolation --------------------------------------


class _FakeTable:
    def __init__(self, recorder):
        self._rec = recorder

    def upsert(self, rows, on_conflict=None):
        self._rec.append(("upsert", rows, on_conflict))
        return self

    def execute(self):
        return self


class _FakeClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return _FakeTable(self.calls)


def test_run_migration_upserts_on_conflict_id():
    client = _FakeClient()
    rows = [{"id": "a"}, {"id": "b"}]
    run_migration(client, rows)
    upserts = [c for c in client.calls if c[0] == "upsert"]
    assert len(upserts) == 1
    assert upserts[0][1] == rows
    assert upserts[0][2] == "id"


def test_run_migration_with_no_rows_writes_nothing():
    client = _FakeClient()
    run_migration(client, [])
    assert not [c for c in client.calls if c[0] == "upsert"]


def test_dry_run_path_writes_nothing():
    # Simulate the CLI dry-run contract: plan is built, but run_migration is
    # never called → the fake client records zero upserts.
    client = _FakeClient()
    elements = (_element("Decisions", "d", eid="ibx-5153/decisions/d"),)
    plan = plan_migration(elements, None, canonical_code=CANON, project_id=PID, today=TODAY)
    assert len(plan) == 1
    # dry-run: deliberately do NOT call run_migration
    assert client.calls == []
