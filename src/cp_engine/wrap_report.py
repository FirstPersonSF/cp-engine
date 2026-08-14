"""Wrap-report bundle — the deterministic raw material for a close-out retro.

Ported in shape (not code) from social-builder-app's `generate_wrap_report`
(cp-engine #184). What was worth taking from it:

  * an explicit AI-FILLS / HUMAN-FILLS split, where the human fields ship as
    visible labelled placeholders rather than being silently omitted. A model
    must never invent a budget, a margin, a licensing status or a rating, and
    a blank nobody can see is a blank nobody fills.
  * FOUR separate learning axes — project, client, vendors, scope+budget —
    instead of one "lessons" blob. The client axis is the one with no home in
    cp today and the one that compounds across engagements.
  * adversarial sections by contract: challenges, what-to-reconsider,
    relationship-strength. A retro that only records wins is worth nothing.

What was deliberately NOT taken: its Airtable -> ClickUp -> Slack integration
layer. cp retired ClickUp (mig 096) and never had Airtable. The equivalent
facts live in MC-2 and the spine, and they are richer — so this bundle can
auto-fill MORE than the original could, from better sources, with no new
dependency.

This module is the ENGINE half only: it gathers facts and never writes prose.
The model synthesizes the report from the bundle in-session (the `/cp-prep`
pattern), so the author can defend and revise it live.

The motivating case: ibx-5192's retro was written by hand on 2026-08-14 and
concluded "actual hours: not captured". `sprint_allocations` held 192.5 hours
across four people the whole time. A bundle would not have let that through.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Fields the model MUST NOT invent. Each ships as a labelled placeholder so a
# human sees a prompt rather than an omission (social-builder's core idea).
HUMAN_ENTRY_FIELDS: tuple[str, ...] = (
    "Actual profitability %",
    "Work-page candidate (Yes/No)",
    "OK to post publicly (Yes/No)",
    "Project rating (1-5)",
    "Non-royalty-free content / talent / music licensing",
    "Link to final client-held artifact",
)

# The learning axes, in the order the report renders them. Four, not one.
LEARNING_AXES: tuple[tuple[str, str], ...] = (
    ("project", "What we learned about the project — what worked, what was "
                "challenging, key decisions, how the work evolved"),
    ("client", "What we learned about the client — communication style, "
               "decision-making, feedback patterns, who could actually end a "
               "round"),
    ("vendors", "What we learned about freelancers/vendors — who performed, "
                "delivery reliability, what to change next time"),
    ("scope_budget", "What we learned about scope & budget — was the original "
                     "scope realistic, what changed, what would we price "
                     "differently"),
)


def _as_date(value: Any) -> _dt.date | None:
    """Parse a date or ISO-ish timestamp defensively. None on anything else."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class MeetingLoad:
    """Meeting cadence — count, hours, and WHERE they fell in the timeline.

    The distribution is the point, not the total. On ibx-5192, 66% of all
    meeting time landed in the final two weeks of a 7.6-week engagement —
    which is the signature of decisions being made at the end instead of the
    beginning, and is invisible in a headline count.
    """

    count: int = 0
    total_minutes: int = 0
    first: _dt.date | None = None
    last: _dt.date | None = None
    # (date, count, minutes), heaviest-first — the days worth explaining.
    heaviest_days: list[tuple[str, int, int]] = field(default_factory=list)
    # minutes in the final `tail_days` window vs everything before it
    tail_minutes: int = 0
    head_minutes: int = 0
    tail_days: int = 14

    @property
    def total_hours(self) -> float:
        return round(self.total_minutes / 60.0, 1)

    @property
    def tail_share(self) -> float:
        """Fraction of meeting MINUTES in the closing window (0.0-1.0)."""
        if not self.total_minutes:
            return 0.0
        return self.tail_minutes / self.total_minutes


