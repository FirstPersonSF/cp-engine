"""#156: the ingest target week derives from the plan's entry dates, not
from `today`. `_planning_monday`'s Wed→next-Monday roll is a PLANNING
anchor; applying it to ingest routed a Tuesday meeting ingested on
Wednesday into the following sprint's dir. Also covers the `_week.md`
auto-scaffold: themes must not be dropped when ingest races ahead of
sync into a week dir that doesn't exist yet.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from cp_engine.ingest import _calendar_week_iso, execute_plan, plan_week_iso


def _plan_with_dates(*dates: str) -> dict:
    return {
        "transcript": {"source": "file", "path": "t.txt"},
        "projects": {
            "ggl-5168-activation": {
                "inbound": [
                    {"text": f"update {i}", "date": d, "who": "Tony"}
                    for i, d in enumerate(dates)
                ],
            },
        },
    }


def _scaffold_week(root: Path, week_iso: str, code: str) -> Path:
    d = root / "sprints" / week_iso
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{code}.md"
    p.write_text(
        "# Sprint\n\n## Client communication\n\n### Inbound\n\n"
        "### Open asks\n\n## Meeting notes & decisions\n\n### Decisions\n"
    )
    return p


def test_plan_week_iso_uses_entry_dates():
    # Tuesday 2026-08-04 is in W32 no matter what day the ingest runs.
    assert plan_week_iso(_plan_with_dates("2026-08-04")) == "2026-W32"


def test_plan_week_iso_most_common_date_wins():
    plan = _plan_with_dates("2026-08-04", "2026-08-04", "2026-08-11")
    assert plan_week_iso(plan) == "2026-W32"


def test_plan_week_iso_none_without_dates():
    plan = {
        "transcript": {"source": "file", "path": "t.txt"},
        "projects": {
            "ggl-5168-activation": {
                "stakeholders": [{"name": "Gustavo"}],
            },
        },
    }
    assert plan_week_iso(plan) is None


def test_calendar_week_has_no_wednesday_roll():
    # Wed 2026-08-05: the planning roll would say W33; ingest must say W32.
    assert _calendar_week_iso(date(2026, 8, 5)) == "2026-W32"


def test_execute_plan_routes_by_meeting_date_not_today(tmp_path):
    # Meeting Tue 2026-08-04 (W32), ingested Wed 2026-08-05: entries land
    # in W32 even though the planning roll would target W33.
    sprint = _scaffold_week(tmp_path, "2026-W32", "ggl-5168-activation")
    result = execute_plan(
        _plan_with_dates("2026-08-04"),
        tenant_root=tmp_path,
        today=date(2026, 8, 5),
    )
    assert result.errors == []
    assert sprint in result.files_written
    assert "update 0" in sprint.read_text()


def test_week_override_still_wins(tmp_path):
    sprint = _scaffold_week(tmp_path, "2026-W31", "ggl-5168-activation")
    result = execute_plan(
        _plan_with_dates("2026-08-04"),
        tenant_root=tmp_path,
        today=date(2026, 8, 5),
        week_iso="2026-W31",
    )
    assert result.errors == []
    assert sprint in result.files_written


def test_themes_scaffold_missing_week_md(tmp_path):
    _scaffold_week(tmp_path, "2026-W32", "ggl-5168-activation")
    plan = _plan_with_dates("2026-08-04")
    plan["themes"] = [{"text": "Google reorg gates the account", "date": "2026-08-04"}]
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 8, 5))
    week_md = tmp_path / "sprints" / "2026-W32" / "_week.md"
    assert result.errors == []
    assert week_md.exists()
    body = week_md.read_text()
    assert "Google reorg gates the account" in body
    assert "Sprint W32" in body
