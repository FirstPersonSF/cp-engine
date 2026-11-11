"""Learning phase 6: the workload lookahead (Drew, 2026-11-11: "it looks like
Marcello has a lot on his plate … what can we do")."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from cp_engine import workload as W


class _Q:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]

    def select(self, _c):
        return self

    def eq(self, c, v):
        self.rows = [r for r in self.rows if r.get(c) == v]
        return self

    def gte(self, c, v):
        self.rows = [r for r in self.rows if str(r.get(c) or "") >= v]
        return self

    def lte(self, c, v):
        self.rows = [r for r in self.rows if str(r.get(c) or "") <= v]
        return self

    def in_(self, c, vals):
        self.rows = [r for r in self.rows if r.get(c) in vals]
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _C:
    def __init__(self, t):
        self.t = t

    def table(self, n):
        return _Q(self.t.get(n, []))


TODAY = date(2026, 11, 11)  # Wednesday; lookahead starts Mon 2026-11-16


def _client():
    return _C({
        "entities": [{"id": "m", "name": "Marcello Grande"}, {"id": "d", "name": "Drew Fiero"},
                     {"id": "t", "name": "Tony Welch"}],
        "projects": [{"id": "p1", "code": "sap-5198"}, {"id": "p2", "code": "slt-5196"}],
        "sprint_allocations": [
            {"entity_id": "m", "project_id": "p1", "hours": 30, "week_start": "2026-11-16"},
            {"entity_id": "m", "project_id": "p2", "hours": 18, "week_start": "2026-11-16"},
            {"entity_id": "d", "project_id": "p1", "hours": 35, "week_start": "2026-11-16"},
            {"entity_id": "t", "project_id": "p2", "hours": 10, "week_start": "2026-11-16"},
            {"entity_id": "m", "project_id": "p1", "hours": 99, "week_start": "2026-11-09"},  # this week: not lookahead
        ],
        "commitments": [{"owner_name": "Drew Fiero", "due_date": "2026-11-25", "status": "open"}] * 6
        + [{"owner_name": "Drew Fiero", "due_date": "2026-11-25", "status": "done"}],
    })


def test_flags_hours_past_capacity_with_a_suggestion():
    look = W.lookahead(_client(), today=TODAY)
    m = next(p for p in look if p["person"] == "Marcello Grande")
    assert m["peak_hours"] == 48
    assert "48h allocated (capacity 40h)" in m["flags"][0]
    assert "could Tony Welch take some of sap-5198?" in m["flags"][0]


def test_flags_a_pile_of_due_commitments_and_ignores_closed_ones():
    look = W.lookahead(_client(), today=TODAY)
    d = next(p for p in look if p["person"] == "Drew Fiero")
    assert any("6 commitments due" in f for f in d["flags"])


def test_this_week_and_people_with_room_are_not_flagged():
    names = [p["person"] for p in W.lookahead(_client(), today=TODAY)]
    assert "Tony Welch" not in names


def test_render_is_empty_when_no_one_is_flagged():
    assert W.render([]) == []
    lines = W.render(W.lookahead(_client(), today=TODAY))
    assert lines[0].startswith("**Workload lookahead**")
    assert W.lookahead(None, today=TODAY) == []
