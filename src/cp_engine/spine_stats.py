"""Project Spine slice 3 — cross-project analytics (Phase C). CURRENTLY A STUB.

UNAVAILABLE SINCE mc-2 MIGRATION 072. These reports were built on the
`spine_elements` table, which that migration dropped in favour of
`spine_substance`. This is NOT a table rename that can be repointed: all three
reports key on columns the substance model does not carry.

    type inventory      needs `type`          — absent from spine_substance
    stage distribution  needs `stage`         — absent from spine_substance
    due soon            needs `target_date`   — absent from spine_substance

`spine.substance_row_to_element` documents the same gap from the other side:
"Columns the legacy element model had but substance does not (type/stage/
fidelity/target_date/...) default to None/empty". The estimator tables hold
adjacent data (`phase_deliverables.due_week`/`due_day`) but in a different
vocabulary — week offsets needing a phase start to resolve, not calendar dates
— so reconstructing these reports is a design question, not a repair.

Until that design question is answered, each function returns empty and
`ELEMENTS_TABLE_RETIRED` tells the CLI to say so plainly. Querying the dropped
table raised PGRST205 and crashed `cxp spine-stats` outright; an empty report
that explains itself is the honest interim.
"""
from __future__ import annotations
from collections import Counter
from datetime import date, timedelta

from cp_engine.spine import _parse_date
from cp_engine.mc2_db import Tables


# The CLI reads this to print one explanatory line instead of three empty
# reports that look like "no data" when they mean "no longer computable".
ELEMENTS_TABLE_RETIRED = True


def _deliverable_rows(client, cols: str) -> list[dict]:
    """No-op since the backing table was dropped. See the module docstring.

    Kept (rather than deleted with its callers) so the shape of what these
    reports needed stays legible to whoever reconstructs them.
    """
    return []


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
