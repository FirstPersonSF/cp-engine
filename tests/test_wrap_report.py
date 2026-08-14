"""Tests for the wrap-report bundle (cp-engine #184).

The bundle exists because a hand-written retro silently drops the facts
nobody remembers to look up. ibx-5192's 2026-08-14 retro concluded "actual
hours: not captured" while `sprint_allocations` held 192.5 hours across four
people — so the tests below are written against that real shape.
"""
from __future__ import annotations

import datetime as dt

from cp_engine.wrap_report import (
    HUMAN_ENTRY_FIELDS,
    LEARNING_AXES,
    EffortSummary,
    WrapBundle,
    summarize_effort,
    summarize_meetings,
    unanswerable_fields,
)

# ── meeting cadence ───────────────────────────────────────────────────


def _mtg(day: str, minutes: int) -> dict:
    return {"meeting_date": f"{day}T16:00:00+00:00", "duration_minutes": minutes}


def test_meeting_load_totals_and_span():
    rows = [_mtg("2026-06-25", 68), _mtg("2026-07-20", 72), _mtg("2026-08-13", 28)]
    load = summarize_meetings(rows)
    assert load.count == 3
    assert load.total_minutes == 168
    assert load.total_hours == 2.8
    assert load.first == dt.date(2026, 6, 25)
    assert load.last == dt.date(2026, 8, 13)


def test_tail_share_surfaces_back_loaded_engagements():
    """The real ibx-5192 signature: most meeting time in the closing window.

    A headline count hides this completely — 39 meetings sounds evenly
    spread. The distribution is what says "the decisions got made at the end".
    """
    rows = (
        [_mtg("2026-06-25", 60)] * 2          # 120 min, early
        + [_mtg("2026-07-20", 60)] * 2        # 120 min, early
        + [_mtg("2026-08-12", 180)]           # 180 min, tail
        + [_mtg("2026-08-13", 300)]           # 300 min, tail
    )
    load = summarize_meetings(rows)
    assert load.tail_minutes == 480
    assert load.head_minutes == 240
    assert load.tail_share == 480 / 720
    assert load.tail_share > 0.6


def test_tail_window_anchors_on_the_last_meeting_not_today():
    """A wrap run weeks later must describe the ENGAGEMENT, not the silence.

    Anchoring on `today` would push every meeting outside the window and
    report a 0% tail share for a project that was in fact back-loaded.
    """
    rows = [_mtg("2026-06-01", 60), _mtg("2026-08-13", 300)]
    late = summarize_meetings(rows, today=dt.date(2026, 12, 25))
    assert late.tail_minutes == 300, "the closing burst must still count"
    assert late.tail_share > 0.8


def test_heaviest_days_are_ranked_by_minutes():
    rows = [
        _mtg("2026-08-06", 100), _mtg("2026-08-06", 210),   # 310
        _mtg("2026-08-04", 237),
        _mtg("2026-08-05", 141),
    ]
    load = summarize_meetings(rows)
    assert load.heaviest_days[0] == ("2026-08-06", 2, 310)
    assert [d for d, _, _ in load.heaviest_days][:3] == [
        "2026-08-06", "2026-08-04", "2026-08-05",
    ]


def test_malformed_rows_do_not_break_the_bundle():
    """One odd row must never fail a wrap report."""
    rows = [
        _mtg("2026-08-01", 60),
        {"meeting_date": None, "duration_minutes": 30},
        {"meeting_date": "not-a-date", "duration_minutes": 30},
        {"meeting_date": "2026-08-02T00:00:00Z", "duration_minutes": "oops"},
    ]
    load = summarize_meetings(rows)
    assert load.count == 2          # the two parseable dates
    assert load.total_minutes == 60  # the bad duration counts as 0, not a crash


def test_no_meetings_is_an_empty_summary_not_a_crash():
    load = summarize_meetings([])
    assert load.count == 0
    assert load.total_hours == 0.0
    assert load.tail_share == 0.0
    assert load.first is None


# ── effort ────────────────────────────────────────────────────────────


