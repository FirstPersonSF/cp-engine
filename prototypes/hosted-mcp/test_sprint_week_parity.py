"""Hosted and engine must name the SAME sprint week (architecture plan step 1a).

WHY. The hosted server computed the sprint dir with a plain calendar
`isocalendar()` while the engine rolls Wed–Sun forward to next week
(`sprints._planning_monday`, the same rule as MC-2's `planningWeekMonday()`).
On Wed 2026-09-30 hosted said 2026-W40 and the engine 2026-W41, so
`get_project_state` served LAST week's sprint file as current while the tree
already had `sprints/2026-W41/`. This pins hosted to the engine's function.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine.sprints import current_sprint_week_iso  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


# A full week around the 09-30 disagreement, plus the ISO-year boundary
# (Wed 2026-12-30 plans 2027-W01; Mon 2026-12-28 is 2026-W53).
_DAYS = [date(2026, 9, 28) + timedelta(days=i) for i in range(7)] + [
    date(2026, 12, 28), date(2026, 12, 30), date(2027, 1, 3), date(2027, 1, 4),
]


@pytest.mark.parametrize("day", _DAYS, ids=lambda d: d.isoformat())
def test_hosted_sprint_week_is_the_engine_sprint_week(server, day):
    assert server.current_sprint_week(day) == current_sprint_week_iso(
        datetime(day.year, day.month, day.day, 12)
    )


def test_wednesday_2026_09_30_is_w41(server):
    """The concrete case from the review: hosted said W40."""
    assert server.current_sprint_week(date(2026, 9, 30)) == "2026-W41"
    assert server.current_sprint_week(date(2026, 9, 29)) == "2026-W40"


def test_default_clock_is_the_tenant_clock(server, monkeypatch):
    """No argument → the tenant's wall clock, rolled by the engine's rule."""
    monkeypatch.setattr(server, "tenant_now", lambda: datetime(2026, 9, 30, 9, 0))
    assert server.current_sprint_week() == "2026-W41"


def test_get_project_state_reads_the_planned_week_file(server, monkeypatch, tmp_path):
    """End to end through `find_sprint_file`: on Wednesday the tree's W41 file
    is current; the W40 one is last week's."""
    for week in ("2026-W40", "2026-W41"):
        d = tmp_path / "sprints" / week
        d.mkdir(parents=True)
        (d / "ggl-5168-activation.md").write_text(f"# {week}\n", encoding="utf-8")
    monkeypatch.setattr(server, "tenant_now", lambda: datetime(2026, 9, 30, 9, 0))
    path, week, note = server.find_sprint_file(tmp_path, "ggl-5168-activation", "ggl-5168")
    assert week == "2026-W41"
    assert path == tmp_path / "sprints" / "2026-W41" / "ggl-5168-activation.md"
    assert note is None
