"""The master-CP one-liner is hand-written, and nothing refreshes it.

THE BUG THESE LOCK DOWN. `master-cp.md`'s summary column is derived from each
project CP's `## Exec Summary` region — hand-written prose. No sync, spine
write or ingest ever updates it. So a project can be worked on every day while
its summary sits untouched for months, and the index renders that old prose as
though it were current fact.

Measured on the live tenant 2026-09-14: SEVEN Google engagements carried an
identical `updated 2026-07-14` stamp while their spines had been authored that
same morning — a 62-day gap, completely invisible in the table. The projects
whose owner runs the local wrap-up ritual sat at 0-10 days; the projects whose
owner works only through hosted MCP (which has no verb that can write the
region) were all 41-62 days behind.

A stale summary is worse than an empty one: empty reads as "nobody has said",
stale reads as "this is how it is".
"""

from __future__ import annotations

from datetime import date

from cp_engine.summary import (
    STALE_AFTER_DAYS,
    exec_summary_updated_on,
    summary_stale_days,
)


def _cp_md(tmp_path, stamp: str | None) -> "object":
    """A project cp.md, optionally carrying an Exec Summary heading stamp."""
    heading = (
        f"## Exec Summary  ·  updated {stamp}\n" if stamp else "## Exec Summary\n"
    )
    f = tmp_path / "cp.md"
    f.write_text(
        "---\nProject: Test\n---\n\n"
        "## Facts\n| **Last touched** | 2026-08-10 |\n\n"
        + heading
        + "**Status:** Audit phase, the dominant workstream this week.\n"
    )
    return f


# ── the gap itself ────────────────────────────────────────────────────


def test_the_ggl_5136_case_is_flagged():
    """The real one: summary written 07-14, spine authored 09-14."""
    assert summary_stale_days(date(2026, 7, 14), date(2026, 9, 14)) == 62


def test_a_current_summary_is_not_flagged():
    """Written today, worked today — nothing to say."""
    assert summary_stale_days(date(2026, 9, 14), date(2026, 9, 14)) is None


def test_a_summary_newer_than_the_work_is_not_flagged():
    """slt-5196 on the live tenant: summary 09-08, last spine write 09-01.

    A summary written AFTER the work is current, not stale by -7 days. Without
    the guard this returns a negative and the template's truthiness check
    renders "-7d stale" on the most up-to-date project in the table.
    """
    assert summary_stale_days(date(2026, 9, 8), date(2026, 9, 1)) is None


def test_just_inside_the_threshold_is_silent():
    updated = date(2026, 8, 1)
    activity = date(2026, 8, 1 + STALE_AFTER_DAYS - 1)
    assert summary_stale_days(updated, activity) is None


def test_exactly_at_the_threshold_is_flagged():
    updated = date(2026, 8, 1)
    activity = date(2026, 8, 1 + STALE_AFTER_DAYS)
    assert summary_stale_days(updated, activity) == STALE_AFTER_DAYS


# ── unknown dates never guess ─────────────────────────────────────────


def test_unknown_summary_date_is_not_stale():
    """An unstamped summary is unknowable, not old.

    Guessing here would flag every pre-stamp project on the tenant at once,
    which trains the reader to ignore the marker.
    """
    assert summary_stale_days(None, date(2026, 9, 14)) is None


def test_unknown_activity_date_is_not_stale():
    assert summary_stale_days(date(2026, 7, 14), None) is None


# ── reading the stamp off disk ────────────────────────────────────────


def test_reads_the_heading_stamp(tmp_path):
    assert exec_summary_updated_on(_cp_md(tmp_path, "2026-07-14")) == date(
        2026, 7, 14
    )


def test_unstamped_summary_reads_as_none(tmp_path):
    assert exec_summary_updated_on(_cp_md(tmp_path, None)) is None


def test_missing_file_reads_as_none(tmp_path):
    assert exec_summary_updated_on(tmp_path / "nope.md") is None


def test_unparseable_stamp_reads_as_none(tmp_path):
    """A malformed date must not raise mid-sync; the whole run would die."""
    f = tmp_path / "cp.md"
    f.write_text("## Exec Summary  ·  updated 2026-13-99\n")
    assert exec_summary_updated_on(f) is None
