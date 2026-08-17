"""What still needs a home — the routing queue's data layer.

The weekly sort asks how long something stays true. This asks what work it
belongs to. Separate acts, and the sort tracked only the first: measured
2026-08-16, ibx-5192's sort queue hit ZERO with 42 of its 74 classified rows
still attached to no work item. Tenant-wide, 84 rows across 11 projects.

WHAT COUNTS AS NEEDING A HOME
-----------------------------
`serves` is empty AND the row is none of:

  - a WORK CARD (activity / deliverable / engagement). A deliverable does not
    serve a work item; it IS one.
  - a STANDING ELEMENT (`_authored/inputs-briefing`, `_authored/sow`), unbound
    by contract — they frame the whole engagement.
  - BACKGROUND. It belongs to no single work item by definition, which is why
    the Sort tab ungates it from needing a home. 14 of ibx-5192's 19 unrouted
    background rows are the Infoblox use-case briefs and the corporate library;
    queueing them asks a human to file the library under one deliverable.

That cut ibx-5192's queue from 42 to 21. The same three rules live in the
frontend's `lib/spine/route.ts` — deliberately duplicated rather than shared,
because one runs in Python against the database and the other in TypeScript
against an already-fetched outline. The tests on both sides state the rules
identically so a change to one fails the other's expectations loudly.

NOTHING HERE WRITES `serves`. `run()` proposes; a human confirms in the UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cp_engine.mc2_db import Tables, get_client

log = logging.getLogger(__name__)

#: Unbound by contract — see the module header.
STANDING_IDS = frozenset({"_authored/inputs-briefing", "_authored/sow"})
#: Work IS the work; it takes no home.
WORK_KINDS = frozenset({"activity", "deliverable", "engagement"})
#: Belongs to no single work item, so it is never queued.
UNROUTABLE_LIFETIMES = frozenset({"background"})

_COLUMNS = (
    "id, project_id, project_code, est_item_id, est_item_kind, card_kind, "
    "lifetime, layer, framing, body, serves, version_date, placement"
)


@dataclass
class Homeless:
    row_id: str
    project_id: str | None
    project_code: str
    framing: str
    layer: str | None
    lifetime: str | None


def needs_home(row: dict) -> bool:
    """The one rule, applied to a raw substance row."""
    serves = row.get("serves") or []
    if serves:
        return False
    if (row.get("card_kind") or "") in WORK_KINDS:
        return False
    if (row.get("est_item_id") or "") in STANDING_IDS:
        return False
    if (row.get("lifetime") or "") in UNROUTABLE_LIFETIMES:
        return False
    return True


def fetch(client, *, project_code: str | None = None) -> list[dict]:
    """Live context rows with no home.

    Filtered in Python rather than SQL: `serves` is jsonb and the emptiness
    test differs between NULL and `[]`, while the lifetime and card_kind rules
    are the same predicate the frontend applies. One place to read the rule
    beats a WHERE clause that has to be kept in step with it.
    """
    q = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(_COLUMNS)
        .eq("status", "live")
        .eq("archived", False)
        .eq("placement", "context")
    )
    if project_code:
        q = q.like("project_code", f"{project_code}%")
    return [r for r in (q.execute().data or []) if needs_home(r)]


def slots_for(client, project_id: str) -> list[dict]:
    """This project's work items, as the pre-pass's answer space.

    Sourced from the ESTIMATE, not from substance rows: measured on ibx-5192,
    only 3 of its 9 slots have a `placement='item'` substance row, so deriving
    the list from the spine would hide two-thirds of the legitimate
    destinations and push the model toward `unsure`.
    """
    from cp_engine.estimate import fetch_estimate

    est = fetch_estimate(client, project_id)
    if est is None:
        return []
    return [
        {
            "id": item.id,
            "name": item.name,
            "kind": item.kind,
            "phase": phase.name,
        }
        for phase in est.phases
        for item in phase.items
    ]


def run(
    *,
    config=None,
    project_code: str | None = None,
    propose: bool = False,
    model: str | None = None,
) -> tuple[list[Homeless], int]:
    """Fetch the queue and optionally run the model pre-pass.

    Returns (queue, proposals_written). Proposals are persisted so the web
    surface can read them; `spine_substance.serves` is never touched here.

    Batched PER PROJECT, because the answer space is per project: a slot list
    from one project is meaningless for another, and mixing them would invite
    the model to route across a boundary that does not exist.
    """
    client = get_client(config, required=True)
    rows = fetch(client, project_code=project_code)
    queue = [
        Homeless(
            row_id=r["id"],
            project_id=r.get("project_id"),
            project_code=r.get("project_code") or "",
            framing=r.get("framing") or r.get("est_item_id") or r["id"],
            layer=r.get("layer"),
            lifetime=r.get("lifetime"),
        )
        for r in rows
    ]
    if not propose or not rows:
        return queue, 0

    # `active_prompt_version` is reused from the lifetime pass rather than
    # re-implemented: both kinds stamp the SAME cp_prompt version, and two
    # readers of one value is how they drift.
    from cp_engine.sort_propose import active_prompt_version
    from cp_engine.route_propose import persist, propose as run_pass

    written = 0
    by_project: dict[str, list[dict]] = {}
    for r in rows:
        pid = r.get("project_id")
        if pid:
            by_project.setdefault(pid, []).append(r)

    for pid, items in by_project.items():
        slots = slots_for(client, pid)
        if not slots:
            # Nowhere to route. Skipping beats asking the model a question with
            # no valid answer and persisting a run of `unsure`.
            log.debug("no estimate slots for project %s — skipped", pid)
            continue
        proposals = run_pass(items, slots, project_id=pid, model=model)
        written += persist(
            client,
            items,
            proposals,
            prompt_version=active_prompt_version(client),
            model=model,
        )
    return queue, written
