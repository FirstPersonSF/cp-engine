# tests/test_spine_done_read.py
import cp_engine.project_sources as ps


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


def _row(eid, important=False):
    return {"est_item_id": eid, "framing": eid, "layer": "Note",
            "binding": "live", "status": "live", "serves": [], "body": "b",
            "important": important, "note": None}


def test_list_spine_adds_done_from_map(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map",
                        lambda client, pid: {"w1": True, "w2": False})
    captured = {}
    rows = [_row("w1"), _row("w2"), _row("_authored/x")]
    out = ps.list_spine(_client(rows, captured), "pid")
    by = {r["est_item_id"]: r for r in out}
    assert by["w1"]["done"] is True
    assert by["w2"]["done"] is False
    assert by["_authored/x"]["done"] is None   # unbound → n/a


def test_list_spine_done_degrades_to_none_on_estimate_error(monkeypatch):
    def _boom(client, pid): raise RuntimeError("estimator down")
    monkeypatch.setattr(ps, "fetch_project_done_map", _boom)
    captured = {}
    out = ps.list_spine(_client([_row("w1")], captured), "pid")
    assert out[0].get("done") is None    # graceful: None, not a crash


def test_pull_spine_includes_done(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map",
                        lambda client, pid: {"w1": True})
    captured = {}
    el = ps.pull_spine(_client([_row("w1")], captured), "pid", "w1")
    assert el["done"] is True


def test_pull_spine_done_none_for_unbound(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda client, pid: {})
    captured = {}
    el = ps.pull_spine(_client([_row("_authored/x")], captured), "pid", "_authored/x")
    assert el["done"] is None


def test_pull_spine_done_degrades_to_none_on_error(monkeypatch):
    def _boom(client, pid): raise RuntimeError("estimator down")
    monkeypatch.setattr(ps, "fetch_project_done_map", _boom)
    captured = {}
    el = ps.pull_spine(_client([_row("w1")], captured), "pid", "w1")
    assert el.get("done") is None   # fail-soft, not a crash
