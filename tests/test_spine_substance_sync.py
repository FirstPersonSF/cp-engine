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
                     framing="framing", layer=None, serves=()):
    d = root / "1p/acct/proj-1/spine" / phase_dir
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        f"---\nest_item_id: {est_item_id}\nest_item_kind: {kind}\n"
        f"phase: Phase 0\nbinding: {binding}\n"
    )
    if layer is not None:
        fm += f"layer: {layer}\n"
    if serves:
        fm += "serves:\n" + "".join(f"  - {s}\n" for s in serves)
    fm += "---\n"
    text = (
        fm
        + f"## v2 — 2026-06-11 · live\nframing: {framing}\nsources:\n  - tx\n\n"
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
                             project_dir=proj,
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
                         project_dir=proj,
                         estimate=_FakeEstimate({"d1"}))
    # Only the two real versions, nothing from _context or .snapshots.
    assert len(client.store["spine_substance"]) == 2


def _write_old_element(root: Path, layer_dir: str, name: str):
    """Write an OLD-style spine element file (capitalized layer dir, NO
    est_item_id frontmatter) — the shipped `spine_elements` files that coexist
    in the tree during the transition. parse_substance must never see these."""
    d = root / "1p/acct/proj-1/spine" / layer_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        "---\ntype: deliverable\nstatus: live\n---\n# Old element\nbody\n"
    )


def test_sync_substance_skips_old_element_files(tmp_path):
    """A transitional tree carries BOTH new substance files (with est_item_id)
    and old element files (under capitalized layer dirs, no est_item_id). The
    loader must SKIP the old element files gracefully instead of crashing
    parse_substance with a missing-est_item_id ValueError (reproduces the live
    IBX-5153 crash where 30 old files aborted the whole substance mirror)."""
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "phase-0-message-strategy", "messaging-system",
                     est_item_id="d1")
    _write_old_element(tmp_path, "Agreement", "engagement-terms")
    _write_old_element(tmp_path, "Deliverables", "foundation-pp-doc")
    _write_context(tmp_path, "carol")  # decoy
    snap = proj / "spine/phase-0-message-strategy/messaging-system.snapshots"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "frozen.md").write_text(
        "---\nest_item_id: d1\nest_item_kind: deliverable\n---\n"
        "## v1 — 2026-01-01 · live\nframing: x\nsources:\n\nb\n"
    )
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=_FakeEstimate({"d1"}))
    # Only the one real substance file's two versions — nothing from the old
    # element files, _context, or .snapshots.
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert ids == {"proj-1/d1/v2", "proj-1/d1/v1"}


def test_sync_substance_malformed_substance_file_still_raises(tmp_path):
    """The skip-probe must NOT over-swallow: a file that genuinely IS a
    substance file (has est_item_id) but is otherwise malformed (broken version
    header) must still raise — only NON-substance files are skipped."""
    import pytest

    proj = tmp_path / "1p/acct/proj-1"
    d = proj / "spine/phase-0-message-strategy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.md").write_text(
        "---\nest_item_id: d1\nest_item_kind: deliverable\nbinding: live\n---\n"
        "## not-a-valid-version-header\nframing: x\nsources:\n\nbody\n"
    )
    client = _FakeClient()
    with pytest.raises(ValueError):
        sync_spine_substance(client, project_id="u1", project_code="proj-1",
                             project_dir=proj, estimate=_FakeEstimate({"d1"}))


def test_sync_substance_reaps_vanished_row(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [
        {"id": "proj-1/GONE/v1", "project_code": "proj-1",
         "field_states": {}, "review_flags": []},
    ]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj,
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
                         project_dir=proj,
                         estimate=_FakeEstimate({"d1"}))
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert "proj-2/d9/v1" in ids  # sibling survived


def test_sync_substance_never_reaps_authored_row(tmp_path):
    """An origin='authored' row with NO confirmed field whose disk file is
    absent must be neither deleted nor flagged — MC-2 owns authored rows, the
    disk markdown is downstream and may not exist yet."""
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/auth/v1", "project_code": "proj-1",
        "framing": "f", "body": "b", "status": "live",
        "field_states": {}, "review_flags": [], "origin": "authored",
    }]
    # No substance file on disk; a distilled unconfirmed row here would be
    # deleted by the reap.
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=None)
    ids = {r["id"] for r in client.store["spine_substance"]}
    assert "proj-1/auth/v1" in ids  # not deleted
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/auth/v1")
    assert row["review_flags"] == []  # not flagged


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
                         project_dir=proj,
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
                         project_dir=proj, estimate=None)
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
                           project_dir=proj)
    assert n == 1
    row = next(r for r in client.store["spine_context"]
               if r["id"] == "proj-1/_context/carol")
    assert row["body"] == "CONFIRMED ctx"  # confirmed not clobbered
    assert any(f["field"] == "body" for f in row["review_flags"])


