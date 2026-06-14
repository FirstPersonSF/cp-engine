from datetime import date
from cp_engine.shell_stats import type_inventory, due_soon, stage_distribution


class _FakeTable:
    def __init__(self, rows): self._rows = rows; self._filter = None
    def select(self, cols): return self
    def eq(self, col, val): self._filter = (col, val); return self
    def execute(self):
        rows = self._rows
        if self._filter:
            col, val = self._filter
            rows = [r for r in rows if r.get(col) == val]
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _FakeTable(self._rows)


_ROWS = [
    {"layer": "Deliverables", "type": "positioning-narrative", "stage": "revised",
     "target_date": "2026-06-17", "project_code": "ibx-5153", "title": "IBX pos"},
    {"layer": "Deliverables", "type": "positioning-narrative", "stage": "final",
     "target_date": None, "project_code": "ggl-5168", "title": "GGL pos"},
    {"layer": "Deliverables", "type": "message-house", "stage": "first",
     "target_date": "2026-06-20", "project_code": "ibx-5153", "title": "IBX MH"},
    {"layer": "Deliverables", "type": "message-house", "stage": "conception",
     "target_date": "2026-07-30", "project_code": "sap-5171", "title": "far future"},
    # final stage WITH an in-window target_date (6/19): inventory/distribution
    # count it, but due_soon excludes it (shipped ≠ due).
    {"layer": "Deliverables", "type": "message-house", "stage": "final",
     "target_date": "2026-06-19", "project_code": "sap-5174", "title": "shipped MH"},
    {"layer": "Research", "type": None, "stage": None,  # not a deliverable — excluded
     "target_date": "2026-06-18", "project_code": "ibx-5153", "title": "interview"},
]


def test_type_inventory():
    inv = type_inventory(_FakeClient(_ROWS))
    # 2 positioning-narrative, 3 message-house (incl. the final-stage one); Research excluded
    assert ("positioning-narrative", 2) in inv
    assert ("message-house", 3) in inv
    assert all(layer_type != None for layer_type, _ in inv)
    # inventory counts ALL deliverables incl. finals → 5
    assert sum(n for _, n in inv) == 5


def test_due_soon():
    soon = due_soon(_FakeClient(_ROWS), today=date(2026, 6, 16), within_days=14)
    # within 6/16..6/30: 6/17 and 6/20 qualify; 7/30 too far; None excluded; Research excluded.
    # The 6/19 row is stage=final → excluded by due_soon even though it's in-window.
    dates = [r["target_date"] for r in soon]
    assert dates == ["2026-06-17", "2026-06-20"]  # ascending
    assert "2026-06-19" not in dates  # final excluded (shipped ≠ due)


def test_due_soon_excludes_finals_but_inventory_counts_them():
    """The in-window final (6/19) is absent from due_soon but present in
    type_inventory and stage_distribution — proving the exclusion is
    due_soon-specific, not a global filter."""
    client = _FakeClient(_ROWS)
    soon_codes = {r["project_code"] for r in
                  due_soon(client, today=date(2026, 6, 16), within_days=14)}
    assert "sap-5174" not in soon_codes  # the final-stage 6/19 row is gone
    # but inventory/distribution still count it
    assert ("message-house", 3) in type_inventory(client)
    assert stage_distribution(client)["final"] == 2


def test_due_soon_inclusive_boundaries():
    """Both endpoints of the [today, today+within_days] window are inclusive."""
    rows = [
        {"layer": "Deliverables", "type": "x", "stage": "first",
         "target_date": "2026-06-16", "project_code": "a", "title": "today"},
        {"layer": "Deliverables", "type": "x", "stage": "first",
         "target_date": "2026-06-30", "project_code": "b", "title": "horizon"},
    ]
    soon = due_soon(_FakeClient(rows), today=date(2026, 6, 16), within_days=14)
    dates = [r["target_date"] for r in soon]
    # 6/16 (today) and 6/30 (today+14) both included — pins the inclusive contract
    assert dates == ["2026-06-16", "2026-06-30"]


def test_stage_distribution():
    dist = stage_distribution(_FakeClient(_ROWS))
    # counts ALL stages incl. both finals; Research row (stage None) excluded
    assert dist == {"revised": 1, "final": 2, "first": 1, "conception": 1}
