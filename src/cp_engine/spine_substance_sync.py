"""Project spine ⇄ MC-2: MC-2 owns every element; ``spine/`` is rendered.

Architecture plan step 4c (Drew, 2026-10-01). This module used to mirror the
work-item *substance* files on disk INTO ``spine_substance`` (disk the source of
truth, a confirmed MC-2 field winning field-by-field). Measured on 2026-10-01,
that push was all cost: nine of the eighteen files it pushed differed from MC-2
only in ``framing``, MC-2 newer and human-confirmed in every case, and disk
held nothing MC-2 lacked. So the direction is now one-way, MC-2 → disk:

* ``sync_spine_substance`` heals stale ``project_code`` spellings (all origins,
  snapshots, relations), reconciles each distilled element's ``binding``
  against the live estimate IN MC-2, then renders the project's whole
  ``spine/`` and its account's ``_stakeholders/`` through `spine_mirror`
  (generated, guarded: a hand edit is quarantined, then overwritten).
* New distilled versions are written to MC-2 by their producer
  (`spine_inbox.promote_card`) before the file is rendered — never by sync.

`substance_to_rows` stays: it is the row shape the promote path writes.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from pathlib import Path

from cp_engine.authored_element import card_kind_for
from cp_engine.mc2_db import Tables
from cp_engine.spine_mirror import render_account_dir, render_project_spine
from cp_engine.spine_sync import _merge_flag
from cp_engine.substance import WorkItemSubstance

logger = logging.getLogger(__name__)

_SUBSTANCE_TABLE = Tables.SPINE_SUBSTANCE


# ---- Task 2.2: row mappers (pure, no client) --------------------------------


def substance_to_rows(
    item: WorkItemSubstance,
    *,
    project_id: str,
    project_code: str,
    rel_path: str,
) -> list[dict]:
    """Map one substance file to ONE row per version.

    id = ``<project_code>/<est_item_id>/<version_label>``. The item-level
    columns (est_item_id/est_item_kind/phase/binding/layer/placement/serves) are
    constant across the item's versions; version_label/version_date/status/
    framing/body/sources vary per version. `sources` and `serves` are each
    emitted as a plain JSON-serializable list.

    field_states/review_flags are NOT set here — the sync fn owns reconcile.
    """
    rows: list[dict] = []
    for v in item.versions:
        rows.append(
            {
                "id": f"{project_code}/{item.est_item_id}/{v.label}",
                "project_id": project_id,
                "project_code": project_code,
                "est_item_id": item.est_item_id,
                "est_item_kind": item.est_item_kind,
                "phase": item.phase,
                "binding": item.binding,
                "layer": item.layer,
                "placement": item.placement,
                # Stamped here for the same reason the authored write path
                # stamps it (#179): `card_kind` was backfilled once by
                # `cxp card-kinds` and nothing wrote it afterwards, so rows
                # created since carried NULL — and `card_class` reads NULL as
                # "not work". Measured 2026-09-14: 5 live
                # distilled rows had drifted this way, all placement='item',
                # i.e. real work slots invisible as work.
                #
                # `card_kind_for` returns None for a straddling layer, which
                # leaves the column NULL for a human rather than laundering a
                # guess into stored fact — the discipline card_class and
                # card_kind_write both hold to.
                "card_kind": card_kind_for(
                    est_item_id=item.est_item_id,
                    layer=item.layer,
                    placement=item.placement,
                ),
                "serves": list(item.serves),
                "archived": item.archived,
                "version_label": v.label,
                "version_date": v.date,
                "status": v.status,
                "framing": v.framing,
                "body": v.body,
                "sources": list(v.sources),
                "rel_path": rel_path,
            }
        )
    return rows


# ---- Task 2.4: binding reconcile (pure) -------------------------------------


def reconcile_bindings(
    items: list[WorkItemSubstance], estimate
) -> list[WorkItemSubstance]:
    """Set each item's ``binding`` against the live estimate.

    No estimate → everything is ``unbound`` (nothing to bind to; no flags). With
    an estimate, an item whose ``est_item_id`` resolves is ``live``; one that no
    longer resolves is ``orphaned``. The orphan review_flag is raised at the row
    level during sync (`source="binding"`), not here.
    """
    if estimate is None:
        return [dataclasses.replace(it, binding="unbound") for it in items]
    out: list[WorkItemSubstance] = []
    for it in items:
        found = estimate.item_by_id(it.est_item_id) is not None
        out.append(dataclasses.replace(it, binding="live" if found else "orphaned"))
    return out


def _rehome_substance_codes(client, *, project_id, project_code):
    """Re-home spine_substance rows whose project_code drifted from the current
    code: update project_code + rewrite the id prefix. A rename, not a delete —
    MC-2 owns every element and its bodies must survive a code change. Returns
    the number of rows re-homed.

    Step 4c widened this from origin='authored' to every origin: distilled rows
    used to be re-created under the current code by the disk push on each sync,
    and nothing does that any more."""
    rows = (client.table(_SUBSTANCE_TABLE)
        .select("id, project_code")
        .eq("project_id", project_id).execute().data) or []
    n = 0
    for r in rows:
        if r.get("project_code") == project_code:
            continue
        old_id = r["id"]
        rest = old_id.split("/", 1)[1] if "/" in old_id else old_id  # strip leading "<old_code>/"
        new_id = f"{project_code}/{rest}"
        if new_id == old_id:
            continue
        # collision guard: a row already at the target id — prefer the existing new-code row, drop the stale old one.
        exists = (client.table(_SUBSTANCE_TABLE).select("id").eq("id", new_id).execute().data)
        if exists:
            logger.warning("spine substance re-home collision; dropping stale %s (kept %s)", old_id, new_id)
            client.table(_SUBSTANCE_TABLE).delete().eq("id", old_id).execute()
            n += 1
            continue
        client.table(_SUBSTANCE_TABLE).update(
            {"id": new_id, "project_code": project_code}
        ).eq("id", old_id).execute()
        n += 1
    return n


_RELATIONS_TABLE = Tables.SPINE_RELATIONS


def _rehome_relation_codes(client, *, project_id, project_code):
    """Re-home spine_relations rows whose project_code drifted (2026-08-03).

    The missing sibling of the substance/snapshot healers: substance
    self-healed every sync while relations accumulated short-code edges
    (45 found across 3 projects, invisible to dir-slug-scoped readers —
    mc-2 migration 129 cleaned the backlog; this keeps it clean). Edges
    carry no code-bearing id, so it's a pure column update keyed on
    project_id. Returns the number of rows re-homed."""
    rows = (client.table(_RELATIONS_TABLE)
        .select("id, project_code")
        .eq("project_id", project_id).execute().data) or []
    n = 0
    for r in rows:
        if r.get("project_code") == project_code:
            continue
        client.table(_RELATIONS_TABLE).update(
            {"project_code": project_code}
        ).eq("id", r["id"]).execute()
        n += 1
    return n


_SNAPSHOT_TABLE = Tables.SPINE_SNAPSHOTS


def _rehome_snapshot_codes(client, *, project_id, project_code):
    """Re-home spine_snapshots rows whose project_code drifted: rewrite the code
    prefix in id + deliverable_id, and project_code. A rename, not a delete.
    Returns count. Keyed on project_id (added in mig 078)."""
    rows = (client.table(_SNAPSHOT_TABLE)
        .select("id, deliverable_id, project_code")
        .eq("project_id", project_id).execute().data) or []
    n = 0
    for r in rows:
        if r.get("project_code") == project_code:
            continue
        old_id = r["id"]
        new_id = f"{project_code}/{old_id.split('/',1)[1]}" if "/" in old_id else old_id
        old_dlv = r.get("deliverable_id") or ""
        new_dlv = f"{project_code}/{old_dlv.split('/',1)[1]}" if "/" in old_dlv else old_dlv
        if new_id == old_id:
            continue
        exists = client.table(_SNAPSHOT_TABLE).select("id").eq("id", new_id).execute().data
        if exists:
            logger.warning("spine snapshot re-home collision; dropping stale %s (kept %s)", old_id, new_id)
            client.table(_SNAPSHOT_TABLE).delete().eq("id", old_id).execute()
            n += 1
            continue
        client.table(_SNAPSHOT_TABLE).update(
            {"id": new_id, "deliverable_id": new_dlv, "project_code": project_code}
        ).eq("id", old_id).execute()
        n += 1
    return n



def reconcile_bindings_in_mc2(client, *, project_id: str, estimate,
                              now: datetime | None = None) -> int:
    """Set each distilled element's ``binding`` against the live estimate, in
    MC-2 (``live`` when its work item resolves, ``orphaned`` when it vanished),
    raising a ``source="binding"`` review_flag on orphans and pruning it on
    recovery. Never deletes. Returns the number of rows updated.

    ``estimate=None`` changes nothing: the caller cannot tell "no estimate"
    from "estimator unreachable", and flipping every binding to ``unbound`` on
    an outage (what the disk push did) is not a fact worth writing."""
    if estimate is None:
        return 0
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    rows = (
        client.table(_SUBSTANCE_TABLE)
        .select("id, est_item_id, binding, origin, review_flags")
        .eq("project_id", project_id)
        .execute()
        .data
    ) or []
    n = 0
    for r in rows:
        eid = str(r.get("est_item_id") or "")
        if r.get("origin") == "authored" or eid.startswith("_authored/"):
            continue
        orphaned = estimate.item_by_id(eid) is None
        want = "orphaned" if orphaned else "live"
        flags = list(r.get("review_flags") or [])
        new_flags = _merge_flag(
            flags, "binding",
            {"field": "binding", "was": "live", "now": "orphaned",
             "at": now_iso, "source": "binding"} if orphaned else None,
            source="binding",
        )
        # An already-raised orphan flag is kept as is (no fresh `at` per sync).
        if orphaned and any(f.get("source") == "binding" for f in flags):
            new_flags = flags
        if r.get("binding") == want and new_flags == flags:
            continue
        client.table(_SUBSTANCE_TABLE).update(
            {"binding": want, "review_flags": new_flags}
        ).eq("id", r["id"]).execute()
        n += 1
    return n


def sync_spine_substance(
    client,
    *,
    project_id: str,
    project_code: str,
    project_dir: Path,
    estimate=None,
    now: datetime | None = None,
    malformed_out: list[dict] | None = None,
    warnings_out: list[str] | None = None,
    writer: str = "cxp sync",
) -> int:
    """MC-2 → disk for one project. Returns the number of element files
    rendered under ``spine/``.

    1. Heal stale ``project_code`` spellings (substance of every origin,
       snapshots, relations) on the stable ``project_id`` key.
    2. Reconcile distilled bindings against ``estimate`` (in MC-2).
    3. Render ``spine/`` (`render_project_spine`) — a READ failure raises
       before any file is touched; a single element that cannot render is
       skipped, warned and its file kept.
    4. Render the account's ``_stakeholders/`` (best-effort).

    Nothing is read from disk into MC-2. ``malformed_out`` is kept for callers'
    signatures and stays empty: there is no disk parse left to fail.
    ``warnings_out`` collects each skip and each quarantined hand edit — the
    webhook's promote route calls this outside sync, where a log record alone
    reaches no one."""
    del malformed_out  # no disk parse any more (step 4c)
    _rehome_substance_codes(client, project_id=project_id, project_code=project_code)
    # Snapshots are CLI-written and code-prefixed too; re-home them on the same
    # project_id key so a code change doesn't strand them (mig 078).
    _rehome_snapshot_codes(client, project_id=project_id, project_code=project_code)
    # Relations too — the healer that was missing while 45 short-code edges
    # accumulated (mig 129 cleaned the backlog; this keeps it clean).
    _rehome_relation_codes(client, project_id=project_id, project_code=project_code)

    try:
        reconcile_bindings_in_mc2(client, project_id=project_id,
                                  estimate=estimate, now=now)
    except Exception as exc:  # noqa: BLE001 — bindings are advisory
        logger.warning("binding reconcile skipped for %s: %s", project_code, exc)
        if warnings_out is not None:
            warnings_out.append(f"binding reconcile skipped: {exc}")

    rendered = render_project_spine(
        client, project_id=project_id, project_code=project_code,
        project_dir=project_dir, writer=writer, warnings_out=warnings_out,
    )

    try:
        _mirror_account_elements(client, project_id=project_id,
                                 project_code=project_code,
                                 project_dir=project_dir,
                                 warnings_out=warnings_out, writer=writer)
    except Exception as exc:  # noqa: BLE001 — best-effort account mirror
        logger.warning(
            "account mirror skipped for %s: %s", project_code, exc, exc_info=True,
        )
        if warnings_out is not None:
            warnings_out.append(f"account mirror skipped: {exc}")
    return rendered


def _mirror_account_elements(client, *, project_id: str, project_code: str,
                             project_dir: Path,
                             warnings_out: list[str] | None = None,
                             writer: str = "cxp sync") -> int:
    """Render the project's company account-scoped elements into
    `<account-dir>/_stakeholders/` (the working dir's parent). Returns how many
    elements were written. A pre-promotion copy under this project's
    `spine/_authored/` is reaped by the project render (account rows are not
    rendered there)."""
    proj = (
        client.table(Tables.PROJECTS)
        .select("company_id")
        .eq("id", project_id)
        .limit(1)
        .execute()
        .data
    ) or []
    company_id = proj[0].get("company_id") if proj else None
    if company_id is None:
        return 0  # initiative-shaped or unlinked — no account scope
    return render_account_dir(
        client, company_id=company_id,
        stakeholders_dir=project_dir.parent / "_stakeholders",
        writer=writer, warnings_out=warnings_out,
    )
