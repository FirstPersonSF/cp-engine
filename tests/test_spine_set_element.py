from cp_engine.authored_element import build_version_rows


def test_version_retains_important_and_note_from_prior_row():
    """The bug Task 3 fixes: a prior row carrying important/note must produce a
    new live version that RETAINS them. _SEL selecting these columns is what
    makes the prior dict carry the keys this asserts on."""
    prior = [{
        "version_label": "v1", "framing": "Fred lens", "layer": "Note",
        "serves": [], "important": True, "note": "the fork",
    }]
    rows = build_version_rows(
        project_id="pid", project_code="p", est_item_id="_authored/fred-lens",
        prior_versions=prior, body="v2 body", version_note="sharpened",
        now_iso="2026-06-26T00:00:00+00:00",
    )
    live = next(r for r in rows if r["status"] == "live")
    assert live["important"] is True
    assert live["note"] == "the fork"


def test_set_spine_element_partial_update_important_only(monkeypatch):
    captured = {"updates": []}
    class _T:
        def __init__(self, n): captured["table"] = n
        def select(self, c): captured["select"] = c; return self
        def eq(self, c, v): captured.setdefault("eqs", []).append((c, v)); return self
        def order(self, *a, **k): return self
        def update(self, d): captured["updates"].append(d); return self
        def execute(self):
            return type("R", (), {"data": [
                {"id": "p/_authored/x/v1", "est_item_id": "_authored/x",
                 "framing": "X", "status": "live", "important": False, "note": None}
            ]})()
    class _C:
        def table(self, n): return _T(n)
    monkeypatch.setattr("cp_engine.mcp_server._resolve",
                        lambda code: (_C(), "pid", "cid"))
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=True)
    assert {"important": True} in captured["updates"]   # only important written
    assert all("note" not in u for u in captured["updates"])  # note NOT touched (None = no-op)
    assert res["est_item_id"] == "_authored/x"
    assert res["important"] is True


def test_set_spine_element_no_match_returns_note_field(monkeypatch):
    class _T:
        def __init__(self, n): pass
        def select(self, c): return self
        def eq(self, c, v): return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": []})()
    class _C:
        def table(self, n): return _T(n)
    monkeypatch.setattr("cp_engine.mcp_server._resolve",
                        lambda code: (_C(), "pid", "cid"))
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "missing", important=True)
    assert "note" in res or "error" in res   # structured miss, never throws
    assert "est_item_id" not in res


def test_set_spine_element_note_only_does_not_touch_important(monkeypatch):
    captured = {"updates": []}
    class _T:
        def __init__(self, n): pass
        def select(self, c): return self
        def eq(self, c, v): return self
        def order(self, *a, **k): return self
        def update(self, d): captured["updates"].append(d); return self
        def execute(self):
            return type("R", (), {"data": [
                {"id": "p/_authored/x/v1", "est_item_id": "_authored/x",
                 "framing": "X", "status": "live", "important": True, "note": None}
            ]})()
    class _C:
        def table(self, n): return _T(n)
    monkeypatch.setattr("cp_engine.mcp_server._resolve", lambda c: (_C(), "pid", "cid"))
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", note="why it matters")
    assert {"note": "why it matters"} in captured["updates"]
    assert all("important" not in u for u in captured["updates"])  # untouched
    assert res["important"] is True   # current DB value echoed back, not clobbered


def test_set_spine_element_important_false_is_written(monkeypatch):
    captured = {"updates": []}
    class _T:
        def __init__(self, n): pass
        def select(self, c): return self
        def eq(self, c, v): return self
        def order(self, *a, **k): return self
        def update(self, d): captured["updates"].append(d); return self
        def execute(self):
            return type("R", (), {"data": [
                {"id": "p/_authored/x/v1", "est_item_id": "_authored/x",
                 "framing": "X", "status": "live", "important": True, "note": None}
            ]})()
    class _C:
        def table(self, n): return _T(n)
    monkeypatch.setattr("cp_engine.mcp_server._resolve", lambda c: (_C(), "pid", "cid"))
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=False)
    assert {"important": False} in captured["updates"]  # explicit False IS written
    assert res["important"] is False
