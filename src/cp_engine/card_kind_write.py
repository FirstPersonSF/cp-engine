"""Persist `card_kind` on spine rows that can be classified structurally.

WHY A WRITER EXISTS AT ALL
--------------------------
``card_class.classify()`` has always read ``row["card_kind"]`` and fallen back
to layer inference "for rows written before the field existed". No such column
existed until mig 140, so ``classify_is_inferred()`` returned True for every
row in the tenant and the fallback the module calls *"a MIGRATION AID with a
known end date, not the model"* was the only path, permanently. The end date
could not arrive because the field it waited for was never added.

WHAT THIS DOES **NOT** DO
-------------------------
It does not persist a guess. ``card_class`` is explicit that it does not guess,
and writing a flagged inference into the column would launder it into unflagged
fact — destroying the ``classify_is_inferred()`` signal for good. So this writer
only stores kinds that follow from STRUCTURE:

  engagement   the row IS ``_authored/inputs-briefing`` — an explicit id match,
               verified one-per-project across 10 projects on 2026-08-15
  deliverable  a Deliverables/Output/Drafts layer row
  activity     ``placement='item'`` — it occupies an estimate slot, so it is
               the work
  attachment   ``placement='context'`` — it informs work without occupying a
               slot

Anything without a recorded ``placement`` and on a straddling layer is left
NULL for a human. ``is_ambiguous()`` still reports those.

MEASURED BEFORE WRITTEN
-----------------------
Run against all 271 live rows on 2026-08-15: 15 activity, 18 deliverable, 10
engagement, 228 attachment — **43 cards, 228 attachments**. The module docstring
independently measured *"39 cards, 230 attachments"* on 2026-08-11 by a
different route (layer, before placement was consulted). Two methods landing in
the same place is the corroboration; a rule that disagreed with that baseline
would have been the thing to question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cp_engine.card_class import CardKind, classify, is_ambiguous
from cp_engine.mc2_db import Tables, get_client

# Only these columns are needed to classify; never `*` (spine_substance carries
# multi-kilobyte bodies and jsonb, and this walks every row in the tenant).
_COLUMNS = "id, project_code, est_item_id, layer, placement, card_kind, framing"


@dataclass
class CardKindPlan:
    """What a run would write, before it writes anything."""

    to_set: list[tuple[str, str]] = field(default_factory=list)  # (row id, kind)
    already_set: int = 0
    left_null: list[tuple[str, str]] = field(default_factory=list)  # (id, why)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, kind in self.to_set:
            out[kind] = out.get(kind, 0) + 1
        return out


def build_plan(rows: list[dict]) -> CardKindPlan:
    """Decide a card_kind for each row, or decline to.

    Pure: rows in, plan out. No network, no clock — so the CLI can print
    exactly what would happen and a test can assert on it.
    """
    plan = CardKindPlan()
    for row in rows:
        if (row.get("card_kind") or "").strip():
            plan.already_set += 1
            continue

        # A straddling layer with no placement recorded is the one case the
        # module says cannot be resolved. Leave it.
        if is_ambiguous(row):
            plan.left_null.append(
                (row["id"], f"ambiguous layer {row.get('layer')!r}, no placement")
            )
            continue

        kind = classify(row)
        plan.to_set.append((row["id"], kind.value))
    return plan


def fetch_rows(client, *, project_code: str | None = None) -> list[dict]:
    """Live, non-archived rows — the population `classify()` is written for.

    Superseded versions are deliberately excluded: `set_spine_element` can only
    write live rows (the RLS policy is `using (status='live')`), so persisting a
    kind on history would be a write the rest of the system cannot maintain.
    """
    q = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(_COLUMNS)
        .eq("status", "live")
        .eq("archived", False)
    )
    if project_code:
        q = q.like("project_code", f"{project_code}%")
    return q.execute().data or []


def apply_plan(client, plan: CardKindPlan) -> int:
    """Write the plan. Returns the number of rows updated.

    One update per row rather than a bulk upsert: `spine_substance`'s PK is a
    composite text id and an upsert would need every NOT NULL column, risking a
    clobber of `body`/`status`/`origin` — which the mig-092 column guard would
    reject anyway. A targeted `.update()` on one column cannot.
    """
    written = 0
    for row_id, kind in plan.to_set:
        client.table(Tables.SPINE_SUBSTANCE).update({"card_kind": kind}).eq(
            "id", row_id
        ).execute()
        written += 1
    return written


def run(
    *, config=None, project_code: str | None = None, apply: bool = False
) -> tuple[CardKindPlan, int]:
    """Fetch, plan, and optionally write. Returns (plan, rows_written)."""
    client = get_client(config, required=True)
    rows = fetch_rows(client, project_code=project_code)
    plan = build_plan(rows)
    written = apply_plan(client, plan) if apply else 0
    return plan, written
