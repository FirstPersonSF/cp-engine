from cp_engine.shell import row_to_element, load_shell_from_mc2


def test_row_to_element_roundtrips_spine():
    row = {
        "element_id": "p/deliverable/d1", "project_code": "p",
        "layer": "Deliverables", "type": "positioning-narrative",
        "title": "D1", "stage": "revised", "target_date": "2026-06-17",
        "status": "active", "last_touched": "2026-06-13",
        "depends_on": ["p/deliverable/d0"], "serves": ["p/deliverable/d1"],
        "source": [], "target_history": [], "author": "drew",
        "rel_path": "1p/a/p/shell/Deliverables/d1.md",
    }
    el = row_to_element(row)
    assert el.id == "p/deliverable/d1"
    assert el.layer == "Deliverables"
    assert el.stage == "revised"
    assert el.depends_on == ("p/deliverable/d0",)
    assert el.serves == ("p/deliverable/d1",)


def test_load_shell_from_mc2_filters_by_project(monkeypatch):
    captured = {}

    class _T:
        def select(self, c): return self
        def eq(self, c, v):
            captured["col"] = c
            captured["val"] = v
            return self
        def execute(self):
            data = [{"element_id": "p/deliverable/d1", "project_code": "p",
                     "layer": "Deliverables", "title": "D1", "status": "active",
                     "last_touched": "2026-06-13", "depends_on": [], "serves": [],
                     "source": [], "target_history": [], "rel_path": "x"}]
            return type("R", (), {"data": data})()
    class _C:
        def table(self, n): return _T()
    els = load_shell_from_mc2(_C(), "p")
    assert len(els) == 1 and els[0].id == "p/deliverable/d1"
    # The query filtered on project_code = the requested code.
    assert captured["col"] == "project_code"
    assert captured["val"] == "p"
