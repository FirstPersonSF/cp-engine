"""Tests for cp_engine.agenda — v0.8.8 sprint planning agenda renderer."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cp_engine.agenda import (
    WeeklyDecision,
    decisions_for_project,
    extract_quick_resume,
    parse_weekly_decisions,
)


# ──────────────────────────────────────────────────────────────────────
#  parse_weekly_decisions
# ──────────────────────────────────────────────────────────────────────


def test_parse_weekly_decisions_extracts_numbered_entries() -> None:
    body = """
## Decisions (cross-cutting, last 4 weeks)

19. **Marcello hours triage for W19** — drop website work this week. (2026-05-11, source: sprint planning)

18. **Begin transition off the Claude team plan** for Brandon and Marcello. (2026-05-11, source: sprint planning)

3. **Drew handles Firebase ownership transfer** for Go Safety. (2026-05-08, source: ggl-5136)
"""
    decisions = parse_weekly_decisions(body)
    assert len(decisions) == 3
    assert decisions[0].number == 19
    assert decisions[0].date == "2026-05-11"
    assert decisions[0].sources == ("sprint planning",)
    assert decisions[2].number == 3
    assert decisions[2].sources == ("ggl-5136",)


def test_parse_weekly_decisions_handles_multi_source() -> None:
    body = """
8. **Infoblox AI-campaign workshop downsized** to 2026-06-08 sessions. (2026-05-07, source: ibx-5167 / ibx-5153)
"""
    decisions = parse_weekly_decisions(body)
    assert len(decisions) == 1
    assert decisions[0].sources == ("ibx-5167", "ibx-5153")


def test_parse_weekly_decisions_stops_at_engine_marker() -> None:
    """Decisions auto-aggregated by the v0.8.5 decisions-strip region
    should NOT be picked up here (they're already surfaced via
    aggregators.aggregate_tenant_strips). Parser truncates at the
    first cp-engine marker so we only consume handwritten content."""
    body = """
3. **Drew handles Firebase** for Go Safety. (2026-05-08, source: ggl-5136)

<!-- cp-engine:start decisions-strip -->
## Decisions (cross-cutting, auto-aggregated)
99. **Auto-decision** body. (2026-05-12, source: ggl-5168)
<!-- cp-engine:end decisions-strip -->
"""
    decisions = parse_weekly_decisions(body)
    # Only #3 (above the marker) — #99 is inside the engine region.
    assert len(decisions) == 1
    assert decisions[0].number == 3


def test_parse_weekly_decisions_returns_empty_for_no_matches() -> None:
    assert parse_weekly_decisions("") == ()
    assert parse_weekly_decisions("just some text\nwith no decisions") == ()


# ──────────────────────────────────────────────────────────────────────
#  decisions_for_project
# ──────────────────────────────────────────────────────────────────────


def test_decisions_for_project_filters_by_source_code() -> None:
    decisions = (
        WeeklyDecision(
            number=1, text="Maria off", date="2026-05-08",
            sources=("weekly account meeting",),
        ),
        WeeklyDecision(
            number=3, text="Firebase transfer", date="2026-05-08",
            sources=("ggl-5136",),
        ),
        WeeklyDecision(
            number=8, text="Workshop downsized", date="2026-05-07",
            sources=("ibx-5167", "ibx-5153"),
        ),
    )
    assert [d.number for d in decisions_for_project("ggl-5136", decisions)] == [3]
    assert [d.number for d in decisions_for_project("ibx-5167", decisions)] == [8]
    assert [d.number for d in decisions_for_project("ibx-5153", decisions)] == [8]
    # Code that nothing references → empty.
    assert decisions_for_project("ggl-5168", decisions) == ()


def test_decisions_for_project_is_case_insensitive() -> None:
    decisions = (
        WeeklyDecision(
            number=3, text="x", date="2026-05-08", sources=("GGL-5136",),
        ),
    )
    assert len(decisions_for_project("ggl-5136", decisions)) == 1
    assert len(decisions_for_project("GGL-5136", decisions)) == 1


# ──────────────────────────────────────────────────────────────────────
#  extract_quick_resume
# ──────────────────────────────────────────────────────────────────────


def test_extract_quick_resume_pulls_first_paragraph() -> None:
    body = """
## Quick Resume

**Last meeting:** 2026-05-08 — Maria/Brandon weekly account meeting.
**Current work:** Pop-up Round 3 shared with Rena.
**Next up:** Wait for Rena Round 3 feedback.
**Blockers:** Awaiting Rena.

## Current Work

Other content.
"""
    out = extract_quick_resume(body)
    assert out is not None
    assert "Last meeting" in out
    assert "Pop-up Round 3" in out
    assert "Other content" not in out  # next H2 not included


def test_extract_quick_resume_returns_none_for_template_placeholders() -> None:
    """Project cp.md files scaffolded but not yet deepened have only the
    template placeholders (`_<date>_`, etc.). Don't surface those."""
    body = """
## Quick Resume

**Last session:** _<date>_
**Current work:** _<what's in flight right now>_
**Next up:** _<next 1-3 concrete actions>_
**Blockers:** _<or "None">_
"""
    assert extract_quick_resume(body) is None


def test_extract_quick_resume_returns_none_for_missing_section() -> None:
    body = "## Some Other Section\n\ncontent"
    assert extract_quick_resume(body) is None


def test_extract_quick_resume_strips_template_lines_when_mixed() -> None:
    """If the section has both real content AND placeholder lines, keep
    the real content and drop the placeholders."""
    body = """
## Quick Resume

**Current work:** Real ongoing work here.
**Last session:** _<date>_
**Blockers:** _<or "None">_

## Next Section
"""
    out = extract_quick_resume(body)
    assert out is not None
    assert "Real ongoing work" in out
    assert "_<date>_" not in out
