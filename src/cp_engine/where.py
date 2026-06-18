"""`cp where <code>` — the grounded "where are we / what's next" answer.

This is the payoff command of the estimate-as-spine build: it composes the
already-built spine pieces (estimate, schedule, derived status, substance) into
a single terminal summary where EVERY factual clause carries a `[source]`
marker so the human can trace each line back to the signal that produced it.

The composition (`build_where_report`) is a PURE function — already-fetched
data in, a formatted string out — so it unit-tests against fixtures with no DB.
The single thin DB read (`fetch_substance_status`) is the one piece this module
adds on top of the existing readers; everything else (estimate/schedule fetch,
status derivation, date math) is imported, never reimplemented.
"""

from __future__ import annotations

from datetime import date

from cp_engine.estimate import (
    native_schedule_events,
    schedule_for_item,
    week_to_date,
)
from cp_engine.execution_status import derive_status, item_has_recent_activity

# Status word → fixed-width label so the per-item lines align in a terminal.
_STATUS_COL = 8
_NAME_COL = 38


def fetch_substance_status(client, project_code: str) -> dict[str, dict]:
    """Read `spine_substance` rows for one project and fold them per est-item.

    Returns ``{est_item_id: {"has_live": bool, "version_dates": [iso, ...]}}``.

    ``has_live`` is True when ANY version row for that item has
    ``status == "live"``; ``version_dates`` collects every version's date (for
    the recent-activity recency signal). ``est_item_id`` is TEXT in the DB and is
    used as-is (string) so it lines up with `EstimateItem.id` compared as a
    string. Explicit-column read per the global Supabase rule.
    """
    rows = (
        client.table("spine_substance")
        .select("est_item_id, status, version_date, version_label, binding")
        .eq("project_code", project_code)
        .execute()
        .data
    ) or []
    out: dict[str, dict] = {}
    for r in rows:
        item_id = str(r.get("est_item_id"))
        slot = out.setdefault(item_id, {"has_live": False, "version_dates": []})
        if (r.get("status") or "") == "live":
            slot["has_live"] = True
        vd = r.get("version_date")
        if vd:
            slot["version_dates"].append(str(vd))
    return out


def _week_of_n(estimate, schedule, today: date) -> tuple[int, int, str] | None:
    """Current week (1-indexed) and the project's total week span.

    Total span = the latest bar end-week across the whole schedule. Current week
    = weeks elapsed since kickoff (0-indexed bar → +1 for human "week N").
    Returns None when there's no calendar anchor (start_date is None) or no bars.
    """
    if estimate.start_date is None or not schedule:
        return None
    total = max(
        (s.start_week + (s.duration or 0) for s in schedule), default=0.0
    )
    start = date.fromisoformat(estimate.start_date)
    elapsed_weeks = (today - start).days // 7
    current = elapsed_weeks + 1  # human-facing "week N"
    return current, int(round(total)), estimate.start_date


def _item_basis_and_source(item, bars, sub, *, start_date, today) -> tuple[str, str, str]:
    """Derive (status_word, basis, source_marker) for one work item.

    `bars` are the item's schedule bars; `sub` is its substance slot (or None).
    The source marker names which signal grounds the line — substance id/version
    when there's live substance, else the bar's week span, else "no bar".
    """
    version_dates = (sub or {}).get("version_dates", [])
    has_live = bool((sub or {}).get("has_live"))
    recent = item_has_recent_activity(version_dates, today=today)
    st = derive_status(
        bars,
        has_live_substance=has_live,
        recent_activity=recent,
        start_date=start_date,
        today=today,
    )

    # Source marker: prefer the strongest grounding signal.
    if has_live:
        latest = max(version_dates) if version_dates else "?"
        src = f"substance live, latest {latest}"
    elif bars:
        b = bars[0]
        s = week_to_date(start_date, b.start_week)
        e = week_to_date(start_date, b.start_week + (b.duration or 0))
        if s and e:
            src = f"bar {s.isoformat()} → {e.isoformat()}"
        else:
            src = f"bar W{int(b.start_week)}–W{int(b.start_week + (b.duration or 0))}"
    else:
        src = "no bar, no substance"
    return st.status, st.basis, src


def build_where_report(estimate, schedule, substance_by_item, today=None) -> str:
    """Compose the grounded where-are-we / what's-next answer (PURE).

    `estimate` is an `Estimate`; `schedule` a list of `ScheduleItem`;
    `substance_by_item` the `fetch_substance_status` map. Returns the formatted
    terminal string. Every factual line carries a `[source]` marker:

      - the header's week-of-N is `[estimate: phases + projects.start_date]`;
      - each work-item line ends with the signal that grounded its status;
      - native events are listed under their own heading with their type.
    """
    today = today or date.today()
    lines: list[str] = []

    # ---- Header: project, week-of-N, start date --------------------------
    won = _week_of_n(estimate, schedule, today)
    if won is not None:
        current, total, start = won
        lines.append(
            f"{estimate.name} · week {current} of {total} (started {start})"
            "   [estimate: phases + projects.start_date]"
        )
    else:
        lines.append(
            f"{estimate.name}   [estimate: phases; no kickoff date set]"
        )
    lines.append("")

    # ---- Per-phase work items --------------------------------------------
    for phase in estimate.phases:
        lines.append(phase.name)
        for item in phase.items:
            bars = list(schedule_for_item(schedule, item.id))
            sub = substance_by_item.get(str(item.id))
            status_word, basis, src = _item_basis_and_source(
                item, bars, sub, start_date=estimate.start_date, today=today
            )
            name = item.name
            if item.kind == "deliverable":
                name = f"{name} (deliverable)"
            lines.append(
                f"  {status_word:<{_STATUS_COL}} {name:<{_NAME_COL}} "
                f"{basis}   [{src}]"
            )
        lines.append("")

    # ---- Native schedule events (milestones, holidays, markers) ----------
    natives = native_schedule_events(schedule)
    if natives:
        lines.append("Native schedule events:")
        for ev in natives:
            when = week_to_date(estimate.start_date, ev.start_week)
            when_str = when.isoformat() if when else f"W{int(ev.start_week)}"
            kind = ev.item_type or "event"
            lines.append(f"  {ev.label} ({kind}, {when_str})   [schedule bar]")

    return "\n".join(lines).rstrip() + "\n"
