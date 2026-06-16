"""Project Spine estimate-binding (Phase 2) — mirror work-item *substance* and
project *context* markdown into MC-2 rows, plus reconcile each substance file's
*binding* against the live estimate.

Source of truth is the markdown on disk; this reconciles two tables to match it
for one project, mirroring the slice-2 element mirror in `spine_sync.py`:

* `spine_substance` — one row per substance *version* (the distilled, buildable
  memory hung off an estimate work item). Human-confirmable fields (the distilled
  ``framing``/``body``/``status``) are RECONCILED, not clobbered, via the shared
  `reconcile_field`/`_merge_flag` machinery so MC-2 stays the authoritative spine.
* `spine_context` — one row per project-level context element. Its distilled
  ``body`` is the human-confirmable field.

Binding reconcile (`reconcile_bindings`) sets each item's ``binding`` to
``live``/``orphaned``/``unbound`` against the live estimate, and the sync raises
a ``source="binding"`` review_flag for items whose estimate work item vanished —
never deleting them.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from cp_engine.spine_context import parse_context
from cp_engine.spine_sync import _has_confirmed_field, _merge_flag, reconcile_field
from cp_engine.substance import WorkItemSubstance, parse_substance

_SUBSTANCE_TABLE = "spine_substance"
_CONTEXT_TABLE = "spine_context"

# The distilled content a human verifies on a substance version. Same one-way
# door as elements: sync proposes, a confirmed value wins and divergence flags.
_SUBSTANCE_TRACKED_FIELDS = ("framing", "body", "status")

# A human can confirm a distilled context body.
_CONTEXT_TRACKED_FIELDS = ("body",)


# ---- Task 2.2: row mappers (pure, no client) --------------------------------


def substance_to_rows(
    item: WorkItemSubstance,
    *,
    project_id: str,
    project_code: str,
    rel_path: str,
) -> list[dict]:
    """Map one substance file to ONE row per version.

    id = ``<project_code>/<est_item_id>/<version_label>``. The estimate-binding
    columns (est_item_id/est_item_kind/phase/binding) are constant across the
    item's versions; version_label/version_date/status/framing/body/sources vary
    per version. `sources` is emitted as a plain JSON-serializable list.

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


def context_to_row(
    el,
    *,
    project_id: str,
    project_code: str,
    rel_path: str,
    slug: str,
) -> dict:
    """Map one context element to its row. id = ``<project_code>/_context/<slug>``."""
    return {
        "id": f"{project_code}/_context/{slug}",
        "project_id": project_id,
        "project_code": project_code,
        "type": el.type,
        "provenance": el.provenance,
        "nature": el.nature,
        "title": el.title,
        "body": el.body,
        "links": list(el.links),
        "rel_path": rel_path,
    }


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


# ---- Task 2.3 + 2.4: per-project reconcile upserts --------------------------


def _load_substance_items(project_dir: Path) -> list[tuple[WorkItemSubstance, str]]:
    """Parse every work-item substance file under ``spine/<phase>/*.md``.

    Skips ``_context/`` (project-level context, not substance) and any
    ``*.snapshots/`` dir (frozen snapshots have their own mirror). Returns
    ``(item, rel_path)`` pairs, rel_path relative to the project dir's parent
    chain — we keep it relative to project_dir for stable, portable storage.
    """
    out: list[tuple[WorkItemSubstance, str]] = []
    spine_root = project_dir / "spine"
    if not spine_root.is_dir():
        return out
    for md in sorted(spine_root.glob("*/*.md")):
        parts = md.relative_to(spine_root).parts
        # parts[0] is the phase dir; skip _context and snapshot dirs.
        if parts[0] == "_context" or any(p.endswith(".snapshots") for p in parts):
            continue
        item = parse_substance(md)
        out.append((item, str(md.relative_to(project_dir))))
    return out


