import json
from pathlib import Path

from cp_engine.spine_substance_sync import (
    context_to_row,
    reconcile_bindings,
    substance_to_rows,
    sync_spine_context,
    sync_spine_substance,
)
from cp_engine.spine_context import ContextElement
from cp_engine.substance import SubstanceVersion, WorkItemSubstance


# ---- Task 2.2: row mappers --------------------------------------------------


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


def test_substance_to_rows_no_field_states_or_flags():
    rows = substance_to_rows(_item(), project_id="u1", project_code="proj-1",
                             rel_path="r.md")
    for r in rows:
        assert "field_states" not in r
        assert "review_flags" not in r


def test_context_to_row_maps_fields():
    el = ContextElement(type="source", provenance="client", nature="framework",
                        links=("d1", "d2"), body="distilled ctx", path=Path("x.md"),
                        title="Carol deck")
    row = context_to_row(el, project_id="u1", project_code="proj-1",
                         rel_path="1p/acct/proj-1/spine/_context/carol.md",
                         slug="carol")
    assert row["id"] == "proj-1/_context/carol"
    assert row["project_id"] == "u1"
    assert row["project_code"] == "proj-1"
    assert row["type"] == "source"
    assert row["provenance"] == "client"
    assert row["nature"] == "framework"
    assert row["title"] == "Carol deck"
    assert row["body"] == "distilled ctx"
    assert row["links"] == ["d1", "d2"]
    assert isinstance(row["links"], list)
    assert row["rel_path"] == "1p/acct/proj-1/spine/_context/carol.md"
    json.dumps(row)


# ---- Task 2.4: binding reconcile (pure fn) ----------------------------------


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


# ---- Task 2.3 + 2.4: per-project reconcile upserts --------------------------


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filter = None

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
        self._filter = (col, val); return self

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
            col, val = self._filter
            return type("R", (), {"data": [x for x in rows if x.get(col) == val]})()
        if op == "update":
            col, val = self._filter
            for x in rows:
                if x.get(col) == val:
                    x.update(payload)
            return type("R", (), {"data": []})()
        if op == "delete":
            col, val = self._filter
            rows[:] = [x for x in rows if x.get(col) != val]
            return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self): self.store = {}
    def table(self, name): return _FakeTable(self.store, name)


def _write_substance(root: Path, phase_dir: str, name: str, *, est_item_id,
                     kind="deliverable", binding="live", live_body="live body",
                     framing="framing"):
    d = root / "1p/acct/proj-1/spine" / phase_dir
    d.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nest_item_id: {est_item_id}\nest_item_kind: {kind}\n"
        f"phase: Phase 0\nbinding: {binding}\n---\n"
        f"## v2 — 2026-06-11 · live\nframing: {framing}\nsources:\n  - tx\n\n"
        f"{live_body}\n\n"
        f"## v1 — 2026-04-23 · superseded\nframing: old\nsources:\n\nold body\n"
    )
    (d / f"{name}.md").write_text(text)


def _write_context(root: Path, name: str, *, ctype="source", body="ctx body"):
    d = root / "1p/acct/proj-1/spine/_context"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\ntype: {ctype}\nprovenance: client\n---\n{body}\n"
    )


def test_sync_substance_upserts_all_version_rows(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    client = _FakeClient()
    n = sync_spine_substance(client, project_id="u1", project_code="proj-1",
                             project_dir=proj, tenant_root=tmp_path,
                             estimate=_FakeEstimate({"d1"}))
    assert n == 2
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert ids == {"proj-1/d1/v2", "proj-1/d1/v1"}
    live = next(r for r in client.store["spine_substance"]
                if r["id"] == "proj-1/d1/v2")
    assert live["binding"] == "live"
    assert live["field_states"] == {}
    assert live["review_flags"] == []


def test_sync_substance_skips_context_and_snapshots(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    _write_context(tmp_path, "carol")
    snap = proj / "spine/Phase0/pos.snapshots"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "frozen.md").write_text("---\nest_item_id: d1\nest_item_kind: deliverable\n---\n## v1 — 2026-01-01 · live\nframing: x\nsources:\n\nb\n")
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"d1"}))
    # Only the two real versions, nothing from _context or .snapshots.
    assert len(client.store["spine_substance"]) == 2


