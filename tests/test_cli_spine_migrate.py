"""CLI test for `cp spine-migrate`: thin wiring + dry-run writes nothing."""

from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main

CANON = "ibx-5153-ai-campaign"
PID = "0b27ca26-a6cd-46a0-a7ab-816549f9a1d2"


def _tenant(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.27"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filter = None
        self._limit = None

    def select(self, cols):
        assert "*" not in cols
        self._op = ("select",)
        return self

    def upsert(self, rows, on_conflict=None):
        self.store.setdefault("_upserts", []).append((self.name, rows, on_conflict))
        self._op = ("upsert",)
        return self

    def eq(self, col, val):
        self._filter = (col, val)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._op[0] == "upsert":
            return type("R", (), {"data": []})()
        rows = self.store.get(self.name, [])
        if self._filter:
            col, val = self._filter
            rows = [r for r in rows if r.get(col) == val]
        if self._limit is not None:
            rows = rows[: self._limit]
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _FakeTable(self.store, name)

    # estimator-schema reads are stubbed out via monkeypatching fetch_estimate,
    # so .schema() never needs to work here.


def _store():
    return {
        # Canonical substance row → resolves project_id.
        "spine_substance": [{"project_code": CANON, "project_id": PID}],
        # Two legacy elements under the shared project_id.
        "spine_elements": [
            {
                "element_id": "ibx-5153/decisions/two-track", "project_code": "ibx-5153",
                "project_id": PID, "layer": "Decisions", "title": "Two-track locked",
                "status": "active", "last_touched": "2026-06-11", "rel_path": "x.md",
            },
            {
                "element_id": "ibx-5153/deliverable/audit", "project_code": "ibx-5153",
                "project_id": PID, "layer": "Deliverables", "title": "Narrative Audit",
                "status": "active", "last_touched": "2026-06-11", "rel_path": "y.md",
            },
        ],
    }


def _patch(monkeypatch, client, estimate):
    monkeypatch.setattr(
        "cp_engine.sync_mc2.MC2Backend.connect", lambda self, config: client
    )
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: estimate)


def _estimate():
    from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase

    item = EstimateItem(
        id="a-0", phase_id="ph-0", kind="activity", name="Narrative Audit",
        short_description=None, position=0, library_item_id=None,
    )
    phase = EstimatePhase(id="ph-0", name="Phase 0", overview=None, position=0, items=(item,))
    return Estimate(id="est-1", mc_project_id=PID, name="Estimate 1", phases=(phase,))


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _store()
    client = _FakeClient(store)
    _patch(monkeypatch, client, _estimate())

    res = CliRunner().invoke(main, ["spine-migrate", CANON, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "DRY RUN" in res.output
    assert "Two-track locked" in res.output
    assert "Narrative Audit" in res.output
    assert "_upserts" not in store  # nothing written


def test_run_upserts_substance_rows(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _store()
    client = _FakeClient(store)
    _patch(monkeypatch, client, _estimate())

    res = CliRunner().invoke(main, ["spine-migrate", CANON])
    assert res.exit_code == 0, res.output
    upserts = store.get("_upserts", [])
    assert len(upserts) == 1
    name, rows, on_conflict = upserts[0]
    assert name == "spine_substance"
    assert on_conflict == "id"
    ids = {r["id"] for r in rows}
    assert f"{CANON}/_context/two-track" in ids
    assert f"{CANON}/a-0/audit" in ids  # Deliverable bound to matched item
    # No auto-confirm.
    assert all("confirmed_by" not in r or r["confirmed_by"] is None for r in rows)


def test_missing_project_id_errors(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = {"spine_substance": [], "spine_elements": []}
    client = _FakeClient(store)
    _patch(monkeypatch, client, None)

    res = CliRunner().invoke(main, ["spine-migrate", "unknown-proj"])
    assert res.exit_code == 1
    assert "No project_id" in res.output


def _store_no_substance():
    """Legacy elements present (keyed on project_id) but NO substance rows yet —
    the fresh-project case that substance-based resolution can't handle."""
    s = _store()
    s["spine_substance"] = []
    return s


def test_explicit_mc_project_id_with_no_substance(tmp_path, monkeypatch):
    """A project with no substance rows still migrates when the human passes the
    known project_id via --mc-project-id: it bypasses substance resolution, reads
    the legacy elements by that id, and upserts substance rows."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _store_no_substance()
    client = _FakeClient(store)
    _patch(monkeypatch, client, _estimate())

    res = CliRunner().invoke(
        main, ["spine-migrate", CANON, "--mc-project-id", PID]
    )
    assert res.exit_code == 0, res.output
    upserts = store.get("_upserts", [])
    assert len(upserts) == 1
    name, rows, on_conflict = upserts[0]
    assert name == "spine_substance"
    ids = {r["id"] for r in rows}
    assert f"{CANON}/_context/two-track" in ids
    assert f"{CANON}/a-0/audit" in ids
    # The explicit id is the project_id stamped on every migrated row.
    assert all(r["project_id"] == PID for r in rows)


def test_no_substance_no_flag_errors(tmp_path, monkeypatch):
    """No substance rows AND no --mc-project-id → clear error, no writes."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _store_no_substance()
    client = _FakeClient(store)
    _patch(monkeypatch, client, _estimate())

    res = CliRunner().invoke(main, ["spine-migrate", CANON])
    assert res.exit_code == 1
    assert "No project_id" in res.output
    assert "_upserts" not in store
