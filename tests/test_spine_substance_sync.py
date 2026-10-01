"""spine_substance_sync after architecture step 4c: MC-2 owns every element.

Kept from before 4c: the pure row mapper (`substance_to_rows` — the shape the
promote path now writes), the pure binding reconcile, and the code re-home
healers (widened to every origin). The disk→MC-2 push tests went with the
push; the generated-view tests live in tests/test_spine_generated_view.py.
"""

import json
from pathlib import Path

from cp_engine.spine_substance_sync import (
    _rehome_snapshot_codes,
    _rehome_substance_codes,
    reconcile_bindings,
    substance_to_rows,
    sync_spine_substance,
)
from cp_engine.substance import SubstanceVersion, WorkItemSubstance


def _item(est_item_id="d1", kind="deliverable", binding="live", phase="Phase 0",
          versions=None):
    if versions is None:
        versions = (
            SubstanceVersion(label="v2", date="2026-06-11", status="live",
                             framing="two-track story", sources=("janet-tx", "carol#p12"),
                             body="live body"),
            SubstanceVersion(label="v1", date="2026-04-23", status="superseded",
                             framing="kickoff", sources=(), body="old body"),
        )
    return WorkItemSubstance(
        est_item_id=est_item_id, est_item_kind=kind, phase=phase,
        binding=binding, versions=tuple(versions), path=Path("x.md"),
    )


def test_substance_to_rows_one_row_per_version():
    rows = substance_to_rows(_item(), project_id="u1", project_code="proj-1",
                             rel_path="1p/acct/proj-1/spine/Phase0/d1.md")
    assert len(rows) == 2
    assert [r["id"] for r in rows] == ["proj-1/d1/v2", "proj-1/d1/v1"]
    assert {r["version_label"] for r in rows} == {"v2", "v1"}


def test_substance_to_rows_carries_kind_binding_phase_status():
    rows = substance_to_rows(_item(kind="activity", binding="orphaned"),
                             project_id="u1", project_code="proj-1",
                             rel_path="r.md")
    live = next(r for r in rows if r["version_label"] == "v2")
    sup = next(r for r in rows if r["version_label"] == "v1")
    assert live["est_item_kind"] == "activity"
    assert live["binding"] == "orphaned"
    assert live["phase"] == "Phase 0"
    assert live["est_item_id"] == "d1"
    assert live["project_id"] == "u1"
    assert live["project_code"] == "proj-1"
    assert live["status"] == "live"
    assert sup["status"] == "superseded"
    assert live["framing"] == "two-track story"
    assert live["body"] == "live body"
    assert live["version_date"] == "2026-06-11"
    assert live["rel_path"] == "r.md"


def test_substance_to_rows_sources_are_json_safe_lists():
    rows = substance_to_rows(_item(), project_id="u1", project_code="proj-1",
                             rel_path="r.md")
    live = next(r for r in rows if r["version_label"] == "v2")
    assert live["sources"] == ["janet-tx", "carol#p12"]
    assert isinstance(live["sources"], list)
    # whole row must JSON-serialize
    json.dumps(live)


def test_substance_to_rows_carries_layer_placement_serves():
    versions = (
        SubstanceVersion(label="v1", date="2026-06-12", status="live",
                         framing="f", sources=(), body="b"),
    )
    item = WorkItemSubstance(
        est_item_id="d1", est_item_kind="deliverable", phase="Phase 0",
        binding="live", versions=versions, path=Path("x.md"),
        layer="Decisions", placement="context", serves=("abc",),
    )
    rows = substance_to_rows(item, project_id="u1", project_code="proj-1",
                             rel_path="r.md")
    for r in rows:
        assert r["layer"] == "Decisions"
        assert r["placement"] == "context"
        assert r["serves"] == ["abc"]
        assert isinstance(r["serves"], list)
        json.dumps(r)


def test_substance_to_rows_layer_placement_serves_defaults():
    rows = substance_to_rows(_item(), project_id="u1", project_code="proj-1",
                             rel_path="r.md")
    for r in rows:
        assert r["layer"] is None
        assert r["placement"] == "item"
        assert r["serves"] == []


def test_substance_to_rows_no_field_states_or_flags():
    rows = substance_to_rows(_item(), project_id="u1", project_code="proj-1",
                             rel_path="r.md")
    for r in rows:
        assert "field_states" not in r
        assert "review_flags" not in r


class _FakeEstimate:
    def __init__(self, ids):
        self._ids = set(ids)

    def item_by_id(self, item_id):
        return object() if item_id in self._ids else None