def summarize_meetings(
    rows: list[dict], *, tail_days: int = 14, today: _dt.date | None = None
) -> MeetingLoad:
    """Fold fathom_meetings rows into a cadence summary.

    Tolerates missing/malformed dates and durations — a wrap report must not
    fail because one meeting row is odd.

    `today` is accepted for signature symmetry with the rest of the engine's
    date-taking helpers and is DELIBERATELY not used to place the tail window:
    see the cutoff comment below. It is kept rather than dropped so callers
    that thread a pinned date through every helper don't special-case this one.
    """
    dated: list[tuple[_dt.date, int]] = []
    for r in rows:
        day = _as_date(r.get("meeting_date"))
        if day is None:
            continue
        try:
            minutes = int(r.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        dated.append((day, max(0, minutes)))

    if not dated:
        return MeetingLoad(tail_days=tail_days)

    dated.sort()
    first, last = dated[0][0], dated[-1][0]
    # The window ALWAYS closes on the last meeting, never on today. A wrap run
    # weeks after delivery must describe the engagement, not the silence since:
    # anchoring on `today` slides the window past every meeting and reports a
    # 0% tail share for a project that was in fact entirely back-loaded.
    # `today` is accepted only so callers/tests can pin it; it never moves the
    # window.
    cutoff = last - _dt.timedelta(days=tail_days)

    per_day_minutes: Counter[str] = Counter()
    per_day_count: Counter[str] = Counter()
    tail = head = 0
    for day, minutes in dated:
        key = day.isoformat()
        per_day_minutes[key] += minutes
        per_day_count[key] += 1
        if day > cutoff:
            tail += minutes
        else:
            head += minutes

    heaviest = sorted(
        ((d, per_day_count[d], m) for d, m in per_day_minutes.items()),
        key=lambda t: (-t[2], t[0]),
    )[:5]

    return MeetingLoad(
        count=len(dated),
        total_minutes=sum(m for _, m in dated),
        first=first,
        last=last,
        heaviest_days=heaviest,
        tail_minutes=tail,
        head_minutes=head,
        tail_days=tail_days,
    )


@dataclass(frozen=True)
class EffortSummary:
    """Recorded hours by person, from `sprint_allocations`.

    NOTE the honesty constraint: these are ALLOCATED hours, which is what
    MC-2 records. They are not timesheet actuals. The renderer must say so —
    presenting an allocation as an actual is the kind of quiet overstatement
    that makes a margin number worse than no number.
    """

    total_hours: float = 0.0
    by_person: list[tuple[str, float]] = field(default_factory=list)
    weeks: int = 0
    # True when we read rows; False when the table was unreadable. Distinct
    # from "read it and found zero" — same None-vs-empty discipline as
    # close_out's commitments.
    verified: bool = True


def summarize_effort(
    rows: list[dict], names: dict[str, str] | None = None
) -> EffortSummary:
    """Fold sprint_allocations rows into per-person hours."""
    names = names or {}
    by: Counter[str] = Counter()
    weeks: set[str] = set()
    total = 0.0
    for r in rows:
        try:
            hours = float(r.get("hours") or 0)
        except (TypeError, ValueError):
            continue
        who = names.get(str(r.get("entity_id")), "unattributed")
        by[who] += hours
        total += hours
        if r.get("week_start"):
            weeks.add(str(r["week_start"])[:10])
    return EffortSummary(
        total_hours=round(total, 1),
        by_person=[(n, round(h, 1)) for n, h in by.most_common()],
        weeks=len(weeks),
        verified=True,
    )


@dataclass(frozen=True)
class WrapBundle:
    """Everything the model needs to synthesize the wrap report.

    Facts only. No prose, no judgement — those are the model's job, and
    keeping them out of here is what lets the report be defended live.
    """

    code: str
    name: str = ""
    status: str = ""
    start_date: _dt.date | None = None
    end_date: _dt.date | None = None
    budget: float | None = None
    target_profit_pct: float | None = None
    account_manager: str = ""
    meetings: MeetingLoad = field(default_factory=MeetingLoad)
    effort: EffortSummary = field(default_factory=EffortSummary)
    deliverables: list[str] = field(default_factory=list)
    # Elements absorbed into a shipped deliverable — what each round consumed.
    sealed_inputs: list[tuple[str, str]] = field(default_factory=list)
    open_commitments: list[dict] | None = None
    exec_summary: str = ""
    feedback_artifacts: list[str] = field(default_factory=list)
    spine_verified: bool = True

    @property
    def duration_days(self) -> int | None:
        if self.start_date is None:
            return None
        end = self.end_date or self.meetings.last
        if end is None:
            return None
        return (end - self.start_date).days

    @property
    def duration_weeks(self) -> float | None:
        days = self.duration_days
        return None if days is None else round(days / 7.0, 1)

    @property
    def budget_per_hour(self) -> float | None:
        """Budget ÷ recorded hours — the crude number that starts the
        margin conversation. None when either side is missing, because a
        made-up denominator is worse than a blank."""
        if not self.budget or not self.effort.total_hours:
            return None
        return round(self.budget / self.effort.total_hours, 2)


def unanswerable_fields(bundle: WrapBundle) -> list[str]:
    """Which HUMAN_ENTRY_FIELDS this bundle genuinely cannot fill.

    Rendered as an explicit "not assessed" block. Naming the gap is the
    feature — ibx-5192's hand-written retro silently omitted licensing,
    work-page candidacy and per-person vendor assessment, and nobody noticed
    until the template was applied afterwards.
    """
    missing = list(HUMAN_ENTRY_FIELDS)
    if bundle.budget and bundle.effort.total_hours:
        # We can at least frame profitability, so it stops being a pure blank.
        missing = [f for f in missing if not f.startswith("Actual profitability")]
    return missing
