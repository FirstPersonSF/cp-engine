"""``cp commitments-sweep`` — surface stale/undated commitments for review.

Read-only (#135). ``list_commitments`` exists as an MCP verb but returns
flat JSON with no age, no staleness signal, and no grouping — a sweep of
~100 rows across projects was a manual read. This module groups open rows
by project, ages them, sorts oldest-first, and renders the fields that
drive a keep/close decision. Resolution stays a deliberate human/agent
act via ``resolve_commitment`` — the sweep makes the *decision* cheap,
not automatic.

Pairs with the wrap-up ritual (#134) and the dates-loop TTL (#136): rows
the TTL will expire are marked so the sweep shows what's about to close
on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from cp_engine.dates_loop import _EXPIRE_AFTER_DAYS, _ttl_bucket
from cp_engine.mc2_db import Tables

_SWEEP_COLUMNS = (
    "id, description, owner_email, owner_name, due_date, date_status, "
    "status, source_kind, project_id, initiative_id, created_at"
)

# --stale: undated AND at least this old — the "is this still real?" bucket.
STALE_DAYS = 14


@dataclass
class SweepRow:
    id: str
    description: str
    owner: str
    source_kind: str
    due_date: date | None
    date_status: str
    age_days: int
    ttl: str | None  # 'warn' | 'expire' | None (dates-loop TTL, #136)

    @property
    def undated(self) -> bool:
        return self.due_date is None

    @property
    def stale(self) -> bool:
        return self.undated and self.age_days >= STALE_DAYS


def _row(c: dict, today: date) -> SweepRow:
    created = None
    try:
        raw = (c.get("created_at") or "").replace("Z", "+00:00")
        created = datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    due = None
    if c.get("due_date"):
        due = date.fromisoformat(c["due_date"])
    return SweepRow(
        id=c["id"],
        description=c.get("description") or "(no description)",
        owner=c.get("owner_name") or c.get("owner_email") or "unowned",
        source_kind=c.get("source_kind") or "?",
        due_date=due,
        date_status=c.get("date_status") or "proposed",
        age_days=(today - created).days if created else 0,
        ttl=_ttl_bucket(c, today),
    )


def _owner_codes(client: Any) -> dict[str, str]:
    """id → code across projects and initiatives (one read each)."""
    codes: dict[str, str] = {}
    for table in (Tables.PROJECTS, Tables.INITIATIVES):
        for r in (
            client.table(table).select("id, code").execute().data or []
        ):
            if r.get("id") and r.get("code"):
                codes[r["id"]] = r["code"]
    return codes


def sweep(
    client: Any,
    *,
    code: str | None = None,
    today: date | None = None,
    undated_only: bool = False,
    older_than: int | None = None,
    stale_only: bool = False,
) -> dict[str, list[SweepRow]]:
    """Open commitments grouped by project code, oldest-first within each.

    ``code=None`` sweeps tenant-wide. Filters compose (AND).
    """
    today = today or date.today()

    query = (
        client.table(Tables.COMMITMENTS)
        .select(_SWEEP_COLUMNS)
        .eq("status", "open")
    )
    if code is not None:
        from cp_engine.commitments import resolve_commitment_owner

        owner = resolve_commitment_owner(client, code)
        if owner is None:
            raise ValueError(f"no project or initiative resolves for code {code!r}")
        col = "project_id" if owner["kind"] == "engagement" else "initiative_id"
        query = query.eq(col, owner["id"])
    rows = query.execute().data or []

    codes = _owner_codes(client)
    groups: dict[str, list[SweepRow]] = {}
    for c in rows:
        r = _row(c, today)
        if undated_only and not r.undated:
            continue
        if older_than is not None and r.age_days < older_than:
            continue
        if stale_only and not r.stale:
            continue
        owner_id = c.get("project_id") or c.get("initiative_id")
        group = code or codes.get(owner_id, "(unmapped)")
        groups.setdefault(group, []).append(r)

    for rs in groups.values():
        rs.sort(key=lambda r: -r.age_days)
    return dict(sorted(groups.items()))


def render_sweep(groups: dict[str, list[SweepRow]], *, today: date) -> str:
    """The review surface: one block per project, decision fields inline."""
    if not groups:
        return "No open commitments match."
    out: list[str] = []
    for code, rows in groups.items():
        out.append(f"{code} — {len(rows)} open")
        out.append("")
        for r in rows:
            if r.undated:
                head = f"  ⚠ UNDATED · {r.age_days}d"
            elif r.due_date < today:
                overdue = (today - r.due_date).days
                head = f"    SLIPPED · due {r.due_date.isoformat()} ({overdue}d ago)"
            else:
                head = f"    due {r.due_date.isoformat()} [{r.date_status}]"
            head += f"  [{r.source_kind}]  {r.owner}"
            if r.ttl == "expire":
                head += "  ← past TTL, expires next dates loop"
            elif r.ttl == "warn":
                head += f"  ← expires at {_EXPIRE_AFTER_DAYS}d unless dated"
            out.append(head)
            out.append(f"    {r.description}")
        out.append("")
    total = sum(len(rs) for rs in groups.values())
    stale = sum(1 for rs in groups.values() for r in rs if r.stale)
    out.append(
        f"{total} open across {len(groups)} project(s) · {stale} stale "
        f"(undated ≥{STALE_DAYS}d)"
    )
    return "\n".join(out)
