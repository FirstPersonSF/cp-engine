# tests/test_sprint_deliverable_block.py — the sprint file's engine-managed
# deliverable-cards region (the last consumer of the cards design).
from datetime import datetime, timezone

from cp_engine.sprints import CarryForward, render_sprint_scaffold
from cp_engine.state import ProjectState


def _project(source="projects"):
    return ProjectState(
        code="sap-5174", name="Vision Update 2026", source=source,
        company_kind="client", company_code="SAP", company_name="SAP Concur",
        status="Open", is_internal=False, owner="drew",
        last_touched=datetime(2026, 7, 11, tzinfo=timezone.utc),
        deadline=None,
    )


def _render(**kw):
    return render_sprint_scaffold(
        project=kw.pop("project", _project()),
        week_iso="2026-W29", week_label="W29",
        week_start="2026-07-13", week_end="2026-07-17",
        prior_sprint="2026-W28", last_sprint_hours_line=None,
        sessions_this_week=0, last_session_date=None, last_session_who=None,
        last_session_summary=None, recent_commits=(), open_issues=(),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        **kw,
    )


def test_deliverable_lines_render_inside_engine_region():
    body = _render(deliverable_lines=(
        "P&P Report · due ~2026-08-24 · 1 output accrued",
        "UNF · due ~2026-10-26",
    ))
    start = body.index("<!-- cp-engine:start deliverable-cards -->")
    end = body.index("<!-- cp-engine:end deliverable-cards -->")
    region = body[start:end]
    assert "- P&P Report · due ~2026-08-24 · 1 output accrued" in region
    assert "- UNF · due ~2026-10-26" in region
    # the hand-written stub survives BELOW the region
    assert body.index("_<top deliverable>_") > end


def test_empty_lines_render_placeholder_not_blank_region():
    body = _render(deliverable_lines=())
    assert "_(no deliverables in the estimate yet)_" in body


def test_initiative_scaffold_has_no_cards_region():
    body = _render(project=_project(source="initiative"))
    assert "deliverable-cards" not in body
