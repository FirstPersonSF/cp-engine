from pathlib import Path

from cp_engine.shell_sync import sync_shell_elements


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

    def select(self, cols):
        self._op = ("select", cols); return self

    def eq(self, col, val):
        self._filter = (col, val); return self

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "upsert":
            for r in payload:
                rows[:] = [x for x in rows if x["element_id"] != r["element_id"]]
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
    def __init__(self): self.store = {}
    def table(self, name): return _FakeTable(self.store, name)


def _write_el(root: Path, layer: str, name: str, eid: str, **fm):
    d = root / "1p/acct/proj-1/shell" / layer
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"id: {eid}", "project: proj-1", f"layer: {layer}",
             f"title: {name}", "status: active", "last_touched: 2026-06-13"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    (d / f"{name}.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n")


def test_sync_inserts_all_elements(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos", stage="revised")
    _write_el(tmp_path, "Research", "iv1", "proj-1/research/iv1")
    client = _FakeClient()
    n = sync_shell_elements(client, project_id="u1",
                            project_dir=proj, tenant_root=tmp_path)
    assert n == 2
    rows = client.store["shell_elements"]
    assert {r["element_id"] for r in rows} == {
        "proj-1/deliverable/pos", "proj-1/research/iv1"}


def test_sync_deletes_orphaned_rows(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    # Pre-seed a stale row for this project that's no longer on disk.
    client.store["shell_elements"] = [
        {"element_id": "proj-1/research/GONE", "project_id": "u1"},
    ]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos")
    sync_shell_elements(client, project_id="u1",
                        project_dir=proj, tenant_root=tmp_path)
    ids = {r["element_id"] for r in client.store["shell_elements"]}
    assert ids == {"proj-1/deliverable/pos"}  # GONE was reaped


def test_sync_no_shell_dir_is_noop(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    n = sync_shell_elements(client, project_id="u1",
                            project_dir=proj, tenant_root=tmp_path)
    assert n == 0
    assert client.store.get("shell_elements", []) == []


def test_sync_reap_is_scoped_to_one_project(tmp_path):
    # A sibling project's row must NOT be reaped when we sync proj-1.
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["shell_elements"] = [
        {"element_id": "proj-2/research/keep", "project_id": "u2"},
    ]
    _write_el(tmp_path, "Deliverables", "pos", "proj-1/deliverable/pos")
    sync_shell_elements(client, project_id="u1",
                        project_dir=proj, tenant_root=tmp_path)
    ids = {r["element_id"] for r in client.store["shell_elements"]}
    assert "proj-2/research/keep" in ids   # sibling survived
    assert "proj-1/deliverable/pos" in ids
