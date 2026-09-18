"""Estimate reader — the spine's backbone (spine estimate-binding, Phase 0).

The "estimate" is the live project plan that already lives in MC-2's Postgres
under the **non-public `estimator` schema** (it drives the client portal). One
default estimate per MC project:

    estimator.projects  (the rendered estimate — see `estimate_scope`)
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
from datetime import date, timedelta

from cp_engine import mc2_db
from cp_engine.estimate_scope import rendered_estimates
from cp_engine.mc2_db import Tables

# Default estimate name when the estimator row carries no `name` (mirrors the
# portal's "Estimate 1" default).
_DEFAULT_ESTIMATE_NAME = "Estimate 1"


def _required(row, key: str, what: str):
    """Read a required column from an estimator row, raising a clear domain
    error (not a bare KeyError) naming the missing key and its context."""
    try:
        return row[key]
    except KeyError:
        raise ValueError(
            f"estimator {what} row missing required key '{key}'"
        ) from None


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
    # Which of the job's admitted estimates this phase belongs to (#291). A
    # job can carry several sold estimates that render together; same-named
    # phases from different estimates stay separate, as mc-2's spine does.
    estimate_id: str = ""


@dataclass(frozen=True)
class Estimate:
    """The job's sold work — EVERY admitted estimate, rendered together (#291).

    `id` is the oldest admitted estimate's id (the root), kept for callers
    that key on one; `estimate_ids` carries all of them, oldest first, and is
    what schedule reads must use. `name` joins the names when there are
    several, so a surface that prints it says "Estimate 1 + CTV addition"
    rather than silently naming the root.
    """
    id: str
    mc_project_id: str
    name: str
    phases: tuple[EstimatePhase, ...] = ()
    # The project's kickoff date (public.projects.start_date), origin for
    # schedule-bar calendar math. Nullable — Drew sets it manually at kickoff.
    start_date: str | None = None
    estimate_ids: tuple[str, ...] = ()

    @classmethod
    def from_rows(cls, project_rows, phases, activities, deliverables, start_date=None) -> "Estimate":
        # One row (the pre-#291 signature, still used by tests and by any
        # caller with exactly one estimate) or the admitted list, oldest first.
        if isinstance(project_rows, dict):
            project_rows = [project_rows]
        project_rows = list(project_rows)
        if not project_rows:
            raise ValueError("Estimate.from_rows: no estimate rows")
        estimate_ids = tuple(_required(r, "id", "project") for r in project_rows)
        by_phase: dict[str, list[EstimateItem]] = {
            _required(p, "id", "phase"): [] for p in phases
        }
        for kind, rows in (("activity", activities), ("deliverable", deliverables)):
            for r in rows:
                phase_id = _required(r, "phase_id", "item")
                # Orphan guard: an item pointing at a phase we weren't given is
                # dropped explicitly (not bucketed into a phantom key that the
                # phase loop below would silently discard).
                if phase_id not in by_phase:
                    continue
                by_phase[phase_id].append(
                    EstimateItem(
                        id=_required(r, "id", "item"), phase_id=phase_id, kind=kind,
                        name=_required(r, "name", "item"),
                        short_description=r.get("short_description"),
                        # `position` is nullable in the estimator schema, so
                        # `.get(..., 0)` is not enough — the key is present and
                        # the value is None. Coerce here so the dataclass never
                        # carries a None the phase sort below would choke on.
                        position=r.get("position") or 0,
                        library_item_id=r.get("library_item_id"),
                    )
                )
        # Phases are ordered by ESTIMATE first (oldest admitted first), then
        # by position within it — never merged by name across estimates. A
        # phase row without `project_id` (older callers, single-estimate
        # tests) is attributed to the root.
        rank = {eid: n for n, eid in enumerate(estimate_ids)}

        def _phase_key(p):
            return (rank.get(p.get("project_id") or estimate_ids[0], len(rank)),
                    p.get("position") or 0)

        ordered_phases = tuple(
            EstimatePhase(
                id=_required(p, "id", "phase"), name=_required(p, "name", "phase"),
                overview=p.get("overview"),
                position=p.get("position") or 0,
                items=tuple(sorted(by_phase.get(p["id"], []), key=lambda i: i.position)),
                estimate_id=p.get("project_id") or estimate_ids[0],
            )
            # Same nullable-`position` guard as the items above: a phase row
            # carries the column with a NULL value, so the default never fires
            # and an all-NULL set raises `None < None` mid-sort.
            for p in sorted(phases, key=_phase_key)
        )
        project_row = project_rows[0]  # the root; the read-shape control scans this name
        names = [project_row.get("name", _DEFAULT_ESTIMATE_NAME) or _DEFAULT_ESTIMATE_NAME] + [
            r.get("name", _DEFAULT_ESTIMATE_NAME) or _DEFAULT_ESTIMATE_NAME for r in project_rows[1:]
        ]
        return cls(
            id=_required(project_row, "id", "project"),
            mc_project_id=_required(project_row, "mc_project_id", "project"),
            name=" + ".join(names),
            phases=ordered_phases,
            start_date=start_date,
            estimate_ids=estimate_ids,
        )

    def item_by_id(self, item_id) -> EstimateItem | None:
        for p in self.phases:
            for i in p.items:
                if i.id == item_id:
                    return i
        return None

    def all_items(self) -> tuple[EstimateItem, ...]:
        return tuple(i for p in self.phases for i in p.items)


@dataclass(frozen=True)
class ScheduleItem:
    """A schedule bar (Gantt row) from estimator.schedule_items. `start_week`
    is weeks-from-kickoff (numeric); pair with the estimate's start_date for a
    calendar date. `work_item_id`/`work_item_kind`/`done` link a bar back to its
    estimate work item — those columns arrive in a later phase, so they default
    to None/False until then."""
    id: str
    label: str
    phase_id: str | None
    start_week: float
    duration: float
    item_type: str | None      # activity | milestone | feedback | holiday
    emphasis: str | None       # important | blocker | external | null
    work_item_id: str | None = None
    work_item_kind: str | None = None
    done: bool = False


# Explicit column lists (never `*`, per the global Supabase rule).
# (the estimate row itself is selected by `estimate_scope`, which owns the
#  which-estimate-counts rule and its own column list — #284)
_PHASE_COLUMNS = mc2_db.EST_PHASE_COLUMNS
_ITEM_COLUMNS = mc2_db.EST_ITEM_COLUMNS
# public.projects carries the kickoff start_date, keyed by the MC project id
# (public.projects.id === estimator.projects.mc_project_id).
_PUBLIC_PROJECT_COLUMNS = mc2_db.PROJECTS_ESTIMATE_COLUMNS
# work_item_id / work_item_kind / done shipped in migration 069 (the
# schedule↔work-item link), so they are selected here. They are still read via
# `.get()` in the row mapper, defaulting None/None/False, so an older schema
# without them degrades gracefully rather than 400-ing.
_SCHEDULE_COLUMNS = mc2_db.EST_SCHEDULE_COLUMNS


def fetch_estimate(client, mc_project_id) -> Estimate | None:
    """Read the job's admitted estimates as ONE `Estimate`, or `None` if none.

    Pure read against the `estimator` schema (drives the client portal). Four
    queries, all explicit-column:
      1. estimator.projects — every estimate `estimate_scope` admits for
         `mc_project_id` (approved; else the on_schedule bridge), oldest first.
      2. estimator.phases — their phases (by project_id IN the admitted ids).
      3/4. estimator.phase_activities / phase_deliverables — scoped to those
         phase ids via `.in_("phase_id", [...])`. These child tables carry no
         project_id, only phase_id, so filtering by the estimate's phase ids is
         the precise scope (and avoids over-fetching across estimates).

    Returns `None` when there is no default estimate row yet — the
    "no-estimate-yet" fallback the binder treats as "nothing to bind to".
    """
    # Which estimate counts is Mission Control's rule, not ours (#284) —
    # `is_default` is dropped by mc-2 migration 183 and a filter on a missing
    # column is a 42703 ERROR, which `sync.py` would catch and log while every
    # spine quietly mirrored unbound.
    project_rows = rendered_estimates(client, mc_project_id)
    if not project_rows:
        return None
    estimate_ids = [r["id"] for r in project_rows]

    # Kickoff date for calendar math — lives on public.projects, keyed by the
    # MC project id (nullable; Drew sets it manually at kickoff).
    public_rows = (
        client.schema("public")
        .table(Tables.PROJECTS)
        .select(_PUBLIC_PROJECT_COLUMNS)
        .eq("id", mc_project_id)
        .execute()
        .data
        or []
    )
    start_date = public_rows[0].get("start_date") if public_rows else None

    phases = (
        client.schema("estimator")
        .table(Tables.EST_PHASES)
        .select(_PHASE_COLUMNS)
        .in_("project_id", estimate_ids)
        .execute()
        .data
        or []
    )
    phase_ids = [p["id"] for p in phases]

    if phase_ids:
        activities = (
            client.schema("estimator")
            .table(Tables.EST_PHASE_ACTIVITIES)
            .select(_ITEM_COLUMNS)
            .in_("phase_id", phase_ids)
            .execute()
            .data
            or []
        )
        deliverables = (
            client.schema("estimator")
            .table(Tables.EST_PHASE_DELIVERABLES)
            .select(_ITEM_COLUMNS)
            .in_("phase_id", phase_ids)
            .execute()
            .data
            or []
        )
    else:
        activities, deliverables = [], []

    return Estimate.from_rows(
        project_rows, phases, activities, deliverables, start_date=start_date
    )


def fetch_schedule(client, estimate_id) -> list[ScheduleItem]:
    """Read the schedule bars (Gantt rows) for an estimate — or for every
    admitted estimate of a job, when given `Estimate.estimate_ids` — ordered
    by (start_week, position).

    `estimator.schedule_items.project_id` references the ESTIMATE id
    (estimator.projects.id), NOT the mc_project_id — so we filter by
    `estimate_id` directly. Pass the job's `estimate_ids` (#291): a bar on an
    addition is as real as one on the root, and each already carries its own
    week offset from the job's start date. Explicit-column read (per the
    global Supabase rule); the not-yet-shipped work_item_id / work_item_kind /
    done columns are read via `.get()` defaulting None/False so this
    tolerates their absence.
    """
    query = (
        client.schema("estimator")
        .table(Tables.EST_SCHEDULE_ITEMS)
        .select(_SCHEDULE_COLUMNS)
    )
    if isinstance(estimate_id, (str, bytes)):
        query = query.eq("project_id", estimate_id)
    else:
        query = query.in_("project_id", list(estimate_id))
    rows = query.execute().data or []
    # Order by (start_week, position) on the raw rows — position is a DB ordering
    # hint we don't carry onto the dataclass.
    ordered = sorted(
        rows, key=lambda r: (float(r.get("start_week") or 0), r.get("position") or 0)
    )
    return [
        ScheduleItem(
            id=_required(r, "id", "schedule_item"),
            label=_required(r, "label", "schedule_item"),
            phase_id=r.get("phase_id"),
            start_week=float(r.get("start_week") or 0),
            duration=float(r.get("duration") or 0),
            item_type=r.get("item_type"),
            emphasis=r.get("emphasis"),
            work_item_id=r.get("work_item_id"),
            work_item_kind=r.get("work_item_kind"),
            done=bool(r.get("done", False)),
        )
        for r in ordered
    ]


def schedule_for_item(schedule, item_id) -> tuple[ScheduleItem, ...]:
    """The schedule bars that belong to a work item, ordered by `start_week`.

    Pure filter over a `ScheduleItem` list — no DB. Returns an empty tuple when
    the item has no bar, and may return >1 bar: a single work item can own
    multiple bars (1:N is allowed by design). Native events (work_item_id None)
    never match a real item_id; a `None` item_id matches nothing (use
    `native_schedule_events` for the unlinked bars)."""
    if item_id is None:
        return ()
    return tuple(
        sorted(
            (s for s in schedule if s.work_item_id == item_id),
            key=lambda s: s.start_week,
        )
    )


def native_schedule_events(schedule) -> tuple[ScheduleItem, ...]:
    """The schedule-native events — bars with no work item (milestones, holidays,
    process markers) — ordered by `start_week`.

    Pure filter over a `ScheduleItem` list; the complement of the linked bars."""
    return tuple(
        sorted(
            (s for s in schedule if s.work_item_id is None),
            key=lambda s: s.start_week,
        )
    )


def week_to_date(start_date, start_week) -> date | None:
    """Map a schedule bar's `start_week` (weeks-from-kickoff) to a calendar date,
    given the project's `start_date` (ISO string). Returns None if either is
    None — the start_date is nullable until Drew sets it at kickoff. `start_week`
    may arrive as a float (numeric DB column), so it's `int()`-ed."""
    if start_date is None or start_week is None:
        return None
    return date.fromisoformat(start_date) + timedelta(days=7 * int(start_week))
