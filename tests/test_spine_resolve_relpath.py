# tests/test_spine_resolve_relpath.py
from cp_engine.project_sources import resolve_live_element


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


def test_resolve_live_element_includes_rel_path():
    captured = {}
    rows = [{"id": "p/w1/v1", "est_item_id": "w1", "framing": "W1",
             "status": "live", "important": False, "note": None,
             "rel_path": "meetings/2026-06-20-kickoff.txt"}]
    el = resolve_live_element(_client(rows, captured), "pid", "w1")
    assert "rel_path" in captured["select"]      # selected, not *
    assert "*" not in captured["select"]
    assert el["rel_path"] == "meetings/2026-06-20-kickoff.txt"
