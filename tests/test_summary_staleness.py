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
    MAX_SUMMARY_LEN,
    STALE_AFTER_DAYS,
    exec_summary_updated_on,
    latest_dated_activity,
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


# ── surfacing the newest recorded signal (option (b), issue #251) ──────
#
# `summary_stale_days` says a summary stopped tracking the work; it cannot say
# what the work IS now. That answer already exists — auto-ingest writes dated
# `[decision · YYYY-MM-DD]` bullets into the sprint file on every meeting — but
# no renderer read it. Measured 2026-09-14: all 13 stale engagements had a
# decision NEWER than their summary. ggl-5136's summary said 2026-07-14 while
# its sprint file carried a 2026-09-01 decision.
#
# This surfaces a sentence a person already wrote. It authors nothing: the
# engine owns scaffold/read/render, the model owns all prose
# (docs/plans/2026-06-30-exec-summary.md).

from cp_engine.summary import latest_recorded_signal


def _sprint(tmp_path, week: str, code: str, decisions: str) -> None:
    d = tmp_path / "sprints" / week
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{code}.md").write_text(
        "---\n"
        f"Project: {code} — Test\n"
        f"Filename: sprints/{week}/{code}.md\n"
        f"Sprint: {week}\n"
        "PriorSprint: \n"
        "---\n\n"
        f"# {code} · Sprint {week}\n\n"
        "## Meeting notes & decisions\n\n"
        "### Decisions\n"
        f"{decisions}\n"
    )


def test_finds_the_newest_decision(tmp_path):
    _sprint(tmp_path, "2026-W36", "ggl-5136",
            "- [decision · 2026-08-31] Tony delivered the first batch.\n"
            "- [decision · 2026-09-01] Rolling the name-change revision in.")
    got = latest_recorded_signal(tmp_path, "ggl-5136")
    assert got is not None
    assert got[1] == date(2026, 9, 1)
    assert "name-change" in got[0]


def test_picks_the_newest_across_weeks(tmp_path):
    _sprint(tmp_path, "2026-W36", "p", "- [decision · 2026-08-31] older\n")
    _sprint(tmp_path, "2026-W38", "p", "- [decision · 2026-09-10] newer\n")
    got = latest_recorded_signal(tmp_path, "p")
    assert got[0] == "newer" and got[1] == date(2026, 9, 10)


def test_newer_than_filters_out_stale_bullets(tmp_path):
    """A current summary must never be second-guessed by an OLDER decision."""
    _sprint(tmp_path, "2026-W36", "p", "- [decision · 2026-08-31] old news\n")
    assert latest_recorded_signal(
        tmp_path, "p", newer_than=date(2026, 9, 8)
    ) is None


def test_newer_than_keeps_a_genuinely_newer_bullet(tmp_path):
    _sprint(tmp_path, "2026-W38", "p", "- [decision · 2026-09-10] real news\n")
    got = latest_recorded_signal(tmp_path, "p", newer_than=date(2026, 9, 8))
    assert got is not None and got[0] == "real news"


def test_no_sprint_dir_is_none(tmp_path):
    assert latest_recorded_signal(tmp_path, "p") is None


def test_project_with_no_sprint_file_is_none(tmp_path):
    _sprint(tmp_path, "2026-W38", "other", "- [decision · 2026-09-10] x\n")
    assert latest_recorded_signal(tmp_path, "p") is None


def test_malformed_date_is_skipped_not_raised(tmp_path):
    """Bullet dates are model-written; a bad one is normal input, not an error."""
    _sprint(tmp_path, "2026-W38", "p",
            "- [decision · not-a-date] broken\n"
            "- [decision · 2026-09-10] good\n")
    got = latest_recorded_signal(tmp_path, "p")
    assert got is not None and got[0] == "good"


def test_unparseable_sprint_file_does_not_break_the_sync(tmp_path):
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "p.md").write_text("\x00 not markdown at all")
    assert latest_recorded_signal(tmp_path, "p") is None


