"""Project Spine slice 3 — cross-project analytics (Phase C).

Read-only queries over the `spine_elements` spine: type inventory, deliverables
due soon, stage distribution. All scoped to the Deliverables layer (the unit of
project work). Pure-after-fetch — the only I/O is one select per function.
"""
from __future__ import annotations
from collections import Counter
from datetime import date, timedelta

from cp_engine.spine import _parse_date
from cp_engine.mc2_db import Tables


def _deliverable_rows(client, cols: str) -> list[dict]:
    return (
        client.table(Tables.SPINE_ELEMENTS)
        .select(cols).eq("layer", "Deliverables").execute().data
    ) or []


def type_inventory(client) -> list[tuple[str, int]]:
    """Count of Deliverables by `type`, across all projects, descending."""
    rows = _deliverable_rows(client, "type")
    counts = Counter(r["type"] for r in rows if r.get("type"))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def due_soon(client, *, today: date, within_days: int = 14) -> list[dict]:
    """Deliverables with target_date in [today, today+within_days], by date asc."""
    rows = _deliverable_rows(
        client, "project_code, title, type, stage, target_date"
    )
    horizon = today + timedelta(days=within_days)
    out = []
    for r in rows:
        if r.get("stage") == "final":
            continue  # shipped — not "due" (finals excluded here only)
        td = r.get("target_date")
        if not td:
            continue
        d = _parse_date(str(td))
        if d is None:
            continue
        if today <= d <= horizon:
            out.append(r)
    return sorted(out, key=lambda r: r["target_date"])


def stage_distribution(client) -> dict[str, int]:
    """Count of Deliverables per `stage` (rows with a stage)."""
    rows = _deliverable_rows(client, "stage")
    return dict(Counter(r["stage"] for r in rows if r.get("stage")))
