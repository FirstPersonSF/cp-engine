from pathlib import Path

from cp_engine.spine_snapshot import row_from_frozen
from cp_engine.spine_sync import sync_spine_snapshots


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filter = None

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows)
        return self

    def delete(self):
        self._op = ("delete", None)
        return self

    def select(self, cols):
        self._op = ("select", cols)
        return self

    def eq(self, col, val):
        self._filter = (col, val)
        return self

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "upsert":
            for r in payload:
                rows[:] = [x for x in rows if x["id"] != r["id"]]
                rows.append(dict(r))
            return type("R", (), {"data": payload})()
        if op == "select":
            col, val = self._filter
            return type("R", (), {"data": [x for x in rows if x[col] == val]})()
        if op == "delete":
            col, val = self._filter
            before = len(rows)
            rows[:] = [x for x in rows if x[col] != val]
            return type("R", (), {"data": [], "count": before - len(rows)})()


class _FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _write_snap(
    root: Path,
    proj_rel: str,
    layer: str,
    deliverable_stem: str,
    fname: str,
    *,
    of: str,
    project: str,
    label: str = "Snap",
    reason: str | None = "because",
    commit: str | None = "abc1234",
    created: str = "2026-06-13",
    dirty: bool = False,
) -> Path:
    d = root / proj_rel / "spine" / layer / f"{deliverable_stem}.snapshots"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"project: {project}",
        "snapshot:",
        f"  of: {of}",
        f"  label: {label}",
        f"  reason: {reason}",
        f"  created: {created}",
        f"  commit: {commit}",
        f"  working_copy_dirty: {str(dirty).lower()}",
        "---",
        "frozen body",
    ]
    p = d / fname
    p.write_text("\n".join(lines) + "\n")
    return p


# --- row_from_frozen ---


def test_row_from_frozen_with_snapshot_block(tmp_path):
    p = _write_snap(
        tmp_path, "1p/acct/proj-1", "Deliverables", "pos",
        "2026-06-13-snap.md",
        of="proj-1/deliverable/pos", project="proj-1",
        label="Before workshop", reason="freeze it", commit="deadbee",
    )
    row = row_from_frozen(p, tenant_root=tmp_path)
    assert row is not None
    assert row["id"] == "proj-1/deliverable/pos@2026-06-13-snap"
    assert row["deliverable_id"] == "proj-1/deliverable/pos"
    assert row["project_code"] == "proj-1"
    assert row["label"] == "Before workshop"
    assert row["reason"] == "freeze it"
    assert row["commit"] == "deadbee"
    assert row["working_copy_dirty"] is False
    assert row["created"] == "2026-06-13"
    assert row["rel_path"] == str(p.relative_to(tmp_path))


def test_row_from_frozen_without_snapshot_block_is_none(tmp_path):
    d = tmp_path / "1p/acct/proj-1/spine/Deliverables/pos.snapshots"
    d.mkdir(parents=True)
    p = d / "not-a-snap.md"
    p.write_text("---\nproject: proj-1\ntitle: nope\n---\nbody\n")
    assert row_from_frozen(p, tenant_root=tmp_path) is None


def test_row_from_frozen_malformed_yaml_is_none(tmp_path):
    d = tmp_path / "1p/acct/proj-1/spine/Deliverables/pos.snapshots"
    d.mkdir(parents=True)
    p = d / "broken.md"
    p.write_text("---\n: : :\n---\nbody\n")  # malformed YAML frontmatter
    assert row_from_frozen(p, tenant_root=tmp_path) is None


# --- sync_spine_snapshots ---


def test_sync_upserts_all_snapshots(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_snap(tmp_path, "1p/acct/proj-1", "Deliverables", "pos",
                "2026-06-13-a.md", of="proj-1/deliverable/pos", project="proj-1")
    _write_snap(tmp_path, "1p/acct/proj-1", "Deliverables", "pos",
                "2026-06-12-b.md", of="proj-1/deliverable/pos", project="proj-1")
    client = _FakeClient()
    n = sync_spine_snapshots(
        client, project_code="proj-1", project_dir=proj, tenant_root=tmp_path
    )
    assert n == 2
    ids = {r["id"] for r in client.store["spine_snapshots"]}
    assert ids == {
        "proj-1/deliverable/pos@2026-06-13-a",
        "proj-1/deliverable/pos@2026-06-12-b",
    }


def test_sync_reaps_orphaned_snapshot_rows(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_snapshots"] = [
        {"id": "proj-1/deliverable/pos@GONE", "project_code": "proj-1"},
    ]
    _write_snap(tmp_path, "1p/acct/proj-1", "Deliverables", "pos",
                "2026-06-13-a.md", of="proj-1/deliverable/pos", project="proj-1")
    sync_spine_snapshots(
        client, project_code="proj-1", project_dir=proj, tenant_root=tmp_path
    )
    ids = {r["id"] for r in client.store["spine_snapshots"]}
    assert ids == {"proj-1/deliverable/pos@2026-06-13-a"}  # GONE reaped


def test_sync_reap_scoped_to_one_project(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_snapshots"] = [
        {"id": "proj-2/deliverable/x@keep", "project_code": "proj-2"},
    ]
    _write_snap(tmp_path, "1p/acct/proj-1", "Deliverables", "pos",
                "2026-06-13-a.md", of="proj-1/deliverable/pos", project="proj-1")
    sync_spine_snapshots(
        client, project_code="proj-1", project_dir=proj, tenant_root=tmp_path
    )
    ids = {r["id"] for r in client.store["spine_snapshots"]}
    assert "proj-2/deliverable/x@keep" in ids  # sibling survived
    assert "proj-1/deliverable/pos@2026-06-13-a" in ids


def test_sync_no_spine_dir_is_noop(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    proj.mkdir(parents=True)
    client = _FakeClient()
    n = sync_spine_snapshots(
        client, project_code="proj-1", project_dir=proj, tenant_root=tmp_path
    )
    assert n == 0
    assert client.store.get("spine_snapshots", []) == []


def test_sync_skips_non_snapshot_files(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    d = tmp_path / "1p/acct/proj-1/spine/Deliverables/pos.snapshots"
    d.mkdir(parents=True)
    (d / "stray.md").write_text("---\nproject: proj-1\n---\nbody\n")
    _write_snap(tmp_path, "1p/acct/proj-1", "Deliverables", "pos",
                "2026-06-13-a.md", of="proj-1/deliverable/pos", project="proj-1")
    client = _FakeClient()
    n = sync_spine_snapshots(
        client, project_code="proj-1", project_dir=proj, tenant_root=tmp_path
    )
    assert n == 1  # stray (no snapshot block) ignored