def sync_spine_substance(
    client,
    *,
    project_id: str,
    project_code: str,
    project_dir: Path,
    estimate=None,
    now: datetime | None = None,
) -> int:
    """Reconcile `spine_substance` rows for one project to match disk.

    One row per substance version. The distilled ``framing``/``body``/``status``
    are RECONCILED (a confirmed MC-2 value wins, divergence flags) exactly like
    the element mirror; all other columns are written as-is. Bindings are
    reconciled against ``estimate`` first (see `reconcile_bindings`); items that
    came back ``orphaned`` get a ``source="binding"`` review_flag, healthy items
    prune any stale binding flag.

    Returns the number of version rows upserted. Reaps rows whose substance file
    vanished, scoped to this project_code; a row with any confirmed tracked field
    is flagged source-missing rather than deleted.

    Recovery: a mid-mirror DB error may leave partial state (some reaps/upserts
    done), but the next ``cp sync`` reconverges because every row is derived from
    disk (mirrors the element mirror's behavior; Phase 3 writes through here)."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()

    parsed = _load_substance_items(project_dir)
    items = [p[0] for p in parsed]
    rel_paths = [p[1] for p in parsed]
    items = reconcile_bindings(items, estimate)

    rows: list[dict] = []
    # Track, per row id, whether its item is orphaned so we can raise/prune the
    # binding flag after the per-field reconcile (which seeds review_flags).
    orphaned_by_id: dict[str, bool] = {}
    for item, rel_path in zip(items, rel_paths):
        item_rows = substance_to_rows(
            item, project_id=project_id, project_code=project_code,
            rel_path=rel_path,
        )
        for r in item_rows:
            orphaned_by_id[r["id"]] = item.binding == "orphaned"
        rows.extend(item_rows)
    present_ids = {r["id"] for r in rows}

    prior = (
        client.table(_SUBSTANCE_TABLE)
        .select(
            "id, framing, body, status, field_states, review_flags"
        )
        .eq("project_code", project_code)
        .execute()
        .data
    ) or []
    existing_by_id = {r["id"]: r for r in prior}

    for row in rows:
        existing = existing_by_id.get(row["id"])
        if existing is None:
            field_states: dict = {}
            review_flags: list = []
        else:
            field_states = dict(existing.get("field_states") or {})
            review_flags = list(existing.get("review_flags") or [])
            for field in _SUBSTANCE_TRACKED_FIELDS:
                value, state, flag = reconcile_field(
                    field,
                    existing.get(field),
                    field_states.get(field),
                    row.get(field),
                    now_iso,
                )
                row[field] = value
                field_states[field] = state
                review_flags = _merge_flag(review_flags, field, flag)
        # Binding flag: raise when orphaned, self-heal (prune) when healthy.
        if orphaned_by_id.get(row["id"]):
            review_flags = _merge_flag(
                review_flags, "binding",
                {"field": "binding", "was": "live", "now": "orphaned",
                 "at": now_iso, "source": "binding"},
                source="binding",
            )
        else:
            review_flags = _merge_flag(review_flags, "binding", None,
                                       source="binding")
        row["field_states"] = field_states
        row["review_flags"] = review_flags

    if rows:
        client.table(_SUBSTANCE_TABLE).upsert(rows, on_conflict="id").execute()

    # Reap orphans scoped to this project_code; flag-not-delete confirmed rows.
    for row_id, existing in existing_by_id.items():
        if row_id in present_ids:
            continue
        if _has_confirmed_field(existing, tracked_fields=_SUBSTANCE_TRACKED_FIELDS):
            review_flags = _merge_flag(
                list(existing.get("review_flags") or []),
                "source",
                {"field": "source", "was": "present", "now": "missing",
                 "at": now_iso},
            )
            client.table(_SUBSTANCE_TABLE).update(
                {"review_flags": review_flags}
            ).eq("id", row_id).execute()
        else:
            client.table(_SUBSTANCE_TABLE).delete().eq("id", row_id).execute()

    return len(rows)


def sync_spine_context(
    client,
    *,
    project_id: str,
    project_code: str,
    project_dir: Path,
    now: datetime | None = None,
) -> int:
    """Reconcile `spine_context` rows for one project to match disk.

    One row per ``spine/_context/*.md`` (slug = file stem). The distilled
    ``body`` is reconciled (confirmed MC-2 value wins, divergence flags); all
    other columns are written as-is. Reaps rows whose file vanished, scoped to
    this project_code; a row with a confirmed body is flagged source-missing
    rather than deleted. Returns the number of rows upserted."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()

    rows: list[dict] = []
    context_root = project_dir / "spine" / "_context"
    if context_root.is_dir():
        for md in sorted(context_root.glob("*.md")):
            el = parse_context(md)
            rows.append(
                context_to_row(
                    el, project_id=project_id, project_code=project_code,
                    rel_path=str(md.relative_to(project_dir)), slug=md.stem,
                )
            )
    present_ids = {r["id"] for r in rows}

    prior = (
        client.table(_CONTEXT_TABLE)
        .select("id, body, field_states, review_flags")
        .eq("project_code", project_code)
        .execute()
        .data
    ) or []
    existing_by_id = {r["id"]: r for r in prior}

    for row in rows:
        existing = existing_by_id.get(row["id"])
        if existing is None:
            row["field_states"] = {}
            row["review_flags"] = []
            continue
        field_states = dict(existing.get("field_states") or {})
        review_flags = list(existing.get("review_flags") or [])
        for field in _CONTEXT_TRACKED_FIELDS:
            value, state, flag = reconcile_field(
                field,
                existing.get(field),
                field_states.get(field),
                row.get(field),
                now_iso,
            )
            row[field] = value
            field_states[field] = state
            review_flags = _merge_flag(review_flags, field, flag)
        row["field_states"] = field_states
        row["review_flags"] = review_flags

    if rows:
        client.table(_CONTEXT_TABLE).upsert(rows, on_conflict="id").execute()

    for row_id, existing in existing_by_id.items():
        if row_id in present_ids:
            continue
        if _has_confirmed_field(existing, tracked_fields=_CONTEXT_TRACKED_FIELDS):
            review_flags = _merge_flag(
                list(existing.get("review_flags") or []),
                "source",
                {"field": "source", "was": "present", "now": "missing",
                 "at": now_iso},
            )
            client.table(_CONTEXT_TABLE).update(
                {"review_flags": review_flags}
            ).eq("id", row_id).execute()
        else:
            client.table(_CONTEXT_TABLE).delete().eq("id", row_id).execute()

    return len(rows)
