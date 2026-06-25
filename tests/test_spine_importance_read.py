# tests/test_spine_importance_read.py
from cp_engine.project_sources import list_spine, pull_spine


def _client(rows, captured):
    class _T:
        def __init__(self, n): captured.setdefault("table", n)
        def select(self, c): captured["select"] = c; return self
        def eq(self, c, v): captured.setdefault("eqs", []).append((c, v)); return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": rows})()
    class _C:
        def table(self, n): return _T(n)
    return _C()


def _row(eid, important):
    return {"est_item_id": eid, "framing": eid, "layer": "Note",
            "binding": "unbound", "status": "live", "serves": [], "body": "b",
            "important": important, "note": f"why {eid}"}


def test_list_spine_returns_important_and_sorts_important_first():
    captured = {}
    rows = [_row("a", False), _row("b", True), _row("c", False)]
    out = list_spine(_client(rows, captured), "pid")
    assert "*" not in captured["select"]
    assert "important" in captured["select"]
    assert out[0]["est_item_id"] == "b"          # important sorts first
    assert out[0]["important"] is True
    assert all("important" in r for r in out)


def test_list_spine_sort_is_stable_within_group():
    captured = {}
    rows = [_row("a", False), _row("b", False), _row("c", True)]
    out = list_spine(_client(rows, captured), "pid")
    # important first, then original order preserved among the non-important
    assert [r["est_item_id"] for r in out] == ["c", "a", "b"]


def test_pull_spine_returns_important_and_note():
    captured = {}
    rows = [_row("a", True)]
    el = pull_spine(_client(rows, captured), "pid", "a")
    assert el["important"] is True
    assert el["note"] == "why a"
