"""Workload lookahead: who is overloaded in the next few weeks, and what
could move (learning phase 6).

Drew, 2026-11-11: "anticipating workload … looking forward and saying,
'Hey, it looks like Marcello has a lot on his plate, or Drew has a lot on
his plate,' and trying to think about what we can do."

Sprint prep already flags capacity for the PLANNING week (owner-of-record
counts, or that week's allocations). This looks further: for each person,
each of the next `weeks` weeks — hours allocated (`sprint_allocations`) and
commitments due that week (open `commitments` they own). A person is
flagged when a week's hours pass `capacity`, or their due commitments pile
up. For each flag it names the person's biggest project that week and who
on the team has room that week — a suggestion, never a decision.

Reads only. Advisory: a failed read returns nothing, never fails prep.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from cp_engine.mc2_db import Tables

CAPACITY_HOURS = 40.0
#: Commitments due in one week that make a week heavy on their own.
DUE_PILE = 6
#: A teammate "has room" below this share of capacity that week.
ROOM_SHARE = 0.6


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def lookahead(client, *, today: date, weeks: int = 3,
              capacity: float = CAPACITY_HOURS) -> list[dict]:
    """`[{person, weeks: [{week, hours, due, projects}], flags: [str]}]` for
    people with a flagged week, heaviest first."""
    if client is None:
        return []
    start = _monday(today) + timedelta(days=7)  # the weeks AFTER this one
    week_starts = [start + timedelta(days=7 * i) for i in range(weeks)]
    end = week_starts[-1] + timedelta(days=6)
    try:
        allocs = (client.table(Tables.SPRINT_ALLOCATIONS)
                  .select("entity_id, project_id, hours, week_start")
                  .gte("week_start", week_starts[0].isoformat())
                  .lte("week_start", week_starts[-1].isoformat())
                  .execute().data or [])
        people = {e["id"]: e.get("name") for e in (client.table(Tables.ENTITIES)
                  .select("id, name").execute().data or [])}
        pids = sorted({a["project_id"] for a in allocs if a.get("project_id")})
        codes = {p["id"]: p.get("code") for p in (client.table(Tables.PROJECTS)
                 .select("id, code").in_("id", pids).execute().data or [])} if pids else {}
        dues = (client.table(Tables.COMMITMENTS).select("owner_name, due_date")
                .eq("status", "open").gte("due_date", week_starts[0].isoformat())
                .lte("due_date", end.isoformat()).execute().data or [])
    except Exception:  # noqa: BLE001 — advisory
        return []

    hours: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    by_project: dict[str, dict[date, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float)))
    for a in allocs:
        name = people.get(a.get("entity_id"))
        try:
            wk = _monday(date.fromisoformat(str(a.get("week_start"))[:10]))
            h = float(a.get("hours") or 0)
        except (TypeError, ValueError):
            continue
        if not name or h <= 0:
            continue
        hours[name][wk] += h
        by_project[name][wk][codes.get(a.get("project_id")) or "?"] += h
    due: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for c in dues:
        name = (c.get("owner_name") or "").strip()
        try:
            wk = _monday(date.fromisoformat(str(c.get("due_date"))[:10]))
        except (TypeError, ValueError):
            continue
        if name:
            due[name][wk] += 1

    out = []
    for person in sorted(set(hours) | set(due)):
        rows, flags = [], []
        for wk in week_starts:
            h = round(hours[person][wk], 1)
            d = due[person][wk]
            projects = sorted(by_project[person][wk].items(), key=lambda kv: -kv[1])
            rows.append({"week": wk.isoformat(), "hours": h, "due": d,
                         "projects": [{"code": c, "hours": round(x, 1)} for c, x in projects]})
            if h > capacity or d >= DUE_PILE:
                why = []
                if h > capacity:
                    why.append(f"{h:g}h allocated (capacity {capacity:g}h)")
                if d >= DUE_PILE:
                    why.append(f"{d} commitments due")
                room = [p for p in sorted(hours) if p != person
                        and hours[p][wk] < ROOM_SHARE * capacity]
                move = projects[0][0] if projects else None
                hint = ""
                if move and room:
                    hint = f"; could {', '.join(room[:2])} take some of {move}?"
                elif move:
                    hint = f"; biggest load is {move}"
                flags.append(f"week of {wk.isoformat()}: {' and '.join(why)}{hint}")
        if flags:
            out.append({"person": person, "weeks": rows, "flags": flags,
                        "peak_hours": max(r["hours"] for r in rows)})
    out.sort(key=lambda r: (-r["peak_hours"], r["person"]))
    return out


def render(look: list[dict]) -> list[str]:
    """Markdown lines for the planning doc; nothing when no one is flagged."""
    if not look:
        return []
    lines = ["**Workload lookahead** _(next weeks; a suggestion, not a decision)_:"]
    for p in look:
        for f in p["flags"]:
            lines.append(f"- **{p['person']}** — {f}")
    return lines
