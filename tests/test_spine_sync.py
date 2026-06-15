from pathlib import Path

from cp_engine.spine_sync import sync_spine_elements


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._payload = None
        self._filter = None

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows); return self

    def delete(self):
        self._op = ("delete", None); return self

    def update(self, values):
        self._op = ("update", values); return self

    def select(self, cols):
        self._op = ("select", cols); return self

    def eq(self, col, val):
        self._filter = (col, val); return self

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "upsert":
            # Real PostgREST upsert does a PARTIAL update on conflict: columns
            # NOT in the payload are preserved. Mirror that so the fake catches
            # a regression that nulls human-only columns (confirmed_by/at).
            for r in payload:
                match = next(
                    (x for x in rows if x["element_id"] == r["element_id"]), None)
                if match is not None:
                    match.update(r)
                else:
                    rows.append(dict(r))
            return type("R", (), {"data": payload})()
        if op == "select":
            col, val = self._filter
            return type("R", (), {"data": [x for x in rows if x[col] == val]})()
        if op == "update":
            col, val = self._filter
            for x in rows:
                if x[col] == val:
                    x.update(payload)
            return type("R", (), {"data": []})()
        if op == "delete":
            col, val = self._filter
            before = len(rows)
            rows[:] = [x for x in rows if x[col] != val]
            return type("R", (), {"data": [], "count": before - len(rows)})()


class _FakeClient:
    def __init__(self): self.store = {}
    def table(self, name): return _FakeTable(self.store, name)


def _write_el(root: Path, layer: str, name: str, eid: str, **fm):
    d = root / "1p/acct/proj-1/spine" / layer
    d.mkdir(parents=True, exist_ok=True)
    status = fm.pop("status", "active")
    lines = [f"id: {eid}", "project: proj-1", f"layer: {layer}",
             f"title: {name}", f"status: {status}", "last_touched: 2026-06-13"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    (d / f"{name}.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n")


def test_sync_inserts_all_elements(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos", stage="revised")
    _write_el(tmp_path, "Research", "iv1", "proj-1/research/iv1")
    client = _FakeClient()
    n = sync_spine_elements(client, project_id="u1",
                            project_dir=proj, tenant_root=tmp_path)
    assert n == 2
    rows = client.store["spine_elements"]
    assert {r["element_id"] for r in rows} == {
        "proj-1/deliverable/pos", "proj-1/research/iv1"}


def test_sync_deletes_orphaned_rows(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    # Pre-seed a stale row for this project that's no longer on disk.
    client.store["spine_elements"] = [
        {"element_id": "proj-1/research/GONE", "project_id": "u1"},
    ]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos")
    sync_spine_elements(client, project_id="u1",
                        project_dir=proj, tenant_root=tmp_path)
    ids = {r["element_id"] for r in client.store["spine_elements"]}
    assert ids == {"proj-1/deliverable/pos"}  # GONE was reaped


def test_sync_no_spine_dir_is_noop(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    n = sync_spine_elements(client, project_id="u1",
                            project_dir=proj, tenant_root=tmp_path)
    assert n == 0
    assert client.store.get("spine_elements", []) == []


def test_sync_reap_is_scoped_to_one_project(tmp_path):
    # A sibling project's row must NOT be reaped when we sync proj-1.
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_elements"] = [
        {"element_id": "proj-2/research/keep", "project_id": "u2"},
    ]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos")
    sync_spine_elements(client, project_id="u1",
                        project_dir=proj, tenant_root=tmp_path)
    ids = {r["element_id"] for r in client.store["spine_elements"]}
    assert "proj-2/research/keep" in ids   # sibling survived
    assert "proj-1/deliverable/pos" in ids


def test_sync_keeps_confirmed_field_and_appends_flag(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    # Pre-seed MC-2 row: status confirmed=active, but disk will say dormant.
    client.store["spine_elements"] = [{
        "element_id": "proj-1/deliverable/pos", "project_id": "u1",
        "status": "active", "stage": None, "target_date": None,
        "serves": [], "depends_on": [],
        "field_states": {"status": "confirmed"}, "review_flags": [],
    }]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos", status="dormant")
    sync_spine_elements(client, project_id="u1", project_dir=proj, tenant_root=tmp_path)
    row = next(r for r in client.store["spine_elements"]
               if r["element_id"] == "proj-1/deliverable/pos")
    assert row["status"] == "active"   # confirmed value kept, not clobbered
    assert row["field_states"]["status"] == "confirmed"
    assert any(f["field"] == "status" and f["was"] == "active" and f["now"] == "dormant"
               for f in row["review_flags"])


def test_sync_updates_proposed_field_freely(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_elements"] = [{
        "element_id": "proj-1/deliverable/pos", "project_id": "u1",
        "status": "active", "stage": None, "target_date": None,
        "serves": [], "depends_on": [],
        "field_states": {}, "review_flags": [],
    }]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos", status="dormant")
    sync_spine_elements(client, project_id="u1", project_dir=proj, tenant_root=tmp_path)
    row = next(r for r in client.store["spine_elements"]
               if r["element_id"] == "proj-1/deliverable/pos")
    assert row["status"] == "dormant"   # proposed ⇒ free update
    assert row["review_flags"] == []


def test_sync_new_element_has_empty_verification_state(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos")
    sync_spine_elements(client, project_id="u1", project_dir=proj, tenant_root=tmp_path)
    row = next(r for r in client.store["spine_elements"]
               if r["element_id"] == "proj-1/deliverable/pos")
    assert row["field_states"] == {}
    assert row["review_flags"] == []


def test_sync_does_not_reap_confirmed_orphan_and_flags_it(tmp_path):
    """A row with a confirmed field whose markdown vanished is part of the
    human-verified spine — it must NOT be deleted. Instead a 'source missing'
    review_flag is recorded so a human can resolve it."""
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_elements"] = [{
        "element_id": "proj-1/deliverable/CONFIRMED", "project_id": "u1",
        "status": "active", "stage": None, "target_date": None,
        "serves": [], "depends_on": [],
        "field_states": {"status": "confirmed"}, "review_flags": [],
        "confirmed_by": "drew", "confirmed_at": "2026-06-15T00:00:00Z",
    }]
    # No markdown on disk for this element.
    sync_spine_elements(client, project_id="u1", project_dir=proj, tenant_root=tmp_path)
    rows = client.store["spine_elements"]
    surviving = next(r for r in rows
                     if r["element_id"] == "proj-1/deliverable/CONFIRMED")
    assert surviving["status"] == "active"                 # not deleted
    assert surviving["confirmed_by"] == "drew"             # human state intact
    assert any(f["field"] == "source" and f["now"] == "missing"
               for f in surviving["review_flags"])


def test_sync_still_reaps_unconfirmed_orphan(tmp_path):
    """An orphan with NO confirmed field is still reaped (unchanged behavior)."""
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_elements"] = [{
        "element_id": "proj-1/research/GONE", "project_id": "u1",
        "field_states": {}, "review_flags": [],
    }]
    sync_spine_elements(client, project_id="u1", project_dir=proj, tenant_root=tmp_path)
    ids = {r["element_id"] for r in client.store["spine_elements"]}
    assert "proj-1/research/GONE" not in ids


