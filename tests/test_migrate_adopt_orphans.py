"""Adoption of re-keyed elements' orphaned distilled rows (#200).

The invariant under test: a stale phase-dir file's rows must become
`origin='authored'` BEFORE the file is removed, because the reap in
spine_substance_sync exempts authored rows only and hard-deletes an
all-`proposed` distilled ladder. See cp_engine.migrate_adopt_orphans.
"""

from __future__ import annotations

import pytest

from cp_engine.migrate_adopt_orphans import (
    AdoptOrphansError,
    adopt_orphaned_versions,
)

EID = "e94d0a03-427d-4f26-b237-d9b732b0e402"
CODE = "sap-5174-vision-update-2026"


def _row(n: int, origin: str, status: str = "superseded") -> dict:
    return {
        "id": f"{CODE}/{EID}/v{n}",
        "est_item_id": EID,
        "version_label": f"v{n}",
        "status": status,
        "origin": origin,
        "archived": False,
    }


class _FakeTable:
    def __init__(self, store):
        self.store = store
        self._filters = {}
        self._payload = None

    def select(self, *_a, **_kw):
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        if self._payload is not None:
            rid = self._filters.get("id")
            for r in self.store["rows"]:
                if r["id"] == rid:
                    r.update(self._payload)
                    self.store["updates"].append((rid, dict(self._payload)))
            return type("R", (), {"data": []})()
        rows = [
            r for r in self.store["rows"]
            if all(r.get(k) == v for k, v in self._filters.items())
        ]
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, _name):
        return _FakeTable(self.store)


@pytest.fixture
def store(monkeypatch):
    s = {"rows": [], "updates": []}
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: _FakeClient(s)
    )
    return s


def test_adopts_distilled_rows_and_leaves_authored_alone(store):
    """The sap-5174 shape: v1-v6 distilled + v7 authored/live."""
    store["rows"] = [_row(n, "distilled") for n in range(1, 7)]
    store["rows"].append(_row(7, "authored", status="live"))

    res = adopt_orphaned_versions(CODE, EID)

    assert res.adopted == [f"v{n}" for n in range(1, 7)]
    assert res.already_authored == ["v7"]
    assert res.live_rows == ["v7"]
    # Every row is now authored -> the reap will skip all of them.
    assert all(r["origin"] == "authored" for r in store["rows"])
    # v7 was never rewritten.
    assert all(rid.endswith(tuple(f"/v{n}" for n in range(1, 7)))
               for rid, _ in store["updates"])


def test_dry_run_writes_nothing(store):
    store["rows"] = [_row(n, "distilled") for n in range(1, 7)]
    store["rows"].append(_row(7, "authored", status="live"))

    res = adopt_orphaned_versions(CODE, EID, dry_run=True)

    assert res.adopted == [f"v{n}" for n in range(1, 7)]
    assert res.dry_run is True
    assert store["updates"] == []
    assert sum(1 for r in store["rows"] if r["origin"] == "distilled") == 6


def test_idempotent_when_already_adopted(store):
    """Re-running must be a no-op, not a second round of writes."""
    store["rows"] = [_row(n, "authored") for n in range(1, 7)]
    store["rows"].append(_row(7, "authored", status="live"))

    res = adopt_orphaned_versions(CODE, EID)

    assert res.adopted == []
    assert store["updates"] == []


def test_refuses_when_no_rows(store):
    store["rows"] = []
    with pytest.raises(AdoptOrphansError, match="nothing to adopt"):
        adopt_orphaned_versions(CODE, EID)


def test_refuses_on_double_live(store):
    """A ladder still carrying two live rows is not yet healed — refuse.

    Adopting here would freeze the double-live state under MC-2 ownership,
    where the shield can no longer correct it from disk.
    """
    store["rows"] = [
        _row(6, "distilled", status="live"),
        _row(7, "authored", status="live"),
    ]
    with pytest.raises(AdoptOrphansError, match="expected exactly 1 live row"):
        adopt_orphaned_versions(CODE, EID)
    assert store["updates"] == []


def test_refuses_when_no_live_row(store):
    store["rows"] = [_row(n, "distilled") for n in range(1, 4)]
    with pytest.raises(AdoptOrphansError, match="expected exactly 1 live row"):
        adopt_orphaned_versions(CODE, EID)
