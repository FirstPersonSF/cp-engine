"""Tests for `cp spine-migrate` — elements→substance with proposed placement."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine import SpineElement
from cp_engine.spine_migrate import (
    element_to_substance_row,
    fetch_legacy_elements,
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


def test_legacy_element_id_serves_are_dropped():
    """Legacy `serves` are element→element graph edges (other element-ids), a
    different namespace than new substance `serves` (context→estimate-item uuids).
    There's no valid automatic translation, so migration starts clean: serves=[]."""
    el = _element(
        "Decisions", "Two-track AI story locked",
        eid="ibx-5153/decisions/two-track",
        serves=("ibx-5153/deliverable/foo",),  # a LEGACY element-id, not an est uuid
    )
    est = _estimate([("d-0", "Perspectives Report", "deliverable")])
    proposal = propose_placement(el, est)
    row = element_to_substance_row(
        el, proposal, canonical_code=CANON, project_id=PID,
        est_item_kind=None, today=TODAY,
    )
    assert row["serves"] == []  # NOT the legacy element-id


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


# ---- body hydration (the write-path bug) ------------------------------------
#
# The real write path goes through fetch_legacy_elements → row_to_element, which
# hardcodes body="". These tests exercise that fetch path end-to-end (NOT a
# directly-constructed SpineElement) to prove the body is hydrated from disk.


class _FakeSelectTable:
    """A fake `spine_elements` table that returns a fixed set of legacy rows
    from `.select(...).eq(...).execute().data`."""

    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return self

    @property
    def data(self):
        return self._rows


class _FakeSelectClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeSelectTable(self._rows)


def _legacy_row(*, element_id, rel_path, layer="Decisions", title="Two-track story"):
    """A legacy spine_elements row shaped like the real _ELEMENT_COLUMNS read
    (the MC-2 row has NO body column — the body lives on disk at rel_path)."""
    return {
        "element_id": element_id,
        "project_code": "ibx-5153",
        "layer": layer,
        "type": None,
        "title": title,
        "stage": None,
        "fidelity": None,
        "target_date": None,
        "status": "active",
        "last_touched": "2026-06-11",
        "depends_on": [],
        "serves": [],
        "source": [],
        "target_history": [],
        "author": None,
        "rel_path": rel_path,
        "project_id": PID,
    }


def _write_element_file(root: Path, rel_path: str, *, element_id, body):
    """Write a real spine element markdown file (frontmatter + body) at
    <root>/<rel_path> so parse_element can read it back."""
    abs_path = root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(
        "---\n"
        f"id: {element_id}\n"
        "project: ibx-5153\n"
        "layer: Decisions\n"
        "title: Two-track story\n"
        "status: active\n"
        "---\n"
        f"{body}\n"
    )
    return abs_path


def test_fetch_legacy_elements_hydrates_body_from_rel_path(tmp_path):
    """The missing test: go through the REAL fetch/hydrate path and assert the
    resulting element carries the NON-EMPTY body from its on-disk file."""
    rel = "1p/infoblox/ibx-5153-ai-campaign/spine/Decisions/two-track-story.md"
    eid = "ibx-5153/decisions/two-track-story"
    real_body = (
        "The two-track AI story (IQ + MCP/APIs) is locked. This prose is the "
        "distilled memory that must survive the migration."
    )
    _write_element_file(tmp_path, rel, element_id=eid, body=real_body)
    client = _FakeSelectClient([_legacy_row(element_id=eid, rel_path=rel)])

    elements = fetch_legacy_elements(client, project_id=PID, tenant_root=tmp_path)

    assert len(elements) == 1
    assert elements[0].body.strip() == real_body
    assert elements[0].body != ""


def test_migrated_row_carries_non_empty_body(tmp_path):
    """End-to-end: hydrate → plan → the upserted substance row body is non-empty."""
    rel = "1p/infoblox/ibx-5153-ai-campaign/spine/Decisions/two-track-story.md"
    eid = "ibx-5153/decisions/two-track-story"
    real_body = "Distilled decision prose."
    _write_element_file(tmp_path, rel, element_id=eid, body=real_body)
    client = _FakeSelectClient([_legacy_row(element_id=eid, rel_path=rel)])

    elements = fetch_legacy_elements(client, project_id=PID, tenant_root=tmp_path)
    plan = plan_migration(elements, None, canonical_code=CANON, project_id=PID, today=TODAY)

    assert plan[0].row["body"].strip() == real_body


def test_dry_run_fetch_skips_body_hydration(tmp_path):
    """Dry-run (tenant_root=None) must not require disk access — body stays empty
    and no file read happens even if rel_path points nowhere."""
    rel = "1p/infoblox/ibx-5153-ai-campaign/spine/Decisions/missing.md"
    eid = "ibx-5153/decisions/missing"
    client = _FakeSelectClient([_legacy_row(element_id=eid, rel_path=rel)])

    elements = fetch_legacy_elements(client, project_id=PID, tenant_root=None)

    assert len(elements) == 1
    assert elements[0].body == ""


def test_missing_file_warns_and_falls_back_to_empty_body(tmp_path, capsys):
    """A missing/unreadable file must NOT crash the migration — warn to stderr
    and fall back to an empty body for that one element."""
    rel = "1p/infoblox/ibx-5153-ai-campaign/spine/Decisions/does-not-exist.md"
    eid = "ibx-5153/decisions/does-not-exist"
    client = _FakeSelectClient([_legacy_row(element_id=eid, rel_path=rel)])

    elements = fetch_legacy_elements(client, project_id=PID, tenant_root=tmp_path)

    assert len(elements) == 1
    assert elements[0].body == ""
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert eid in err
