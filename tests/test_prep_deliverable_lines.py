# tests/test_prep_deliverable_lines.py — /cp-prep's deliverable-card strip.
from datetime import datetime, timezone

from cp_engine.prep_planning import _fetch_deliverable_lines
from cp_engine.state import ProjectState


def _project(code="sap-5174"):
    return ProjectState(
        code=code, name="Vision", source="projects",
        company_kind="client", company_code="SAP", company_name="SAP",
        status="Open", is_internal=False, owner="drew",
        last_touched=datetime(2026, 7, 11, tzinfo=timezone.utc),
        deadline=None,
    )


class _Client:
    """Answers the five fetches by table name + filters."""

    def __init__(self):
        self.projects = [{"id": "mcp1", "start_date": "2026-06-15"}]
        self.est_projects = [{"id": "est1"}]
        self.phases = [{"id": "ph1"}]
        self.deliverables = [{"id": "d1", "name": "P&P Report", "position": 1}]
        self.bars = [{"work_item_id": "d1", "start_week": 10, "done": False}]
        self.substance = [{"serves": ["d1"], "status": "live", "archived": False}]

    def schema(self, name): return self

    def table(self, name):
        outer = self

        class _T:
            def __init__(self): self._name = name
            def select(self, c): return self
            def eq(self, c, v): return self
            def in_(self, c, v): return self
            def execute(self):
                data = {
                    "projects": outer.projects if not hasattr(self, "_est") else [],
                    "phases": outer.phases,
                    "phase_deliverables": outer.deliverables,
                    "schedule_items": outer.bars,
                    "spine_substance": outer.substance,
                }[self._name]
                return type("R", (), {"data": [dict(r) for r in data]})()
        t = _T()
        # estimator.projects vs public.projects share the name; the second
        # projects call in the flow is the estimator one
        if name == "projects" and getattr(self, "_projects_called", False):
            t.execute = lambda: type("R", (), {"data": [dict(r) for r in outer.est_projects]})()
        if name == "projects":
            self._projects_called = True
        return t


def test_card_line_carries_due_and_outputs():
    lines = _fetch_deliverable_lines(_Client(), _project())
    assert lines == ("P&P Report · due ~2026-08-24 · 1 output accrued",)


def test_done_deliverable_gets_checkmark():
    client = _Client()
    client.bars[0]["done"] = True
    [line] = _fetch_deliverable_lines(client, _project())
    assert "done ✓" in line


def test_unlinked_bar_renders_undated_line():
    client = _Client()
    client.bars = []
    client.substance = []
    assert _fetch_deliverable_lines(client, _project()) == ("P&P Report",)


def test_initiative_slug_returns_empty():
    assert _fetch_deliverable_lines(_Client(), _project("mission-control")) == ()


def test_fetch_failure_is_empty_not_raise():
    class _Boom:
        def table(self, n): raise RuntimeError("db down")
        def schema(self, n): return self
    assert _fetch_deliverable_lines(_Boom(), _project()) == ()