def test_sync_substance_reaps_vanished_row(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [
        {"id": "proj-1/GONE/v1", "project_code": "proj-1",
         "field_states": {}, "review_flags": []},
    ]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"d1"}))
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert "proj-1/GONE/v1" not in ids
    assert "proj-1/d1/v2" in ids


def test_sync_substance_reap_scoped_to_project(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [
        {"id": "proj-2/d9/v1", "project_code": "proj-2",
         "field_states": {}, "review_flags": []},
    ]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"d1"}))
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert "proj-2/d9/v1" in ids  # sibling survived


def test_sync_substance_preserves_confirmed_body(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "CONFIRMED body", "status": "live",
        "field_states": {"body": "confirmed"}, "review_flags": [],
    }]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1",
                     live_body="disk body")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"d1"}))
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["body"] == "CONFIRMED body"  # not clobbered
    assert any(f["field"] == "body" and f["now"] == "disk body"
               for f in row["review_flags"])


def test_sync_substance_confirmed_orphan_flagged_not_deleted(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "f", "body": "b", "status": "live",
        "field_states": {"body": "confirmed"}, "review_flags": [],
        "confirmed_by": "drew",
    }]
    # No substance file on disk.
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path, estimate=None)
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["confirmed_by"] == "drew"  # not deleted
    assert any(f["field"] == "source" and f["now"] == "missing"
               for f in row["review_flags"])


def test_sync_context_upserts_and_reconciles_body(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_context"] = [{
        "id": "proj-1/_context/carol", "project_code": "proj-1",
        "body": "CONFIRMED ctx",
        "field_states": {"body": "confirmed"}, "review_flags": [],
    }]
    _write_context(tmp_path, "carol", body="disk ctx")
    n = sync_spine_context(client, project_id="u1", project_code="proj-1",
                           project_dir=proj, tenant_root=tmp_path)
    assert n == 1
    row = next(r for r in client.store["spine_context"]
               if r["id"] == "proj-1/_context/carol")
    assert row["body"] == "CONFIRMED ctx"  # confirmed not clobbered
    assert any(f["field"] == "body" for f in row["review_flags"])


# ---- Task 2.4: binding flags wired into sync --------------------------------


def test_sync_substance_live_binding_no_flag(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"d1"}))
    for r in client.store["spine_substance"]:
        assert r["binding"] == "live"
        assert not any(f.get("source") == "binding" for f in r["review_flags"])


def test_sync_substance_orphaned_binding_flagged_not_deleted(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="gone")
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"other"}))
    rows = client.store["spine_substance"]
    assert len(rows) == 2  # not deleted
    for r in rows:
        assert r["binding"] == "orphaned"
    assert any(
        any(f.get("source") == "binding" and f["now"] == "orphaned"
            for f in r["review_flags"])
        for r in rows
    )


def test_sync_substance_none_estimate_all_unbound_no_flags(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path, estimate=None)
    for r in client.store["spine_substance"]:
        assert r["binding"] == "unbound"
        assert not any(f.get("source") == "binding" for f in r["review_flags"])


def test_sync_substance_orphan_recovered_prunes_stale_binding_flag(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    # Pre-seed a row that previously carried an orphan binding flag.
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "live body", "status": "live",
        "field_states": {}, "review_flags": [
            {"field": "binding", "now": "orphaned", "source": "binding"}],
    }, {
        "id": "proj-1/d1/v1", "project_code": "proj-1",
        "framing": "old", "body": "old body", "status": "superseded",
        "field_states": {}, "review_flags": [
            {"field": "binding", "now": "orphaned", "source": "binding"}],
    }]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, tenant_root=tmp_path,
                         estimate=_FakeEstimate({"d1"}))  # now found again
    for r in client.store["spine_substance"]:
        assert r["binding"] == "live"
        assert not any(f.get("source") == "binding" for f in r["review_flags"])
