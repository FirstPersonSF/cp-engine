"""Project lifecycle status on MCP read results (#279).

Archiving a workstream in MC-2 sets `projects.mc_status = 'Archived'`. That is
a PROJECT state and it is independent of `spine_substance.archived` (an
ELEMENT state, set only by an explicit retire) — correctly so. But no MCP verb
read the project state, so `list_spine_elements("SNT-labscon-26")` on a
project archived in July answered exactly like one opened last week. A
finished, polished brief from dead work then competes with the live one on
presentation alone.

WHY ANNOTATE, NOT FILTER. Archived does not mean irrelevant: a closed
engagement's SOW, retro and stakeholder dossiers are the best evidence of how
we work, `cxp wrap` exists so a finished project has something to say
afterwards, and Archived is reversible (projects flip back — the reason
`archived/` became `inactive/` in v0.7.1). A verb that hid archived data would
break every one of those. So every project-scoped read carries the status and,
when the work is finished, says so in one line. Ranking and opt-in flags are
further steps the issue leaves open; this is the signal they would build on.

WHICH STATUSES SPEAK. The vocabulary is `Deal | Open | Holding | Closed |
Archived`, active = `Deal ∪ Open`. `Closed` and `Archived` are finished work
and carry a note. `Holding` is paused-but-current work — it carries its status
and no note, because "weigh this as history" would be wrong advice for it.

ONE READ PER CALL. The status is one primary-key read of `projects` (or one
`in_` read for a multi-project result). It is fail-soft by construction: this
is an annotation on a read that already succeeded, and it must never turn that
read into an error.

The hosted server imports this module (the stdio server that shared it was
retired in step 5b), so there is one wording of the signal. The list-result
branch of `annotate` served that stdio server; the hosted verbs return dicts.
"""

from __future__ import annotations

from typing import Any

# Explicit columns — never SELECT *.
PROJECT_STATUS_COLUMNS = "id, mc_status"

# Statuses whose results are finished work. Order-free; checked by membership.
FINISHED_STATUSES = frozenset({"Closed", "Archived"})


def fetch_statuses(client, project_ids) -> dict[str, str | None]:
    """`{project_id: mc_status}` for the given ids, in ONE read.

    Returns `{}` on any failure (an absent row, a policy denial, a fake client
    in a test) — see the module note on fail-soft.
    """
    ids = sorted({str(p) for p in (project_ids or ()) if p})
    if not ids:
        return {}
    try:
        # Inside the try: the vendored `mc2_db` shim carries `Tables` whole,
        # and an import problem must degrade like any other lookup failure.
        from cp_engine.mc2_db import Tables

        q = client.table(Tables.PROJECTS).select(PROJECT_STATUS_COLUMNS)
        q = q.eq("id", ids[0]) if len(ids) == 1 else q.in_("id", ids)
        rows = q.execute().data or []
    except Exception:  # noqa: BLE001 — an annotation must never fail the read
        return {}
    out: dict[str, str | None] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            out[str(row["id"])] = row.get("mc_status")
    return out


def status_note(status: str | None, code: str | None = None) -> str | None:
    """The one-line note for a finished status, or None."""
    if status not in FINISHED_STATUSES:
        return None
    who = f"{code} is" if code else "This project is"
    if status == "Archived":
        return (
            f"{who} ARCHIVED in MC-2 — finished work, still readable. Weigh it "
            "as history, not current direction (archived is reversible; the "
            "data is shown as-is)."
        )
    return (
        f"{who} CLOSED in MC-2 — finished work. Read it as the record of what "
        "was done, not as live direction."
    )


def status_fields(status: str | None, code: str | None = None) -> dict[str, Any]:
    """The keys a dict result gains: `project_status` always; `archived: true`
    and `project_note` when the work is finished.

    `project_note`, not `note`: many results already carry a `note` of their
    own (an element's importance note, the empty-team hint), and the signal
    must not overwrite one or be overwritten by it.
    """
    fields: dict[str, Any] = {"project_status": status}
    if status == "Archived":
        fields["archived"] = True
    note = status_note(status, code)
    if note:
        fields["project_note"] = note
    return fields


def hit_fields(status: str | None) -> dict[str, Any]:
    """`{project_status, archived?}` for one hit from finished work, else `{}`.

    For multi-project results (search hits): a live hit stays exactly as it
    was, so the marker is the exception a reader notices rather than a field
    on every row.
    """
    if status not in FINISHED_STATUSES:
        return {}
    return {"project_status": status, **({"archived": True} if status == "Archived" else {})}


def annotate(result, status: str | None, code: str | None = None):
    """Add the status signal to a verb's result, in place, and return it.

    - dict (not an error): gains `status_fields` (existing keys win).
    - list: a result shape with no room for a top-level key. A finished
      project gets ONE leading note row `{note, project_status, archived?}` —
      the list already uses note rows for "resolved to nothing" and the
      layer-miss hint. A live project's list is left byte-identical, so no
      caller iterating rows sees a new shape for current work.
    - anything else, or an error result: untouched.
    """
    if isinstance(result, dict):
        if "error" in result:
            return result
        for k, v in status_fields(status, code).items():
            result.setdefault(k, v)
        return result
    if isinstance(result, list):
        if any(isinstance(r, dict) and "error" in r for r in result):
            return result
        note = status_note(status, code)
        if note:
            row: dict[str, Any] = {"note": note, "project_status": status}
            if status == "Archived":
                row["archived"] = True
            result.insert(0, row)
    return result


def annotate_project(result, client, project_id: str | None, code: str | None = None):
    """Fetch one project's status and `annotate` the result with it.

    A failed lookup leaves the result untouched — no key at all, rather than
    `project_status: null`, so "unknown" is never mistaken for "no status".
    """
    if not project_id:
        return result
    statuses = fetch_statuses(client, [project_id])
    if str(project_id) not in statuses:
        return result
    return annotate(result, statuses[str(project_id)], code)