# ---- Phase 3: layer / serves / archived reconcile (confirmed-wins) ----------


def test_sync_substance_preserves_confirmed_layer(tmp_path):
    """A UI-set, confirmed `layer` must survive sync: the DB value stays and the
    disk-derived value is recorded as a review_flag, never clobbered."""
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "live body", "status": "live",
        "layer": "Research", "serves": [], "archived": False,
        "field_states": {"layer": "confirmed"}, "review_flags": [],
    }]
    # Disk frontmatter classifies it under a different layer.
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1",
                     layer="Decisions")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=_FakeEstimate({"d1"}))
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["layer"] == "Research"  # confirmed value wins, not clobbered
    assert any(f["field"] == "layer" and f["was"] == "Research"
               and f["now"] == "Decisions" for f in row["review_flags"])


def test_sync_substance_serves_confirmed_order_insensitive(tmp_path):
    """A confirmed `serves` that differs from disk ONLY in order/type must be
    treated as equal — no review_flag, value preserved. Equality must not be
    order- or list-vs-tuple-sensitive."""
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "live body", "status": "live",
        "layer": None, "serves": ["b", "a"], "archived": False,
        "field_states": {"serves": "confirmed"}, "review_flags": [],
    }]
    # Disk serves is the same set, different order, written as a YAML list.
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1",
                     serves=("a", "b"))
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=_FakeEstimate({"d1"}))
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    # Same set → no drift flag at all.
    assert not any(f["field"] == "serves" for f in row["review_flags"])
    assert sorted(row["serves"]) == ["a", "b"]


def test_sync_substance_serves_confirmed_genuine_diff_flagged(tmp_path):
    """A genuinely different confirmed `serves` (DB ['a'] vs disk ['a','b'])
    keeps the DB value and flags drift."""
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "live body", "status": "live",
        "layer": None, "serves": ["a"], "archived": False,
        "field_states": {"serves": "confirmed"}, "review_flags": [],
    }]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1",
                     serves=("a", "b"))
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=_FakeEstimate({"d1"}))
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["serves"] == ["a"]  # confirmed value preserved
    assert any(f["field"] == "serves" for f in row["review_flags"])


def test_sync_substance_preserves_confirmed_archived(tmp_path):
    """A confirmed `archived=true` (UI archive action) survives a sync where the
    disk file has no `archived` key (parses to False)."""
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "live body", "status": "live",
        "layer": None, "serves": [], "archived": True,
        "field_states": {"archived": "confirmed"}, "review_flags": [],
    }]
    # Disk has no archived key → parses False.
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=_FakeEstimate({"d1"}))
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["archived"] is True  # confirmed archive not undone by disk
    assert any(f["field"] == "archived" for f in row["review_flags"])


def test_sync_substance_unconfirmed_layer_tracks_disk(tmp_path):
    """When a field has no confirmed state, the disk-derived value wins (proposed
    path). Guards the default behavior — UI hasn't touched it, sync proceeds
    normally with no spurious flag."""
    proj = tmp_path / "1p/acct/proj-1"
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "framing", "body": "live body", "status": "live",
        "layer": "Research", "serves": [], "archived": False,
        "field_states": {}, "review_flags": [],
    }]
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1",
                     layer="Decisions")
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=_FakeEstimate({"d1"}))
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["layer"] == "Decisions"  # disk wins (proposed)
    assert not any(f["field"] == "layer" for f in row["review_flags"])


def test_sync_substance_confirmed_layer_orphan_flagged_not_deleted(tmp_path):
    """A row confirmed ONLY on `layer` whose disk file vanished must be flagged
    source-missing (not deleted) — the reap path must protect the new fields."""
    proj = tmp_path / "1p/acct/proj-1"; proj.mkdir(parents=True)
    client = _FakeClient()
    client.store["spine_substance"] = [{
        "id": "proj-1/d1/v2", "project_code": "proj-1",
        "framing": "f", "body": "b", "status": "live",
        "layer": "Research", "serves": [], "archived": False,
        "field_states": {"layer": "confirmed"}, "review_flags": [],
        "confirmed_by": "drew",
    }]
    # No substance file on disk.
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj, estimate=None)
    row = next(r for r in client.store["spine_substance"]
               if r["id"] == "proj-1/d1/v2")
    assert row["confirmed_by"] == "drew"  # not deleted
    assert any(f["field"] == "source" and f["now"] == "missing"
               for f in row["review_flags"])


