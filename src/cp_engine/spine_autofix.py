"""The morning spine fix: clear the floating elements the record already answers.

A floating element is important, unbound and serves nothing (`spine_lint`;
the health line's "Spine unbound · … floating"). Most need a person: what
an element serves, or whether it belongs in the standing brief, is a
judgement. Two cases need no judgement, because the spine already holds the
answer:

  1. **Inherit from what it replaced.** The element has an active
     `supersedes` edge to an older element. The new one takes over the old
     one's links: the work the old one served (`serves`), and its place in
     the standing brief (`canon_of`). A replacement that never picked these
     up is a write that was missed, not a decision nobody made.
  2. **Bind to the deliverable it informs.** The element has an ACTIVE
     `informs` edge to a live deliverable. Someone confirmed that edge; the
     element serves that deliverable. (A `proposed` edge is not enough: it
     is a guess awaiting a person.)

Anything else is left alone and stays on the health line. Nothing is
guessed from titles or bodies.

`plan()` is pure: rows in, fixes out. `run()` reads MC-2, plans and, with
`apply=True`, writes each fix: the live row's `serves` (marked confirmed so
`cp sync` keeps it, as `set_spine_element` does), the `canon_of` edge, and
one review-gated auto-step per fixed element so the move is on its trail.
A failed write is reported and the element stays floating; it never raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cp_engine.mc2_db import Tables
from cp_engine.spine_lint import is_deliverable

log = logging.getLogger(__name__)

#: `confirmed_by` on the rows this writes: who made the edit, for the trail.
FIXER = "cp-engine morning spine fix"


@dataclass
class Fix:
    project_id: str
    est_item_id: str
    serves: list[str] = field(default_factory=list)      # new serves (empty: unchanged)
    canon_brief: str | None = None                        # brief to join, if any
    canon_from: str | None = None                         # predecessor leaving the brief
    reason: str = ""

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, "est_item_id": self.est_item_id,
                "serves": self.serves, "canon_brief": self.canon_brief,
                "reason": self.reason}


def is_floating(row: dict) -> bool:
    """The `spine_lint` / health-line predicate on one live, unbound row."""
    return (bool(row.get("important")) and not (row.get("serves") or [])
            and not is_deliverable(row.get("layer")))


def plan(floating: list[dict], relations: list[dict], substance: list[dict]) -> list[Fix]:
    """The fixes the spine already answers, one per element at most.

    `floating`: live floating rows (`project_id, est_item_id`).
    `relations`: ACTIVE edges in those projects (`project_id, kind,
    from_item_id, to_item_id`), kinds `supersedes`, `informs`, `canon_of`.
    `substance`: every row (any status) of the elements those edges touch
    (`project_id, est_item_id, status, archived, serves, layer`).
    """
    by_key: dict[tuple, list[dict]] = {}
    for r in substance:
        by_key.setdefault((r.get("project_id"), r.get("est_item_id")), []).append(r)

    def live(pid, eid) -> dict | None:
        return next((r for r in by_key.get((pid, eid), [])
                     if r.get("status") == "live" and not r.get("archived")), None)

    def dead(pid, eid) -> bool:
        rows = by_key.get((pid, eid), [])
        return bool(rows) and live(pid, eid) is None

    def served_by(pid, eid) -> list[str] | None:
        """What an element served: its live row's serves, else the serves its
        versions agree on. None when the versions disagree (ambiguous)."""
        row = live(pid, eid)
        if row is not None:
            return list(row.get("serves") or [])
        seen = {tuple(r.get("serves") or []) for r in by_key.get((pid, eid), [])
                if r.get("serves")}
        if len(seen) > 1:
            return None
        return list(next(iter(seen), ()))

    edges: dict[tuple, list[dict]] = {}
    for e in relations:
        edges.setdefault((e.get("project_id"), e.get("from_item_id")), []).append(e)

    out: list[Fix] = []
    for row in floating:
        pid, eid = row.get("project_id"), row.get("est_item_id")
        mine = edges.get((pid, eid), [])
        serves: list[str] = []
        reasons: list[str] = []
        canon_brief = canon_from = None

        # Rule 1 — inherit from what it replaced.
        for sup in (e for e in mine if e.get("kind") == "supersedes"):
            old = sup.get("to_item_id")
            inherited = served_by(pid, old)
            if inherited is None:
                continue  # versions disagree: not ours to pick
            # Drop only targets KNOWN dead (spine rows, none live). `serves`
            # usually holds estimate work-item ids with no spine row at all;
            # inheriting the pointer the predecessor already held is no guess.
            alive = [t for t in inherited if t != eid and not dead(pid, t)]
            if alive:
                serves += [t for t in alive if t not in serves]
                reasons.append(f"serves what {old} served")
            old_canon = next((e for e in edges.get((pid, old), [])
                              if e.get("kind") == "canon_of"), None)
            if old_canon and canon_brief is None:
                canon_brief, canon_from = old_canon.get("to_item_id"), old
                reasons.append(f"takes {old}'s place in the brief")

        # Rule 2 — bind to the deliverable it informs (confirmed edges only).
        for inf in (e for e in mine if e.get("kind") == "informs"):
            target = live(pid, inf.get("to_item_id"))
            if target is not None and is_deliverable(target.get("layer")):
                t = inf.get("to_item_id")
                if t not in serves:
                    serves.append(t)
                    reasons.append(f"informs deliverable {t}")

        if serves or canon_brief:
            out.append(Fix(pid, eid, serves, canon_brief, canon_from, "; ".join(reasons)))
    return out


def _read(client, floating_keys: set[tuple]) -> tuple[list[dict], list[dict]]:
    pids = sorted({p for p, _ in floating_keys})
    relations = [e for e in (client.table(Tables.SPINE_RELATIONS)
                             .select("project_id, kind, from_item_id, to_item_id, status")
                             .in_("project_id", pids).eq("status", "active")
                             .in_("kind", ["supersedes", "informs", "canon_of"])
                             .limit(5000).execute().data or [])
                 if e.get("status") == "active"]
    touched = {e.get("from_item_id") for e in relations} | {e.get("to_item_id") for e in relations}
    substance: list[dict] = []
    if touched:
        substance = _rows(client, pids, touched)
        # The predecessors' serve targets too: whether each is still live.
        targets = {t for r in substance for t in (r.get("serves") or [])} - touched
        if targets:
            substance += _rows(client, pids, targets)
    return relations, substance


def _rows(client, pids: list, ids: set) -> list[dict]:
    return (client.table(Tables.SPINE_SUBSTANCE)
            .select("project_id, est_item_id, status, archived, serves, layer")
            .in_("project_id", pids).in_("est_item_id", sorted(ids))
            .limit(5000).execute().data or [])


def _apply(client, fix: Fix, *, today: str) -> str | None:
    """Write one fix. Returns an error string, or None on success."""
    from cp_engine import spine_steps

    try:
        if fix.serves:
            live = (client.table(Tables.SPINE_SUBSTANCE)
                    .select("id, field_states")
                    .eq("project_id", fix.project_id).eq("est_item_id", fix.est_item_id)
                    .eq("status", "live").eq("archived", False)
                    .limit(2).execute().data or [])
            if len(live) != 1:
                return f"{len(live)} live rows; expected 1"
            fs = dict(live[0].get("field_states") or {})
            fs["serves"] = "confirmed"  # so `cp sync` keeps it (#180)
            (client.table(Tables.SPINE_SUBSTANCE)
             .update({"serves": fix.serves, "binding": "live", "field_states": fs,
                      "confirmed_by": FIXER,
                      "confirmed_at": datetime.now(UTC).isoformat()})
             .eq("id", live[0]["id"]).execute())
        if fix.canon_brief:
            code = (client.table(Tables.SPINE_SUBSTANCE).select("project_code")
                    .eq("project_id", fix.project_id).eq("est_item_id", fix.est_item_id)
                    .limit(1).execute().data or [{}])[0].get("project_code")
            try:
                (client.table(Tables.SPINE_RELATIONS)
                 .insert({"project_id": fix.project_id, "project_code": code,
                          "kind": "canon_of", "from_item_id": fix.est_item_id,
                          "to_item_id": fix.canon_brief, "status": "active",
                          "source": "manual", "note": f"inherited from {fix.canon_from}",
                          "created_by": FIXER})
                 .execute())
            except Exception as exc:  # noqa: BLE001
                if "23505" not in str(exc) and "duplicate key" not in str(exc).lower():
                    raise
            # The replaced element leaves the brief: one member per slot, the
            # same move promote_to_canon(replaces_key=) makes.
            (client.table(Tables.SPINE_RELATIONS).delete()
             .eq("project_id", fix.project_id).eq("kind", "canon_of")
             .eq("from_item_id", fix.canon_from).eq("to_item_id", fix.canon_brief)
             .execute())
    except Exception as exc:  # noqa: BLE001 — reported; the element stays floating
        return f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        spine_steps.upsert_auto_step(client, fix.project_id, fix.est_item_id,
                                     f"Bound by the morning spine fix: {fix.reason}"[:120],
                                     step_date=today)
    except Exception as exc:  # noqa: BLE001 — journaling is non-fatal
        log.info("spine autofix: step for %s not journaled: %s", fix.est_item_id, exc)
    return None


def run(client, *, floating: list[dict], apply: bool, today: str) -> dict[str, Any]:
    """Plan (and, with `apply`, write) the fixes for these floating rows.

    Returns {"fixed": [...], "failed": [...]} — with `apply=False`, "fixed"
    lists what WOULD be fixed. Never raises; a read failure lands in "error".
    """
    keys = {(r.get("project_id"), r.get("est_item_id")) for r in floating}
    if not keys:
        return {"fixed": [], "failed": []}
    try:
        relations, substance = _read(client, keys)
    except Exception as exc:  # noqa: BLE001
        return {"fixed": [], "failed": [], "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    fixes = plan(floating, relations, substance)
    if not apply:
        return {"fixed": [f.to_dict() for f in fixes], "failed": [], "dry_run": True}
    fixed, failed = [], []
    for f in fixes:
        err = _apply(client, f, today=today)
        (failed if err else fixed).append({**f.to_dict(), **({"error": err} if err else {})})
    return {"fixed": fixed, "failed": failed}