def test_reconcile_bindings_none_estimate_all_unbound():
    items = [_item(est_item_id="a"), _item(est_item_id="b")]
    out = reconcile_bindings(items, None)
    assert all(i.binding == "unbound" for i in out)


def test_reconcile_bindings_live_and_orphaned():
    items = [_item(est_item_id="present"), _item(est_item_id="gone")]
    out = reconcile_bindings(items, _FakeEstimate({"present"}))
    by_id = {i.est_item_id: i for i in out}
    assert by_id["present"].binding == "live"
    assert by_id["gone"].binding == "orphaned"


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filters = []

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows); return self

    def delete(self):
        self._op = ("delete", None); return self

    def update(self, values):
        self._op = ("update", values); return self

    def select(self, cols):
        assert "*" not in cols, "never select('*')"
        self._op = ("select", cols); return self

    def eq(self, col, val):
        # Chained .eq() calls AND together (the authored reverse-mirror select
        # filters on project_code AND origin).
        self._filters.append((col, val)); return self

    def _matches(self, x):
        return all(x.get(col) == val for col, val in self._filters)

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "upsert":
            for r in payload:
                match = next((x for x in rows if x["id"] == r["id"]), None)
                if match is not None:
                    match.update(r)
                else:
                    rows.append(dict(r))
            return type("R", (), {"data": payload})()
        if op == "select":
            return type("R", (), {"data": [x for x in rows if self._matches(x)]})()
        if op == "update":
            for x in rows:
                if self._matches(x):
                    x.update(payload)
            return type("R", (), {"data": []})()
        if op == "delete":
            rows[:] = [x for x in rows if not self._matches(x)]
            return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self): self.store = {}
    def table(self, name): return _FakeTable(self.store, name)


def test_rehome_authored_row_renamed_not_deleted(tmp_path):
    """An authored row under an OLD code is RENAMED to the current code: its
    project_code + id prefix are rewritten, the old id is gone, the body
    survives, and the function returns a count of 1."""
    proj = tmp_path / "1p/acct/new"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "OLD/_authored/x/v1", "project_id": "P", "project_code": "OLD",
        "origin": "authored", "framing": "f", "body": "AUTHORED body",
        "status": "live", "field_states": {}, "review_flags": [],
    }]
    n = _rehome_substance_codes(client, project_id="P", project_code="NEW")
    assert n == 1
    rows = client.store["spine_substance"]
    ids = {r["id"] for r in rows}
    assert "OLD/_authored/x/v1" not in ids
    assert "NEW/_authored/x/v1" in ids
    row = next(r for r in rows if r["id"] == "NEW/_authored/x/v1")
    assert row["project_code"] == "NEW"
    assert row["body"] == "AUTHORED body"  # body preserved


def test_rehome_authored_multiple_old_codes(tmp_path):
    """Two authored rows under two different old codes (same project_id) are both
    re-homed to the current code."""
    client = _FakeClient()
    client.store["spine_substance"] = [
        {"id": "OLD1/_authored/x/v1", "project_id": "P", "project_code": "OLD1",
         "origin": "authored", "body": "b1"},
        {"id": "OLD2/_authored/y/v1", "project_id": "P", "project_code": "OLD2",
         "origin": "authored", "body": "b2"},
    ]
    n = _rehome_substance_codes(client, project_id="P", project_code="NEW")
    assert n == 2
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert ids == {"NEW/_authored/x/v1", "NEW/_authored/y/v1"}
    assert all(r["project_code"] == "NEW" for r in client.store["spine_substance"])


def test_rehome_authored_already_current_no_op(tmp_path):
    """An authored row already at the current code is not touched; count 0."""
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "NEW/_authored/x/v1", "project_id": "P", "project_code": "NEW",
        "origin": "authored", "body": "b",
    }]
    n = _rehome_substance_codes(client, project_id="P", project_code="NEW")
    assert n == 0
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert ids == {"NEW/_authored/x/v1"}


def test_rehome_authored_collision_drops_stale_keeps_new(tmp_path):
    """When both OLD and NEW ids already exist (same project_id), the stale OLD
    row is dropped and the existing NEW row is kept; count 1."""
    client = _FakeClient()
    client.store["spine_substance"] = [
        {"id": "OLD/_authored/x/v1", "project_id": "P", "project_code": "OLD",
         "origin": "authored", "body": "stale"},
        {"id": "NEW/_authored/x/v1", "project_id": "P", "project_code": "NEW",
         "origin": "authored", "body": "current"},
    ]
    n = _rehome_substance_codes(client, project_id="P", project_code="NEW")
    assert n == 1
    rows = client.store["spine_substance"]
    ids = {r["id"] for r in rows}
    assert ids == {"NEW/_authored/x/v1"}
    assert next(r for r in rows if r["id"] == "NEW/_authored/x/v1")["body"] == "current"