def test_sync_keeps_at_most_one_flag_per_field(tmp_path):
    """A persistent confirmed/disk conflict must not accrue a flag per sync —
    syncing twice with the same divergence yields exactly one status flag."""
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_elements"] = [{
        "element_id": "proj-1/deliverable/pos", "project_id": "u1",
        "status": "active", "stage": None, "target_date": None,
        "serves": [], "depends_on": [],
        "field_states": {"status": "confirmed"}, "review_flags": [],
    }]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos", status="dormant")
    for _ in range(2):
        sync_spine_elements(client, project_id="u1", project_dir=proj, tenant_root=tmp_path)
    row = next(r for r in client.store["spine_elements"]
               if r["element_id"] == "proj-1/deliverable/pos")
    status_flags = [f for f in row["review_flags"] if f["field"] == "status"]
    assert len(status_flags) == 1                          # not 2
    assert row["status"] == "active"                       # still not clobbered


def test_merge_flag_is_source_aware():
    # A reconcile clean-up (flag=None, source default) must NOT wipe a sweep
    # flag on the same field — two independent producers coexist.
    from cp_engine.spine_sync import _merge_flag
    sweep_flag = {"field": "status", "now": "drifted", "source": "sweep"}
    flags = [sweep_flag]
    # reconcile finds no divergence on status → calls with flag=None:
    after = _merge_flag(flags, "status", None)
    assert sweep_flag in after  # sweep flag survived the reconcile clean-up
    # a NEW reconcile flag on status replaces only the reconcile-sourced one:
    recon_flag = {"field": "status", "was": "active", "now": "dormant"}
    after2 = _merge_flag(after, "status", recon_flag)
    assert sweep_flag in after2          # still there
    assert recon_flag in after2          # both producers' flags coexist
    # a second sweep flag replaces the first sweep flag (≤1 per producer/field):
    sweep_flag2 = {"field": "status", "now": "drifted again", "source": "sweep"}
    after3 = _merge_flag(after2, "status", sweep_flag2)
    assert sweep_flag2 in after3
    assert sweep_flag not in after3
    assert recon_flag in after3          # reconcile flag untouched