def test_signal_is_length_capped(tmp_path):
    _sprint(tmp_path, "2026-W38", "p",
            f"- [decision · 2026-09-10] {'x' * 400}\n")
    got = latest_recorded_signal(tmp_path, "p")
    assert got is not None and len(got[0]) <= MAX_SUMMARY_LEN


# ──────────────────────────────────────────────────────────────────────
#  #260 — "Last activity" dates the WORK, not the job record
# ──────────────────────────────────────────────────────────────────────


def test_last_activity_finds_the_newest_dated_bullet(tmp_path):
    _sprint(tmp_path, "2026-W36", "ibx-5153", "- `[decision · 2026-08-31]` Old.")
    _sprint(tmp_path, "2026-W38", "ibx-5153", "- `[decision · 2026-09-14]` New.")
    assert latest_dated_activity(tmp_path, "ibx-5153") == date(2026, 9, 14)


def test_last_activity_differentiates_where_mtime_collapses(tmp_path):
    """THE POINT OF #260, and why the mtime attempt was reverted.

    Both sprint files are WRITTEN NOW, so their mtimes are identical — exactly
    what a tenant-wide ingest produces. The human-assigned dates inside them
    still differ, so the column separates a project that moved yesterday from
    one that has not moved since August. This is the assertion the reverted
    mtime implementation could not make.
    """
    _sprint(tmp_path, "2026-W38", "ibx-5153", "- `[decision · 2026-09-14]` Moved.")
    _sprint(tmp_path, "2026-W38", "ggl-5185", "- `[decision · 2026-08-12]` Quiet.")

    assert latest_dated_activity(tmp_path, "ibx-5153") == date(2026, 9, 14)
    assert latest_dated_activity(tmp_path, "ggl-5185") == date(2026, 8, 12)


def test_last_activity_is_none_when_nothing_is_dated(tmp_path):
    """6 of the rendered projects have no dated bullet. None -> em dash.

    Inventing a date for these is the failure mode #260 documents: a column
    where every row looks current is worse than one that admits it cannot say.
    """
    _sprint(tmp_path, "2026-W38", "cp-engine", "- A decision with no date marker.")
    assert latest_dated_activity(tmp_path, "cp-engine") is None


def test_last_activity_is_none_for_an_unknown_project(tmp_path):
    (tmp_path / "sprints").mkdir()
    assert latest_dated_activity(tmp_path, "does-not-exist") is None


def test_last_activity_reads_more_than_decisions(tmp_path):
    """A project can be moving without deciding anything.

    Restricting to `[decision · …]` (what `latest_recorded_signal` does, for
    its own good reasons) would blank a project whose week produced open asks
    and client mail but no decision.
    """
    d = tmp_path / "sprints" / "2026-W38"
    d.mkdir(parents=True)
    (d / "slt-5196.md").write_text(
        "---\n"
        "Project: slt-5196 — Test\n"
        "Filename: sprints/2026-W38/slt-5196.md\n"
        "Sprint: 2026-W38\n"
        "PriorSprint: \n"
        "---\n\n"
        "# slt-5196 · Sprint 2026-W38\n\n"
        "## Client communication\n\n"
        "### Open asks\n"
        "- [open · 2026-09-16 · Janet] Send the estimate.\n\n"
        "## Meeting notes & decisions\n\n"
        "### Decisions\n"
        "- `[decision · 2026-09-02]` Older than the ask above.\n"
    )
    assert latest_dated_activity(tmp_path, "slt-5196") == date(2026, 9, 16)


def test_last_activity_survives_a_malformed_sprint_file(tmp_path):
    """One bad file must not break a tenant-wide sync; the surface is advisory."""
    d = tmp_path / "sprints" / "2026-W37"
    d.mkdir(parents=True)
    (d / "ibx-5153.md").write_text("not a sprint file at all")
    _sprint(tmp_path, "2026-W38", "ibx-5153", "- `[decision · 2026-09-14]` Fine.")
    assert latest_dated_activity(tmp_path, "ibx-5153") == date(2026, 9, 14)
