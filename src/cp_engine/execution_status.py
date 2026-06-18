"""Derived execution status — the spine's "where does this work-item stand?"
signal (spine estimate-binding, Phase 4).

Status is DERIVED, never hand-stored, so it can't rot. It's computed from
signals the spine already has:

  - schedule bar dates  — a bar's `start_week`/`duration` mapped to calendar
    dates via the project `start_date` (Drew keeps bars current as work lands,
    so a bar's dates are trustworthy);
  - the `done` flag      — the one-click completion toggle on a bar; the ONLY
    non-derived signal — when true the item is done for certain;
  - live substance       — whether the item has confirmed live substance;
  - recent activity      — whether it got fresh substance versions recently.

`derive_status` applies five rules in priority order (see its docstring).
`item_has_recent_activity` is the recency signal that feeds rule 2.

Pure functions: plain data in, a `Status` out. No DB, no supabase client.
"""

from __future__ import annotations

from dataclasses import dataclass

from cp_engine.estimate import week_to_date


@dataclass(frozen=True)
class Status:
    status: str         # "done" | "active" | "next" | "flag" | "done?"
    basis: str          # human-readable reason, with real dates where available
    confidence: float


def _bar_field(bar, key, default=None):
    """Read a field from a bar that may be a dict OR an object."""
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def _bar_dates(bar, start_date):
    """A bar's (start_date, end_date) as calendar dates, or (None, None) when
    there's no calendar anchor. End = start_week + duration weeks out."""
    start_week = _bar_field(bar, "start_week")
    duration = _bar_field(bar, "duration") or 0
    start = week_to_date(start_date, start_week)
    end = week_to_date(start_date, None if start_week is None else start_week + duration)
    return start, end


def derive_status(bars, *, has_live_substance, recent_activity, start_date, today):
    """Derive a work-item's execution status from its schedule bars + substance.

    Rules, in PRIORITY order:
      1. any bar marked done           → "done"   (certain, confidence 1.0)
      2. a bar overlaps today, OR recent_activity → "active"
      3. all bars in the future AND no live substance → "next"
      4. a bar ended before today AND no substance AND not done → "flag"
      5. a bar ended before today WITH live substance, not toggled → "done?"

    When `start_date` is None there's no calendar anchor, so bar-vs-today can't
    be computed: fall back to the non-date signals — recent_activity or
    has_live_substance both imply work exists → "active"; otherwise "next".
    With no bars at all and no substance, the item is simply "next".
    """
    bars = list(bars or [])

    # Rule 1 — the explicit completion toggle wins over everything.
    if any(_bar_field(b, "done") for b in bars):
        return Status("done", "marked done on the schedule bar", 1.0)

    # No calendar anchor: can't compute bar-vs-today, use relative signals only.
    if start_date is None:
        if recent_activity:
            return Status(
                "active", "no kickoff date set — but fresh substance recently", 0.5
            )
        if has_live_substance:
            return Status(
                "active", "no kickoff date set — but live substance exists", 0.4
            )
        return Status("next", "no kickoff date set and nothing captured yet", 0.5)

    # Compute each bar's calendar span (anchored, so dates are real).
    spans = [_bar_dates(b, start_date) for b in bars]
    spans = [(s, e) for (s, e) in spans if s is not None and e is not None]

    # Rule 2 — a bar overlaps today, or substance landed recently → active.
    overlapping = next(((s, e) for (s, e) in spans if s <= today <= e), None)
    if overlapping is not None:
        s, e = overlapping
        return Status(
            "active",
            f"in progress — bar runs {s.isoformat()} → {e.isoformat()}",
            0.9,
        )
    if recent_activity:
        return Status("active", "fresh substance landed recently", 0.8)

    # No overlap and no recent activity. Classify by bar position vs today.
    if spans:
        all_future = all(s > today for (s, e) in spans)
        latest_end = max(e for (s, e) in spans)
        any_ended = any(e < today for (s, e) in spans)

        # Rule 3 — everything still ahead and no substance yet → next.
        if all_future and not has_live_substance:
            earliest = min(s for (s, e) in spans)
            return Status(
                "next",
                f"upcoming — bar starts {earliest.isoformat()}",
                0.8,
            )

        # A bar has ended in the past.
        if any_ended:
            # Rule 5 — past-ended with confirmed substance → proposed-done.
            if has_live_substance:
                return Status(
                    "done?",
                    f"looks done — bar ended {latest_end.isoformat()}, substance "
                    "confirmed; confirm on the bar",
                    0.7,
                )
            # Rule 4 — past-ended, nothing captured, not toggled → flag.
            return Status(
                "flag",
                f"bar ended {latest_end.isoformat()} but nothing captured — "
                "done? or needs a synthesis?",
                0.6,
            )

    # Fallbacks: bars exist but none ended/future-with-substance, or no dated
    # bars. Live substance ⇒ work exists (active); otherwise upcoming.
    if has_live_substance:
        return Status("active", "live substance exists", 0.5)
    return Status("next", "no schedule signal yet and nothing captured", 0.5)
