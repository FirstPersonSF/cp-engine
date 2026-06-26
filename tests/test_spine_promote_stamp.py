# tests/test_spine_promote_stamp.py
from cp_engine.spine_promote import stamp_promoted_asset


def test_stamp_sets_source_provider_and_file_id():
    captured = {"updates": []}
    class _T:
        def __init__(self, n): pass
        def update(self, d): captured["updates"].append(d); return self
        def eq(self, c, v): captured.setdefault("eqs", []).append((c, v)); return self
        def execute(self): return type("R", (), {"data": [{"id": "a1"}]})()
    class _C:
        def table(self, n): return _T(n)
    out = stamp_promoted_asset(_C(), project_id="pid", est_item_id="w1",
                               title="T", file_path="/tmp/w1/t.txt")
    u = captured["updates"][0]
    assert u["source_provider"] == "spine-promote"
    assert u["source_file_id"] == "w1"
    # located by project_id + file_path + active
    assert ("project_id", "pid") in captured["eqs"]
    assert ("file_path", "/tmp/w1/t.txt") in captured["eqs"]
    assert ("status", "active") in captured["eqs"]


def test_stamp_returns_structured_result():
    class _T:
        def __init__(self, n): pass
        def update(self, d): return self
        def eq(self, c, v): return self
        def execute(self): return type("R", (), {"data": [{"id": "a1"}]})()
    class _C:
        def table(self, n): return _T(n)
    out = stamp_promoted_asset(_C(), project_id="pid", est_item_id="w1",
                               title="T", file_path="/tmp/w1/t.txt")
    assert out.get("stamped") is True or out.get("asset") or out.get("id")  # some success signal