def test_effort_rolls_up_by_person():
    """The real ibx-5192 numbers the hand-written retro missed."""
    names = {"e1": "Geoff Ahmann", "e2": "Drew Fiero", "e3": "Marcello Grande"}
    rows = [
        {"entity_id": "e1", "hours": 40, "week_start": "2026-08-03"},
        {"entity_id": "e1", "hours": 48, "week_start": "2026-08-10"},
        {"entity_id": "e2", "hours": 45.5, "week_start": "2026-08-03"},
        {"entity_id": "e3", "hours": 43, "week_start": "2026-08-10"},
    ]
    eff = summarize_effort(rows, names)
    assert eff.total_hours == 176.5
    assert eff.by_person[0] == ("Geoff Ahmann", 88.0)
    assert eff.weeks == 2


def test_unknown_entity_is_attributed_not_dropped():
    """Hours with no matching person still count toward the total.

    Dropping them would understate effort — the opposite of the bug this
    bundle exists to prevent.
    """
    eff = summarize_effort([{"entity_id": "ghost", "hours": 12}], {})
    assert eff.total_hours == 12.0
    assert eff.by_person == [("unattributed", 12.0)]


# ── bundle-level derivations ──────────────────────────────────────────


def test_duration_falls_back_to_the_last_meeting_when_no_end_date():
    """MC-2's `projects` has no end_date column — the last meeting stands in."""
    bundle = WrapBundle(
        code="ibx-5192",
        start_date=dt.date(2026, 6, 22),
        meetings=summarize_meetings([_mtg("2026-08-13", 28)]),
    )
    assert bundle.duration_days == 52
    assert bundle.duration_weeks == 7.4


def test_budget_per_hour_needs_both_sides():
    """A made-up denominator is worse than a blank."""
    no_hours = WrapBundle(code="x", budget=38000.0)
    assert no_hours.budget_per_hour is None

    no_budget = WrapBundle(
        code="x", effort=EffortSummary(total_hours=192.5)
    )
    assert no_budget.budget_per_hour is None

    both = WrapBundle(
        code="ibx-5192",
        budget=38000.0,
        effort=EffortSummary(total_hours=192.5),
    )
    assert both.budget_per_hour == 197.4


def test_human_entry_fields_are_never_auto_filled():
    """The load-bearing contract ported from social-builder.

    These are exactly the fields a model would happily hallucinate. If this
    list ever shrinks silently, a wrap report starts inventing margins and
    licensing clearances.
    """
    for field_name in (
        "Work-page candidate (Yes/No)",
        "OK to post publicly (Yes/No)",
        "Project rating (1-5)",
        "Non-royalty-free content / talent / music licensing",
    ):
        assert field_name in HUMAN_ENTRY_FIELDS


def test_unanswerable_fields_names_the_gaps():
    """Naming a gap is the feature — a silent omission is the bug."""
    thin = WrapBundle(code="x")
    missing = unanswerable_fields(thin)
    assert "Project rating (1-5)" in missing
    assert "Actual profitability %" in missing


def test_profitability_stops_being_a_pure_blank_once_hours_exist():
    rich = WrapBundle(
        code="ibx-5192",
        budget=38000.0,
        effort=EffortSummary(total_hours=192.5),
    )
    missing = unanswerable_fields(rich)
    assert not any(f.startswith("Actual profitability") for f in missing)
    # ...but the judgement calls stay human.
    assert "Project rating (1-5)" in missing


def test_four_learning_axes_including_the_client():
    """One 'lessons' blob loses the axis that compounds across engagements."""
    keys = [k for k, _ in LEARNING_AXES]
    assert keys == ["project", "client", "vendors", "scope_budget"]


def test_effort_verified_flag_distinguishes_unread_from_empty():
    """Same None-vs-empty discipline as close_out's commitments: 'we could
    not read the table' must not render as 'this project used no hours'."""
    unread = EffortSummary(verified=False)
    assert unread.total_hours == 0.0
    assert unread.verified is False

    read_and_empty = summarize_effort([], {})
    assert read_and_empty.total_hours == 0.0
    assert read_and_empty.verified is True
