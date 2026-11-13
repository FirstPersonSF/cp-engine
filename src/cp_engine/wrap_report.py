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
class Financials:
    """Money in and money out, as Mission Control holds them.

    The totals come from MC-2's `get_job_financials` — the same function the
    Financials page reads — so the report and the page start from the same
    billed and cost figures. `by_payee` and `costs_through` come from
    `actual_costs`, to show what the total is made of and how fresh it is.

    The margin subtracts ALL three cost buckets (external, internal labor,
    software), as mc-2's Jobs card does. The function's own
    `actual_margin_pct` subtracts external costs only, so it reads higher
    whenever a cost is filed against an employee: on slt-5196, 51.7% against
    50.0% here, the gap being $2,678.88 of travel on staff cards.

    Two margins, because a job is usually wrapped before its last invoice:
    `margin_to_date_pct` is on what has been billed; `projected_margin_pct`
    adds the invoices still planned. Costs are what is RECORDED. A cost not
    yet entered, and partner time that is never costed, are not in either
    number, and the report must say so.

    The slt-5196 wrap of 2026-11-13 said the margin "cannot be told" while
    `invoices` and `actual_costs` held every figure needed. The bundle had
    not read them.
    """

    billed: float = 0.0
    collected: float = 0.0
    planned_forward: float = 0.0
    external_costs: float = 0.0
    internal_labor: float = 0.0
    software_costs: float = 0.0
    planned_costs: float = 0.0
    by_payee: list[tuple[str, float]] = field(default_factory=list)
    costs_through: _dt.date | None = None
    # False when the read failed. Distinct from "read it and found nothing".
    verified: bool = True

    @property
    def costs_total(self) -> float:
        return round(
            self.external_costs + self.internal_labor + self.software_costs, 2
        )

    @property
    def outstanding(self) -> float:
        return round(self.billed - self.collected, 2)

    @property
    def revenue_at_completion(self) -> float:
        """Billed so far plus the invoices still planned."""
        return round(self.billed + self.planned_forward, 2)

    @property
    def margin_to_date_pct(self) -> float | None:
        """Margin on what has been billed. None with nothing billed — a
        margin on zero revenue is not a number."""
        if not self.verified or self.billed <= 0:
            return None
        return round((self.billed - self.costs_total) / self.billed * 100, 1)

    @property
    def projected_margin_pct(self) -> float | None:
        """Margin once the planned invoices go out, on today's costs."""
        revenue = self.revenue_at_completion
        if not self.verified or revenue <= 0:
            return None
        return round((revenue - self.costs_total) / revenue * 100, 1)

    @property
    def projected_profit(self) -> float | None:
        if not self.verified or self.revenue_at_completion <= 0:
            return None
        return round(self.revenue_at_completion - self.costs_total, 2)


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize_financials(
    fin_row: dict | None,
    cost_rows: list[dict],
    names: dict[str, str] | None = None,
) -> Financials:
    """Fold one `get_job_financials` row and the project's `actual_costs`
    rows into a :class:`Financials`.

    `fin_row` is None when the function returned no row for the project: it
    was read, and the job has no invoices or costs. That is verified and
    empty, not unread.
    """
    names = names or {}
    row = fin_row or {}
    by: Counter[str] = Counter()
    latest: _dt.date | None = None
    for r in cost_rows:
        amount = _money(r.get("amount"))
        if not amount:
            continue
        by[names.get(str(r.get("entity_id")), "unattributed")] += amount
        day = _as_date(r.get("cost_date"))
        if day and (latest is None or day > latest):
            latest = day
    return Financials(
        billed=_money(row.get("billed")),
        collected=_money(row.get("collected")),
        planned_forward=_money(row.get("planned_forward")),
        external_costs=_money(row.get("external_costs")),
        internal_labor=_money(row.get("internal_labor")),
        software_costs=_money(row.get("software_costs")),
        planned_costs=_money(row.get("planned_costs")),
        by_payee=[(n, round(a, 2)) for n, a in by.most_common()],
        costs_through=latest,
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
    # Unread by default: a bundle built without the financial read must not
    # look like a job with no money in it.
    financials: Financials = field(
        default_factory=lambda: Financials(verified=False)
    )
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


# MC-2's one definition of a job's billed, collected and costs (mc-2
# `routers/financials.py` reads the same function).
_JOB_FINANCIALS_RPC = "get_job_financials"


def financials_payload(f: Financials) -> dict:
    """The `financials` block of the bundle."""
    return {
        "verified": f.verified,
        "note": (
            "From Mission Control's invoices and RECORDED costs. Costs not "
            "yet entered and partner time are not in the margin; say so, "
            "and name `costs.through`."
        ),
        "billed": f.billed,
        "collected": f.collected,
        "outstanding": f.outstanding,
        "planned_forward": f.planned_forward,
        "revenue_at_completion": f.revenue_at_completion,
        "costs": {
            "total": f.costs_total,
            "external": f.external_costs,
            "internal_labor": f.internal_labor,
            "software": f.software_costs,
            "planned": f.planned_costs,
            "through": f.costs_through.isoformat() if f.costs_through else None,
            "by_payee": [{"name": n, "amount": a} for n, a in f.by_payee],
        },
        "margin_to_date_pct": f.margin_to_date_pct,
        "projected_margin_pct": f.projected_margin_pct,
        "projected_profit": f.projected_profit,
    }


def unanswerable_fields(bundle: WrapBundle) -> list[str]:
    """Which HUMAN_ENTRY_FIELDS this bundle genuinely cannot fill.

    Rendered as an explicit "not assessed" block. Naming the gap is the
    feature — ibx-5192's hand-written retro silently omitted licensing,
    work-page candidacy and per-person vendor assessment, and nobody noticed
    until the template was applied afterwards.
    """
    missing = list(HUMAN_ENTRY_FIELDS)
    if bundle.financials.projected_margin_pct is not None:
        # Mission Control holds the invoices and the costs, so the margin is
        # a measured number, not a blank for a person to fill.
        missing = [f for f in missing if not f.startswith("Actual profitability")]
    if bundle.budget and bundle.effort.total_hours:
        # We can at least frame profitability, so it stops being a pure blank.
        missing = [f for f in missing if not f.startswith("Actual profitability")]
    return missing


def build_wrap(client, code: str, workdir, *, tail_days: int = 14,
               today=None, warn=None) -> tuple[WrapBundle, dict]:
    """The wrap bundle as a dict — the facts `/cp-wrap` and the Close run
    (learning phase 4) write the close-out report from. Facts only; reads
    only. `warn` receives degradation notes (a failed read is said)."""
    from cp_engine import close_out as _close
    from cp_engine import mc2_db
    from cp_engine.clock import tenant_today
    from cp_engine.mc2_db import Tables

    warn = warn or (lambda _m: None)

    # ── Project facts ────────────────────────────────────────────────
    row: dict = {}
    try:
        rows = (
            client.table(Tables.PROJECTS)
            .select(
                "id, code, name, number, mc_status, start_date, budget, "
                "target_profit_pct, account_manager"
            )
            .eq("code", workdir.name)
            .execute()
            .data
        ) or []
        if not rows:
            # `projects.code` is the SHORT code, not the dir slug (the known
            # code-vs-slug bridge). Fall back to the numeric suffix.
            digits = "".join(ch for ch in code if ch.isdigit())
            if digits:
                rows = (
                    client.table(Tables.PROJECTS)
                    .select(
                        "id, code, name, number, mc_status, start_date, "
                        "budget, target_profit_pct, account_manager"
                    )
                    .eq("number", int(digits))
                    .execute()
                    .data
                ) or []
        row = rows[0] if rows else {}
    except Exception as exc:  # noqa: BLE001 — degrade loudly, keep going
        warn(f"(WARNING: project row read failed: {exc})")

    project_id = row.get("id")

    # ── Meetings ─────────────────────────────────────────────────────
    meetings = MeetingLoad(tail_days=tail_days)
    if project_id:
        try:
            mrows = mc2_db.project_meetings(
                client, project_id, "id, meeting_date, title, duration_minutes")
            meetings = summarize_meetings(mrows, tail_days=tail_days)
        except Exception as exc:  # noqa: BLE001
            warn(f"(WARNING: meeting read failed: {exc})")

    # ── Effort (allocated hours, NOT timesheet actuals) ───────────────
    effort = EffortSummary(verified=False)
    names: dict[str, str] = {}
    if project_id:
        try:
            arows = (
                client.table(Tables.SPRINT_ALLOCATIONS)
                .select("entity_id, hours, week_start")
                .eq("project_id", project_id)
                .execute()
                .data
            ) or []
            names = {
                str(e["id"]): e.get("name") or "unattributed"
                for e in (
                    client.table(Tables.ENTITIES)
                    .select("id, name")
                    .execute()
                    .data
                    or []
                )
            }
            effort = summarize_effort(arows, names)
        except Exception as exc:  # noqa: BLE001
            warn(f"(WARNING: allocation read failed: {exc})")

    # ── Money: invoices and recorded costs, as the Financials page has them ──
    financials = Financials(verified=False)
    if project_id:
        try:
            frows = (
                client.rpc(_JOB_FINANCIALS_RPC, {"p_project_id": project_id})
                .execute()
                .data
            ) or []
            crows = (
                client.table(Tables.ACTUAL_COSTS)
                .select("entity_id, amount, cost_date")
                .eq("project_id", project_id)
                .execute()
                .data
            ) or []
            if not names:
                names = {
                    str(e["id"]): e.get("name") or "unattributed"
                    for e in (
                        client.table(Tables.ENTITIES)
                        .select("id, name")
                        .execute()
                        .data
                        or []
                    )
                }
            own = [r for r in frows if str(r.get("group_key")) == str(project_id)]
            financials = summarize_financials(
                (own or frows or [None])[0], crows, names
            )
        except Exception as exc:  # noqa: BLE001
            warn(f"(WARNING: financials read failed: {exc})")

    # ── Spine: deliverables + what each round absorbed ────────────────
    elements: list = []
    spine_ok = True
    try:
        elements = [
            _close.element_from_row(r)
            for r in _close.fetch_live_spine_rows(client, workdir.name)
        ]
    except Exception as exc:  # noqa: BLE001
        warn(f"(WARNING: spine read failed: {exc})")
        spine_ok = False

    deliverables = [e.title for e in elements if (e.layer or "") == "Deliverables"]

    commitments = None
    try:
        commitments = _close.fetch_open_commitments(client, code)
    except Exception as exc:  # noqa: BLE001
        warn(f"(WARNING: commitments read failed: {exc})")

    feedback = sorted(
        p.name
        for p in (workdir / "feedback-on-deck").glob("*.md")
    ) if (workdir / "feedback-on-deck").is_dir() else []

    b = WrapBundle(
        code=code,
        name=str(row.get("name") or ""),
        status=str(row.get("mc_status") or ""),
        start_date=_as_date(row.get("start_date")),
        budget=float(row["budget"]) if row.get("budget") else None,
        target_profit_pct=(
            float(row["target_profit_pct"]) if row.get("target_profit_pct") else None
        ),
        account_manager=str(row.get("account_manager") or ""),
        meetings=meetings,
        effort=effort,
        financials=financials,
        deliverables=deliverables,
        open_commitments=commitments,
        feedback_artifacts=feedback,
        spine_verified=spine_ok,
    )

    payload = {
        "code": b.code,
        "name": b.name,
        "status": b.status,
        "account_manager": b.account_manager,
        "start_date": b.start_date.isoformat() if b.start_date else None,
        "duration_days": b.duration_days,
        "duration_weeks": b.duration_weeks,
        "budget": b.budget,
        "target_profit_pct": b.target_profit_pct,
        "effort": {
            "verified": b.effort.verified,
            "note": (
                "ALLOCATED hours from sprint_allocations — MC-2's planning "
                "record, NOT timesheet actuals. Say so in the report; "
                "presenting an allocation as an actual overstates precision."
            ),
            "total_hours": b.effort.total_hours,
            "weeks": b.effort.weeks,
            "by_person": [{"name": n, "hours": h} for n, h in b.effort.by_person],
        },
        "budget_per_hour": b.budget_per_hour,
        "financials": financials_payload(b.financials),
        "meetings": {
            "count": b.meetings.count,
            "total_hours": b.meetings.total_hours,
            "first": b.meetings.first.isoformat() if b.meetings.first else None,
            "last": b.meetings.last.isoformat() if b.meetings.last else None,
            "tail_days": b.meetings.tail_days,
            "tail_share": round(b.meetings.tail_share, 3),
            "tail_hours": round(b.meetings.tail_minutes / 60.0, 1),
            "head_hours": round(b.meetings.head_minutes / 60.0, 1),
            "heaviest_days": [
                {"date": d, "meetings": c, "minutes": m}
                for d, c, m in b.meetings.heaviest_days
            ],
        },
        "deliverables": b.deliverables,
        "feedback_artifacts": b.feedback_artifacts,
        "open_commitments": b.open_commitments,
        "spine_verified": b.spine_verified,
        "learning_axes": [{"key": k, "prompt": p} for k, p in LEARNING_AXES],
        "human_entry_fields": list(HUMAN_ENTRY_FIELDS),
        "not_assessable_from_data": unanswerable_fields(b),
        "generated": (today or tenant_today()).isoformat(),
        "project_id": project_id,
    }

    return b, payload