# ---- Task 2.4: binding flags wired into sync --------------------------------


def test_sync_substance_live_binding_no_flag(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj,
                         estimate=_FakeEstimate({"d1"}))
    for r in client.store["spine_substance"]:
        assert r["binding"] == "live"
        assert not any(f.get("source") == "binding" for f in r["review_flags"])


def test_sync_substance_orphaned_binding_flagged_not_deleted(tmp_path):
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="gone")
    client = _FakeClient()
    sync_spine_substance(client, project_id="u1", project_code="proj-1",
                         project_dir=proj,
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
                         project_dir=proj, estimate=None)
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
                         project_dir=proj,
                         estimate=_FakeEstimate({"d1"}))  # now found again
    for r in client.store["spine_substance"]:
        assert r["binding"] == "live"
        assert not any(f.get("source") == "binding" for f in r["review_flags"])


def test_sync_substance_idempotent_across_two_passes(tmp_path):
    """Two back-to-back syncs against the same disk + a live estimate that
    orphans one item must converge: identical row set after pass 2, and the
    orphaned item's binding flag is REPLACED (not appended) so review_flags
    length stays put. The healthy item carries zero binding flags throughout.
    Locks the bounded-growth guarantee `_merge_flag` exists to provide."""
    proj = tmp_path / "1p/acct/proj-1"
    # One healthy item (d1) and one orphaned item (gone — not in the estimate).
    _write_substance(tmp_path, "Phase0", "live", est_item_id="d1")
    _write_substance(tmp_path, "Phase0", "orph", est_item_id="gone")
    client = _FakeClient()
    estimate = _FakeEstimate({"d1"})

    def _do_sync():
        sync_spine_substance(client, project_id="u1", project_code="proj-1",
                             project_dir=proj, estimate=estimate)

    _do_sync()
    after_1 = {r["id"]: dict(r) for r in client.store["spine_substance"]}
    flags_len_1 = {
        rid: len(r["review_flags"]) for rid, r in after_1.items()}

    _do_sync()
    after_2 = {r["id"]: dict(r) for r in client.store["spine_substance"]}

    # (a) identical row set across passes.
    assert set(after_1) == set(after_2)
    # (b) the orphaned rows' flag count is unchanged — replaced, not appended.
    for rid, r in after_2.items():
        assert len(r["review_flags"]) == flags_len_1[rid]

    orphan_rows = [r for r in after_2.values() if r["est_item_id"] == "gone"]
    healthy_rows = [r for r in after_2.values() if r["est_item_id"] == "d1"]
    assert orphan_rows and healthy_rows
    for r in orphan_rows:
        binding_flags = [f for f in r["review_flags"]
                         if f.get("source") == "binding"]
        assert len(binding_flags) == 1  # exactly one, no accumulation
        assert binding_flags[0]["now"] == "orphaned"
    for r in healthy_rows:
        assert not any(f.get("source") == "binding"
                       for f in r["review_flags"])


def test_resyncing_same_item_is_idempotent(tmp_path):
    """Re-syncing the SAME substance item must leave exactly one row per version
    (no duplicates). Regression guard for the prod issue that prompted the DB
    unique constraint on (project_code, est_item_id, version_label): the sync
    upserts on the stable id `<project_code>/<est_item_id>/<version_label>`, it
    does not append. A 2nd sync of the same disk converges to the same row set."""
    proj = tmp_path / "1p/acct/proj-1"
    _write_substance(tmp_path, "Phase0", "pos", est_item_id="d1")  # 2 versions
    client = _FakeClient()
    estimate = _FakeEstimate({"d1"})

    n1 = sync_spine_substance(client, project_id="u1", project_code="proj-1",
                              project_dir=proj, estimate=estimate)
    assert n1 == 2
    rows_after_1 = client.store["spine_substance"]
    assert len(rows_after_1) == 2
    ids_1 = [r["id"] for r in rows_after_1]
    assert sorted(ids_1) == ["proj-1/d1/v1", "proj-1/d1/v2"]

    n2 = sync_spine_substance(client, project_id="u1", project_code="proj-1",
                              project_dir=proj, estimate=estimate)
    assert n2 == 2
    rows_after_2 = client.store["spine_substance"]
    # Exactly one row per version — the 2nd sync upserted, did not duplicate.
    assert len(rows_after_2) == 2
    assert ids_1 == [r["id"] for r in rows_after_2]
    # Each (est_item_id, version_label) pair is unique — the constraint's key.
    keys = [(r["est_item_id"], r["version_label"]) for r in rows_after_2]
    assert len(keys) == len(set(keys))
