"""Spine steps — the ordered progress trail inside an element (mig 119).

A step is a lightweight, ordered marker of one move toward finishing its parent
element (drafted -> walked through -> ratified -> rewriting -> booked). It is NOT
a spine_substance version: no version history, sources, or body. Steps live in
their own table `public.spine_steps`, keyed by (project_id, est_item_id), ordered
by an explicit integer `position` (ties break by step_date).

These pure helpers back the four MCP verbs (add/set/reorder/remove_spine_step).
Each resolves the parent element's est_item_id via the same matcher the read
tools use (exact est_item_id OR a distinct framing substring), then reads/writes
`spine_steps` scoped to (project_id, est_item_id) so a stray id can never touch
another element's trail. The tool wrappers just resolve the project and delegate
here — which keeps them unit-testable with a fake client.

Boundary (cp/docs/plans/2026-07-21-spine-steps-build.md §0): a step NEVER writes
the work-item's schedule `done`. That bridge (all-steps-done -> suggest slot
complete) is a UI affordance reusing suggest_done, not anything these verbs do.
"""

from __future__ import annotations

from cp_engine.mc2_db import SPINE_RESOLVE_COLUMNS, Tables

STEP_STATUSES = ("done", "active", "upcoming")
NOTE_MAX = 8000  # parity with partner notes (long allowed, truncated for Slack)

# The read shape the verbs return (and the outline API mirrors).
_STEP_SELECT = "id, est_item_id, position, title, status, step_date, note"


def _resolve_est_item_id(client, project_id, key, company_id=None):
    """Resolve `key` to the parent element's est_item_id, or return None.

    Reuses resolve_element_versions (the write-side resolver) — we want only its
    est_item_id, not the substance versions, since steps live in their own table.
    """
    from cp_engine.project_sources import resolve_element_versions

    est_item_id, _versions = resolve_element_versions(
        client, project_id, key, columns=SPINE_RESOLVE_COLUMNS,
        company_id=company_id,
    )
    return est_item_id


def _read_steps(client, project_id, est_item_id):
    """This element's steps, ordered by position (the outline read order)."""
    return (
        client.table(Tables.SPINE_STEPS)
        .select(_STEP_SELECT)
        .eq("project_id", project_id)
        .eq("est_item_id", est_item_id)
        .order("position")
        .execute()
        .data
        or []
    )


def _densify(client, project_id, est_item_id):
    """Renumber positions to a contiguous 1..N after a delete."""
    for pos, s in enumerate(_read_steps(client, project_id, est_item_id), start=1):
        if s.get("position") != pos:
            client.table(Tables.SPINE_STEPS).update({"position": pos}).eq(
                "id", s["id"]
            ).execute()


def add_step(client, project_id, key, title, *, status="upcoming",
             step_date=None, note=None, company_id=None):
    """Append a step to the resolved element (position = max+1)."""
    if not (title and title.strip()):
        return {"error": "title is required to add a step"}
    if status not in STEP_STATUSES:
        return {"error": f"status must be one of {list(STEP_STATUSES)}"}
    if note is not None and len(note) > NOTE_MAX:
        return {"error": f"note exceeds {NOTE_MAX} characters"}

    est_item_id = _resolve_est_item_id(client, project_id, key, company_id)
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    existing = _read_steps(client, project_id, est_item_id)
    next_pos = max((s.get("position") or 0 for s in existing), default=0) + 1
    client.table(Tables.SPINE_STEPS).insert({
        "project_id": project_id,
        "est_item_id": est_item_id,
        "position": next_pos,
        "title": title.strip(),
        "status": status,
        "step_date": step_date,
        "note": note,
    }).execute()
    return {"est_item_id": est_item_id, "position": next_pos,
            "steps": _read_steps(client, project_id, est_item_id)}


def set_step(client, project_id, key, step_id, *, title=None, status=None,
             step_date=None, note=None, company_id=None):
    """Update one step's title/status/step_date/note. Only passed fields change
    (None = untouched — so you can't null a field through this verb, matching the
    partial-update discipline of set_spine_element)."""
    if status is not None and status not in STEP_STATUSES:
        return {"error": f"status must be one of {list(STEP_STATUSES)}"}
    if note is not None and len(note) > NOTE_MAX:
        return {"error": f"note exceeds {NOTE_MAX} characters"}

    est_item_id = _resolve_est_item_id(client, project_id, key, company_id)
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    patch = {}
    if title is not None:
        if not title.strip():
            return {"error": "title cannot be blank"}
        patch["title"] = title.strip()
    if status is not None:
        patch["status"] = status
    if step_date is not None:
        patch["step_date"] = step_date
    if note is not None:
        patch["note"] = note
    if not patch:
        return {"note": "nothing to update (pass title/status/step_date/note)"}

    client.table(Tables.SPINE_STEPS).update(patch).eq("id", step_id).eq(
        "project_id", project_id
    ).eq("est_item_id", est_item_id).execute()
    return {"est_item_id": est_item_id, "step_id": step_id,
            "steps": _read_steps(client, project_id, est_item_id)}


def reorder_steps(client, project_id, key, order, *, company_id=None):
    """Renumber positions to match `order` (a full list of step_ids)."""
    if not order:
        return {"error": "order (list of step_ids) is required"}
    est_item_id = _resolve_est_item_id(client, project_id, key, company_id)
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    for pos, sid in enumerate(order, start=1):
        client.table(Tables.SPINE_STEPS).update({"position": pos}).eq(
            "id", sid
        ).eq("project_id", project_id).eq("est_item_id", est_item_id).execute()
    return {"est_item_id": est_item_id,
            "steps": _read_steps(client, project_id, est_item_id)}


def remove_step(client, project_id, key, step_id, *, company_id=None):
    """Delete one step, then densify positions to stay 1..N contiguous."""
    est_item_id = _resolve_est_item_id(client, project_id, key, company_id)
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    client.table(Tables.SPINE_STEPS).delete().eq("id", step_id).eq(
        "project_id", project_id
    ).eq("est_item_id", est_item_id).execute()
    _densify(client, project_id, est_item_id)
    return {"est_item_id": est_item_id, "removed": step_id,
            "steps": _read_steps(client, project_id, est_item_id)}
