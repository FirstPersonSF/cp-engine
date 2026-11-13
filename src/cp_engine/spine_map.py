"""What a person says about the Spine Map, and where it is kept.

Mission Control's Map (mc-2 `backend/src/lib/spine_map.py`) is a read-only
list: one line per deliverable on the scope of work, its work items folded
inside, and four columns of what was gathered, made, shared and said back.
Nobody files into it by hand. A person says where things go, in a session or
on a belief-check page, and Claude records it.

Four things can be recorded, in three stores mc-2 owns the schema of:

  work_item_deliverables   "this work item serves this deliverable"
                           (suggested by Claude, confirmed by a person)
  spine_map_marks          "the client saw this on <date>" / "show this under
                           another column" / "this deliverable is done"
  estimator.phases         "this phase was sold under this agreement"
      .agreement_id

and one column mc-2 writes itself: `fathom_meetings.work_item_id`, the work
item a meeting belongs to.

On 2026-11-12/13 every one of these rows on `ibx-5153` went in by direct SQL
from one session, because no verb wrote them: no attribution, no validation
that a work item and a deliverable were on the same job, and no way for a
hosted-only session to keep the Map right. This module is the validation and
the reads; the writes themselves sit in the hosted verbs, under the caller's
own identity.

PURE except `read_state`: the row builders take what they need and return a
row or raise `MapError` with a sentence a person can act on.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.mc2_db import Tables


class MapError(ValueError):
    """What the database would refuse, or what would be wrong, said plainly."""


# Column key -> the heading the Map shows. A person says the heading.
COLUMNS: dict[str, str] = {
    "bg": "Background",
    "work": "Work sessions",
    "shared": "Shared",
    "fb": "Feedback",
}
# Deliverable status key -> the word the Map shows.
STATUSES: dict[str, str] = {
    "done": "Done",
    "replaced": "Replaced",
    "not_approved": "Not approved",
}
PAIRING_STATUSES = ("suggested", "confirmed", "removed")

_COLUMN_WORDS = {
    **{k: k for k in COLUMNS},
    **{v.lower(): k for k, v in COLUMNS.items()},
    "work": "work",
    "work session": "work",
}
_STATUS_WORDS = {
    **{k: k for k in STATUSES},
    **{v.lower(): k for k, v in STATUSES.items()},
    "not-approved": "not_approved",
}

PAIRING_COLUMNS = "id, work_item_id, deliverable_id, status, confirmed_by, confirmed_at, note"
MARK_COLUMNS = "id, item_key, shared_on, shared_to, column_key, status_key, note, set_by"
# `estimator.phases.agreement_id` (mc-2 mig 221): the agreement a phase was
# sold under. Named here once, as a constant, because `spine_substance` has a
# column of the same name that mc-2 owns end to end, and
# `test_specced_columns_wired` tracks that one by its name.
PHASE_AGREEMENT = "agreement_id"


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _phase_of(estimate: Estimate) -> dict[str, EstimatePhase]:
    return {it.id: ph for ph in estimate.phases for it in ph.items}


def _describe(item: EstimateItem, phases: dict[str, EstimatePhase]) -> str:
    ph = phases.get(item.id)
    return f"{item.name!r} in phase {ph.name!r} (id {item.id})" if ph else f"{item.name!r} (id {item.id})"


def find_item(estimate: Estimate | None, ref: str, kind: str) -> EstimateItem:
    """The ONE work item (`kind='activity'`) or deliverable this `ref` names.

    Tried in order: its id; `"<phase> / <name>"`; its exact name; a distinct
    part of its name. Two phases of one job often carry the same deliverable
    name (ibx-5153 has two "Messaging System"s, one per agreement), so a name
    that matches more than one is refused with each candidate's phase and id
    rather than taking the first.
    """
    what = "work item" if kind == "activity" else "deliverable"
    want = (ref or "").strip()
    if not want:
        raise MapError(f"name the {what}")
    if estimate is None:
        raise MapError(f"this job has no estimate, so it has no {what}s to name")
    items = [it for ph in estimate.phases for it in ph.items if it.kind == kind]
    phases = _phase_of(estimate)

    by_id = [it for it in items if it.id == want]
    if by_id:
        return by_id[0]

    if " / " in want:
        phase_part, _, name_part = want.partition(" / ")
        scoped = [
            it for it in items
            if _norm(it.name) == _norm(name_part)
            and _norm(phases[it.id].name) == _norm(phase_part)
        ]
        if len(scoped) == 1:
            return scoped[0]

    exact = [it for it in items if _norm(it.name) == _norm(want)]
    if not exact:
        # An exact name of the OTHER kind beats a partial match of this one:
        # "Campaign System" is the deliverable, not the work item "Campaign
        # System Development", and a swapped pair should be told so.
        other_kind = "deliverable" if kind == "activity" else "activity"
        wrong = [it for ph in estimate.phases for it in ph.items
                 if it.kind == other_kind and _norm(it.name) == _norm(want)]
        if wrong:
            other = "deliverable" if kind == "activity" else "work item"
            raise MapError(f"{want!r} is a {other} on this job, not a {what}")
    hits = exact or [it for it in items if _norm(want) in _norm(it.name)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise MapError(f"no {what} on this job matches {want!r}")
    listed = "; ".join(_describe(it, phases) for it in hits)
    raise MapError(
        f"{len(hits)} {what}s match {want!r}: {listed}. "
        f"Say which by id, or as \"<phase> / <name>\"."
    )


def find_phase(estimate: Estimate | None, ref: str) -> EstimatePhase:
    """The ONE phase this `ref` names: its id, exact name, or a distinct part."""
    want = (ref or "").strip()
    if not want:
        raise MapError("name the phase")
    if estimate is None:
        raise MapError("this job has no estimate, so it has no phases to name")
    by_id = [ph for ph in estimate.phases if ph.id == want]
    if by_id:
        return by_id[0]
    exact = [ph for ph in estimate.phases if _norm(ph.name) == _norm(want)]
    hits = exact or [ph for ph in estimate.phases if _norm(want) in _norm(ph.name)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise MapError(f"no phase on this job matches {want!r}")
    listed = "; ".join(f"{ph.name!r} (id {ph.id})" for ph in hits)
    raise MapError(f"{len(hits)} phases match {want!r}: {listed}. Say which by id.")


def find_agreement(agreements: list[dict], ref: str) -> dict:
    """The ONE agreement this `ref` names: its id, number ("SOW-6") or title."""
    want = (ref or "").strip()
    if not want:
        raise MapError("name the agreement")
    by_id = [a for a in agreements if a.get("id") == want]
    if by_id:
        return by_id[0]
    hits = [a for a in agreements if _norm(a.get("number")) == _norm(want)]
    hits = hits or [a for a in agreements if _norm(want) in _norm(a.get("title"))]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        have = ", ".join(a.get("number") or a.get("title") or "?" for a in agreements) or "none"
        raise MapError(f"no agreement on this job matches {want!r} (it has: {have})")
    listed = "; ".join(f"{a.get('number')} (id {a.get('id')})" for a in hits)
    raise MapError(f"{len(hits)} agreements match {want!r}: {listed}. Say which by id.")


def pairing_row(
    *,
    project_id: str,
    work_item_id: str,
    deliverable_id: str,
    status: str,
    confirmed_by: str = "",
    now_iso: str = "",
    note: str = "",
) -> dict[str, Any]:
    """The `work_item_deliverables` row for one pairing.

    A confirmed pairing names who confirmed it and when: the table refuses a
    `confirmed` row with no `confirmed_at`, and a confirmation nobody is named
    on is a suggestion wearing its clothes.
    """
    if status not in ("suggested", "confirmed"):
        raise MapError("status is suggested or confirmed (or removed, to take the pairing away)")
    row: dict[str, Any] = {
        "project_id": project_id,
        "work_item_id": work_item_id,
        "deliverable_id": deliverable_id,
        "status": status,
        "note": note.strip() or None,
        "confirmed_by": None,
        "confirmed_at": None,
    }
    if status == "confirmed":
        if not confirmed_by.strip():
            raise MapError("a confirmed pairing names who confirmed it")
        if not now_iso:
            raise MapError("a confirmed pairing needs the time it was confirmed")
        row["confirmed_by"] = confirmed_by.strip()
        row["confirmed_at"] = now_iso
    return row


def _iso_date(value: str, what: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise MapError(
            f"{what} must be a date as YYYY-MM-DD (got {value!r}); "
            "a guessed date is worse than none"
        ) from exc


def merge_mark(
    existing: dict | None,
    *,
    shared_on: str | None = None,
    shared_to: str | None = None,
    column: str | None = None,
    status: str | None = None,
    note: str | None = None,
    set_by: str = "",
) -> dict[str, Any] | None:
    """The mark a `spine_map_marks` row should hold after this change.

    Every argument left `None` keeps what is there; `""` clears that one
    thing. Returns the fields to write, or `None` when nothing is left to
    say and the row should go: the table refuses a mark that says nothing.
    """
    cur = dict(existing or {})
    out: dict[str, Any] = {
        "shared_on": cur.get("shared_on"),
        "shared_to": cur.get("shared_to"),
        "column_key": cur.get("column_key"),
        "status_key": cur.get("status_key"),
        "note": cur.get("note"),
    }
    if shared_on is not None:
        out["shared_on"] = _iso_date(shared_on, "shared_on") if shared_on.strip() else None
    if shared_to is not None:
        out["shared_to"] = shared_to.strip() or None
    if column is not None:
        if column.strip():
            key = _COLUMN_WORDS.get(_norm(column))
            if key is None:
                raise MapError(f"column is one of {', '.join(COLUMNS.values())}")
            out["column_key"] = key
        else:
            out["column_key"] = None
    if status is not None:
        if status.strip():
            key = _STATUS_WORDS.get(_norm(status))
            if key is None:
                raise MapError(f"status is one of {', '.join(STATUSES.values())}")
            out["status_key"] = key
        else:
            out["status_key"] = None
    if note is not None:
        out["note"] = note.strip() or None
    if out["shared_on"] is None:
        # "Shared with" says nothing once nothing was shared.
        out["shared_to"] = None
    if out["shared_on"] is None and out["column_key"] is None and out["status_key"] is None:
        return None
    if set_by.strip():
        out["set_by"] = set_by.strip()
    return out


def root_agreement(agreements: list[dict], agreement_id: str | None) -> dict | None:
    """The SOW a change order hangs from, or the agreement itself."""
    by_id = {a.get("id"): a for a in agreements}
    cur = by_id.get(agreement_id)
    hops = 0
    # Bounded: a parent chain is one or two deep, and a cycle must not hang.
    while cur and cur.get("parent_agreement_id") in by_id and hops < 8:
        cur = by_id[cur["parent_agreement_id"]]
        hops += 1
    return cur


def read_agreements(client, project_id: str) -> list[dict]:
    """A job's agreements by identity: id, kind, number, title, signed date,
    the estimate one names, and its parent. Oldest first.

    Through the `map_agreements` function (mc-2 mig 224), never the table.
    `public.agreements` has RLS on and no policy, so a signed-in team member
    reads zero rows from it: v0.137.0 shipped a table read here, and on its
    first real run every phase came back with no agreement and "SOW-6"
    matched nothing. The pre-release check had used the service role, which
    RLS does not bind. The function is team-gated and returns no amounts.
    """
    rows = client.rpc("map_agreements", {"p_project_id": project_id}).execute().data or []
    return sorted(rows, key=lambda a: (a.get("signed_date") or "9999", a.get("number") or ""))


def read_state(client, project_id: str, estimate: Estimate | None) -> dict[str, Any]:
    """Everything recorded about one project's Map, read with names attached.

    Four reads: the job's agreements (`read_agreements`), which agreement each
    phase names, the work-item pairings, and the marks. A pairing or mark
    whose target is no longer on the estimate is returned under `stale`
    rather than dropped: a row that points nowhere is worth seeing.
    """
    agreements = read_agreements(client, project_id)
    pairings = (
        client.table(Tables.WORK_ITEM_DELIVERABLES)
        .select(PAIRING_COLUMNS)
        .eq("project_id", project_id)
        .execute()
        .data
        or []
    )
    marks = (
        client.table(Tables.SPINE_MAP_MARKS)
        .select(MARK_COLUMNS)
        .eq("project_id", project_id)
        .execute()
        .data
        or []
    )
    phase_agreement: dict[str, str | None] = {}
    phase_ids = [ph.id for ph in estimate.phases] if estimate else []
    if phase_ids:
        rows = (
            client.schema("estimator")
            .table(Tables.EST_PHASES)
            .select(f"id, {PHASE_AGREEMENT}")
            .in_("id", phase_ids)
            .execute()
            .data
            or []
        )
        phase_agreement = {r["id"]: r.get(PHASE_AGREEMENT) for r in rows}

    label = {a["id"]: (a.get("number") or a.get("title") or "Agreement") for a in agreements}
    estimate_agreement = {
        a["estimate_id"]: a["id"] for a in agreements if a.get("estimate_id")
    }
    items = {it.id: it for ph in (estimate.phases if estimate else ()) for it in ph.items}
    mark_of = {m["item_key"]: m for m in marks}

    serves: dict[str, list[dict]] = {}
    served_by: dict[str, list[dict]] = {}
    stale: list[dict] = []
    for p in pairings:
        act, deliv = items.get(p["work_item_id"]), items.get(p["deliverable_id"])
        if act is None or deliv is None:
            stale.append({"kind": "pairing", "work_item_id": p["work_item_id"],
                          "deliverable_id": p["deliverable_id"],
                          "why": "its work item or deliverable is no longer on the estimate"})
            continue
        serves.setdefault(act.id, []).append(
            {"deliverable": deliv.name, "deliverable_id": deliv.id, "status": p["status"]})
        served_by.setdefault(deliv.id, []).append(
            {"work_item": act.name, "work_item_id": act.id, "status": p["status"]})

    phases_out: list[dict] = []
    unpaired: list[dict] = []
    for ph in (estimate.phases if estimate else ()):
        named = phase_agreement.get(ph.id)
        follows = estimate_agreement.get(ph.estimate_id)
        root = root_agreement(agreements, named or follows)
        work_items, deliverables = [], []
        for it in ph.items:
            if it.kind == "activity":
                entry = {"id": it.id, "name": it.name, "serves": serves.get(it.id, [])}
                work_items.append(entry)
                if not entry["serves"]:
                    unpaired.append({"id": it.id, "name": it.name, "phase": ph.name})
            else:
                status = (mark_of.get(it.id) or {}).get("status_key")
                deliverables.append({
                    "id": it.id, "name": it.name,
                    "status": STATUSES.get(status) if status else None,
                    "work_items": served_by.get(it.id, []),
                })
        phases_out.append({
            "id": ph.id, "name": ph.name,
            "agreement": label.get(root["id"]) if root else None,
            "agreement_named_on_phase": bool(named),
            "work_items": work_items, "deliverables": deliverables,
        })

    marks_out: list[dict] = []
    for m in marks:
        if not (m.get("shared_on") or m.get("column_key")):
            if m["item_key"] not in items:
                stale.append({"kind": "mark", "item": m["item_key"],
                              "why": "a status on a deliverable that is no longer on the estimate"})
            continue
        marks_out.append({
            "item": m["item_key"],
            "shared_on": m.get("shared_on"),
            "shared_to": m.get("shared_to"),
            "column": COLUMNS.get(m.get("column_key") or ""),
            "note": m.get("note"),
            "set_by": m.get("set_by"),
        })
    marks_out.sort(key=lambda m: (m["shared_on"] or "9999", m["item"]))

    return {
        "agreements": [
            {"id": a["id"], "number": a.get("number"), "title": a.get("title"),
             "kind": a.get("kind"), "signed": a.get("signed_date"),
             "names_estimate": bool(a.get("estimate_id"))}
            for a in agreements
        ],
        "phases": phases_out,
        "marks": marks_out,
        "work_items_serving_nothing": unpaired,
        "stale": stale,
    }
