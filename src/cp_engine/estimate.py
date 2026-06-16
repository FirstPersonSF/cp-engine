"""Estimate reader — the spine's backbone (spine estimate-binding, Phase 0).

The "estimate" is the live project plan that already lives in MC-2's Postgres
under the **non-public `estimator` schema** (it drives the client portal). One
default estimate per MC project:

    estimator.projects  (is_default=true, one per mc_project_id)
      → estimator.phases               (ordered by position)
        → estimator.phase_activities   (work items, ordered by position)
        → estimator.phase_deliverables (work items, ordered by position)

Each activity/deliverable row is a "work item" that spine substance versions
will later hang off. This module is the pure-read foundation: build an in-memory
`Estimate` from rows, and fetch the live default estimate for a project. No
writes, no binding, no mirror — those are later phases.

Schema access: the `estimator` tables are NOT in `public`, so reads go through
`client.schema("estimator").table(...)`. `sync_mc2.py` already uses
`client.schema("public")...` against the same supabase-py client (v2.30), which
supports `.schema(...)`, so the pattern is proven in this codebase.

GLOBAL RULE: never `.select("*")` — always explicit columns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EstimateItem:
    id: str
    phase_id: str
    kind: str               # "activity" | "deliverable"
    name: str
    short_description: str | None
    position: int
    library_item_id: str | None


@dataclass(frozen=True)
class EstimatePhase:
    id: str
    name: str
    overview: str | None
    position: int
    items: tuple[EstimateItem, ...] = ()


@dataclass(frozen=True)
class Estimate:
    id: str
    mc_project_id: str
    name: str
    phases: tuple[EstimatePhase, ...] = ()

    @classmethod
    def from_rows(cls, project_row, phases, activities, deliverables) -> "Estimate":
        by_phase: dict[str, list[EstimateItem]] = {p["id"]: [] for p in phases}
        for kind, rows in (("activity", activities), ("deliverable", deliverables)):
            for r in rows:
                by_phase.setdefault(r["phase_id"], []).append(
                    EstimateItem(
                        id=r["id"], phase_id=r["phase_id"], kind=kind,
                        name=r["name"], short_description=r.get("short_description"),
                        position=r.get("position", 0), library_item_id=r.get("library_item_id"),
                    )
                )
        ordered_phases = tuple(
            EstimatePhase(
                id=p["id"], name=p["name"], overview=p.get("overview"),
                position=p.get("position", 0),
                items=tuple(sorted(by_phase.get(p["id"], []), key=lambda i: i.position)),
            )
            for p in sorted(phases, key=lambda p: p.get("position", 0))
        )
        return cls(id=project_row["id"], mc_project_id=project_row["mc_project_id"],
                   name=project_row.get("name", "Estimate 1"), phases=ordered_phases)

    def item_by_id(self, item_id):
        for p in self.phases:
            for i in p.items:
                if i.id == item_id:
                    return i
        return None

    def all_items(self):
        return tuple(i for p in self.phases for i in p.items)


# Explicit column lists (never `*`, per the global Supabase rule).
_PROJECT_COLUMNS = "id, mc_project_id, name, is_default"
_PHASE_COLUMNS = "id, name, overview, position"
_ITEM_COLUMNS = "id, phase_id, name, short_description, library_item_id, position"


def fetch_estimate(client, mc_project_id):
    """Read the live default estimate for an MC project, or `None` if none.

    Pure read against the `estimator` schema (drives the client portal). Four
    queries, all explicit-column:
      1. estimator.projects — the one default estimate for `mc_project_id`.
      2. estimator.phases — its phases (by project_id).
      3/4. estimator.phase_activities / phase_deliverables — scoped to those
         phase ids via `.in_("phase_id", [...])`. These child tables carry no
         project_id, only phase_id, so filtering by the estimate's phase ids is
         the precise scope (and avoids over-fetching across estimates).

    Returns `None` when there is no default estimate row yet — the
    "no-estimate-yet" fallback the binder treats as "nothing to bind to".
    """
    proj_rows = (
        client.schema("estimator")
        .table("projects")
        .select(_PROJECT_COLUMNS)
        .eq("mc_project_id", mc_project_id)
        .eq("is_default", True)
        .execute()
        .data
        or []
    )
    if not proj_rows:
        return None
    project_row = proj_rows[0]

    phases = (
        client.schema("estimator")
        .table("phases")
        .select(_PHASE_COLUMNS)
        .eq("project_id", project_row["id"])
        .execute()
        .data
        or []
    )
    phase_ids = [p["id"] for p in phases]

    if phase_ids:
        activities = (
            client.schema("estimator")
            .table("phase_activities")
            .select(_ITEM_COLUMNS)
            .in_("phase_id", phase_ids)
            .execute()
            .data
            or []
        )
        deliverables = (
            client.schema("estimator")
            .table("phase_deliverables")
            .select(_ITEM_COLUMNS)
            .in_("phase_id", phase_ids)
            .execute()
            .data
            or []
        )
    else:
        activities, deliverables = [], []

    return Estimate.from_rows(project_row, phases, activities, deliverables)
