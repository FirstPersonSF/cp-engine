"""#337 / #338 — horizon items after #331 made them settle-able.

#337: a carried horizon item rendered only its first line; its indented
continuation (kept as `note`) was dropped.
#338: the sprint index, agenda and prep-planning still counted settled
(`[done · …]` / struck) decision items.
"""

from __future__ import annotations

from cp_engine import prep_planning, sprints
from cp_engine.state import HorizonItem

BODY = """\
## Horizon

### Milestones

### Decisions due

- [by W42] **Still open.**
- [done · by W40] **Settled by token.**
- ~~[by W40] **Settled by strike.**~~

### Opportunities

- [opportunity] **The wall is reusable.** Forty / thirty-five /
  twenty-five across the territories —
  a hedge against the copy-volume problem.
"""


def test_prep_planning_skips_settled_decisions():
    got = prep_planning._parse_decisions_due_from_body(BODY)
    assert [d["text"] for d in got] == ["**Still open.**"]
    assert got[0]["target_date"] == "by W42"


def test_parse_horizon_marks_settled_and_keeps_the_continuation():
    items = sprints._parse_horizon(BODY)
    decisions = [h for h in items if h.bucket == "decision"]
    assert [h.is_open for h in decisions] == [True, False, False]
    opp = next(h for h in items if h.bucket == "opportunity")
    assert "twenty-five" in (opp.note or "")


def _sf_with_decisions():
    from datetime import date  # noqa: F401
    from tests.test_aggregators import _make_sprint

    return _make_sprint(horizon=(
        HorizonItem(text="Open one", bucket="decision"),
        HorizonItem(text="Done one", bucket="decision", status="done"),
    ))


def test_sprint_index_counts_only_open_decisions():
    out = sprints.render_sprint_index(
        week_iso="2026-W20", week_dates="May 11–17", sprint_files=[_sf_with_decisions()]
    )
    row = next(ln for ln in out.splitlines() if "ggl-5168" in ln)
    assert row.split("|")[5].strip() == "1"


def test_agenda_decisions_due_skip_settled():
    from datetime import date

    from cp_engine import agenda

    got = agenda._extract_decisions_due_for_project(
        "ggl-5168", (_sf_with_decisions(),), date(2026, 5, 12)
    )
    assert [d["text"] for d in got] == ["Open one"]


def test_master_cp_rollup_skips_settled():
    from datetime import date

    from cp_engine.aggregators import carry_forward_rollup

    rollup = carry_forward_rollup((_sf_with_decisions(),), date(2026, 5, 12))
    assert [d["text"] for d in rollup["decisions_due"]] == ["Open one"]