def test_rehome_snapshot_row_renamed_not_deleted(tmp_path):
    """A snapshot row under an OLD code is RENAMED to the current code: its id,
    deliverable_id, and project_code are rewritten; the old id is gone; count 1."""
    client = _FakeClient()
    client.store["spine_snapshots"] = [{
        "id": "OLD/deliverable/x@s", "deliverable_id": "OLD/deliverable/x",
        "project_id": "P", "project_code": "OLD",
    }]
    n = _rehome_snapshot_codes(client, project_id="P", project_code="NEW")
    assert n == 1
    rows = client.store["spine_snapshots"]
    ids = {r["id"] for r in rows}
    assert "OLD/deliverable/x@s" not in ids
    assert ids == {"NEW/deliverable/x@s"}
    row = rows[0]
    assert row["deliverable_id"] == "NEW/deliverable/x"
    assert row["project_code"] == "NEW"


def test_rehome_snapshot_already_current_no_op(tmp_path):
    """A snapshot row already at the current code is not touched; count 0."""
    client = _FakeClient()
    client.store["spine_snapshots"] = [{
        "id": "NEW/deliverable/x@s", "deliverable_id": "NEW/deliverable/x",
        "project_id": "P", "project_code": "NEW",
    }]
    n = _rehome_snapshot_codes(client, project_id="P", project_code="NEW")
    assert n == 0
    ids = {r["id"] for r in client.store["spine_snapshots"]}
    assert ids == {"NEW/deliverable/x@s"}


def test_rehome_snapshot_collision_drops_stale_keeps_new(tmp_path):
    """When both OLD and NEW snapshot ids exist (same project_id), the stale OLD
    row is dropped and the existing NEW row is kept; count 1."""
    client = _FakeClient()
    client.store["spine_snapshots"] = [
        {"id": "OLD/deliverable/x@s", "deliverable_id": "OLD/deliverable/x",
         "project_id": "P", "project_code": "OLD"},
        {"id": "NEW/deliverable/x@s", "deliverable_id": "NEW/deliverable/x",
         "project_id": "P", "project_code": "NEW"},
    ]
    n = _rehome_snapshot_codes(client, project_id="P", project_code="NEW")
    assert n == 1
    ids = {r["id"] for r in client.store["spine_snapshots"]}
    assert ids == {"NEW/deliverable/x@s"}


def test_sync_substance_rehomes_snapshots_on_code_change(tmp_path):
    """sync_spine_substance re-homes a drifted snapshot row to the current code
    alongside the authored re-home (wired in at the same call site)."""
    proj = tmp_path / "1p/acct/new"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_snapshots"] = [{
        "id": "OLD/deliverable/x@s", "deliverable_id": "OLD/deliverable/x",
        "project_id": "u1", "project_code": "OLD",
    }]
    sync_spine_substance(client, project_id="u1", project_code="NEW",
                         project_dir=proj, estimate=None)
    ids = {r["id"] for r in client.store["spine_snapshots"]}
    assert ids == {"NEW/deliverable/x@s"}
    assert client.store["spine_snapshots"][0]["project_code"] == "NEW"


def test_sync_substance_rehomes_authored_on_code_change(tmp_path):
    """sync_spine_substance re-homes a drifted authored row to the current code.
    The authored row survives (never reaped) and now carries the new code."""
    proj = tmp_path / "1p/acct/new"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "OLD/_authored/x/v1", "project_id": "u1", "project_code": "OLD",
        "origin": "authored", "framing": "f", "body": "b", "status": "live",
        "est_item_id": "x", "field_states": {}, "review_flags": [],
    }]
    sync_spine_substance(client, project_id="u1", project_code="NEW",
                         project_dir=proj, estimate=None)
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert "OLD/_authored/x/v1" not in ids
    assert "NEW/_authored/x/v1" in ids
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "NEW/_authored/x/v1")
    assert row["project_code"] == "NEW"


def test_rehome_now_covers_distilled_rows_too(tmp_path):
    """Step 4c: a distilled row under an OLD code is re-homed as well. The
    disk push used to re-create distilled rows under the current code every
    sync; with the push gone, only this healer keeps them findable."""
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "OLD/d1/v2", "project_id": "P", "project_code": "OLD",
        "origin": "distilled", "body": "b",
    }]
    n = _rehome_substance_codes(client, project_id="P", project_code="NEW")
    assert n == 1
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert ids == {"NEW/d1/v2"}
