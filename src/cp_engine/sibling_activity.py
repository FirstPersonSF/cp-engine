"""Cross-engagement view for a quiet workstream (cp-engine #355 item 4).

A job can look stalled while the same client people are busy on a sibling
job under the same company: ibx-5153 went 19 days without a meeting in
August while Janet, Kimber, Mehul and Jaime met on the summit job seven
times. Measured 2026-10-22 across the client jobs: 48 meeting gaps of 10+
days, of which ~20 had a sibling meeting with an overlapping client
attendee inside the gap.

This module turns that into ONE line for a quiet project:

    quiet 19d — Janet Noe, Kimber Myers +2 active on ibx-5192-srs (7 meetings)

It reuses `fathom_meetings` (project_id, meeting_date, participants) — no new
table. "Same people" is decided on the participants list, not stakeholder
cards: the cards are hand-curated and sparse, the attendee list is what the
meetings actually had. Internal people are dropped — a participant with an
email and `is_external = false` (the calendar invite's own verdict), or one
whose first name is in the tenant's `[team] members` — so "Drew was busy on
another job" never reads as an explanation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from cp_engine.mc2_db import Tables

QUIET_DAYS = 10
WINDOW_DAYS = 180
MAX_NAMES = 3

# Explicit columns: `transcript` and `summary` are the heavy ones on this table.
_COLUMNS = "project_id, meeting_date, participants, ignored, duplicate_of"

_ANGLE = re.compile(r"<[^>]*>")


def _name_key(name: str | None) -> tuple[str, str] | None:
    """`(key, display)` for a participant name, or None when unusable.

    Fathom sometimes stores `Tyler Johnson <tjohnson@triptych.co>` or a bare
    email as the name; the angle part is stripped and a bare email is
    skipped (it never matches a transcript-sourced name anyway).
    """
    display = " ".join(_ANGLE.sub("", name or "").split())
    if not display or "@" in display:
        return None
    # Calendar exports sometimes say "Lubel, Olivia"; the transcript says
    # "Olivia Lubel". One shape, so they match and the line reads cleanly.
    if display.count(",") == 1:
        last, first = (part.strip() for part in display.split(","))
        if last and first:
            display = f"{first} {last}"
    return display.lower(), display


def _meeting_day(row: Mapping[str, Any]) -> date | None:
    raw = str(row.get("meeting_date") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _internal_keys(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        for p in row.get("participants") or ():
            if p.get("email") and p.get("is_external") is False:
                k = _name_key(p.get("name"))
                if k:
                    keys.add(k[0])
    return keys


def _is_internal(key: str, internal: set[str], team_first: set[str]) -> bool:
    return key in internal or key.split(" ", 1)[0] in team_first


def sibling_activity_line(
    project_id: str,
    sibling_codes: Mapping[str, str],
    meetings: list[Mapping[str, Any]],
    *,
    today: date,
    team: Iterable[str] = (),
    quiet_days: int = QUIET_DAYS,
) -> str | None:
    """The cross-engagement line for one project, or None.

    `sibling_codes` maps a sibling's MC-2 id to its code (same company, the
    project itself excluded). `meetings` carries rows for the project AND its
    siblings. Returns None when the project is not quiet, has no meeting to
    learn its people from, or no sibling met with any of them since.
    """
    rows = [
        r for r in meetings
        if not r.get("ignored") and not r.get("duplicate_of") and _meeting_day(r)
    ]
    own = [r for r in rows if r.get("project_id") == project_id]
    if not own:
        return None
    last = max(_meeting_day(r) for r in own)  # type: ignore[type-var]
    quiet = (today - last).days
    if quiet < quiet_days:
        return None

    internal = _internal_keys(rows)
    team_first = {t.strip().lower() for t in team if t and t.strip()}
    people: set[str] = set()
    for r in own:
        for p in r.get("participants") or ():
            k = _name_key(p.get("name"))
            if k and not _is_internal(k[0], internal, team_first):
                people.add(k[0])
    if not people:
        return None

    shared: dict[str, str] = {}  # key → display, first-seen order
    per_sibling: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: _meeting_day(r)):  # type: ignore[arg-type,return-value]
        code = sibling_codes.get(str(r.get("project_id")))
        if code is None or _meeting_day(r) <= last:  # type: ignore[operator]
            continue
        hit = False
        for p in r.get("participants") or ():
            k = _name_key(p.get("name"))
            if k and k[0] in people:
                shared.setdefault(k[0], k[1])
                hit = True
        if hit:
            per_sibling[code] = per_sibling.get(code, 0) + 1
    if not per_sibling:
        return None

    names = list(shared.values())
    who = ", ".join(names[:MAX_NAMES])
    if len(names) > MAX_NAMES:
        who += f" +{len(names) - MAX_NAMES}"
    where = ", ".join(
        f"{code} ({n} meeting{'s' if n != 1 else ''})"
        for code, n in sorted(per_sibling.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return f"quiet {quiet}d — {who} active on {where}"


def fetch_meetings(client, project_ids: Iterable[str], *, today: date) -> list[dict]:
    """One read of the window's meetings for every id given. Raises on failure
    — the caller reports it rather than rendering "no sibling activity"."""
    ids = sorted({str(i) for i in project_ids if i})
    if not ids:
        return []
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    resp = (
        client.table(Tables.FATHOM_MEETINGS)
        .select(_COLUMNS)
        .in_("project_id", ids)
        .gte("meeting_date", since)
        .execute()
    )
    return list(getattr(resp, "data", None) or [])
