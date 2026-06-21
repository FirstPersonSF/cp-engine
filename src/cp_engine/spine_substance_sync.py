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
import logging
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from cp_engine.authored_mirror import write_authored_element
from cp_engine.spine_context import parse_context
from cp_engine.spine_sync import _has_confirmed_field, _merge_flag, reconcile_field
from cp_engine.substance import (
    WorkItemSubstance,
    is_skipped_spine_dir,
    parse_substance,
)

logger = logging.getLogger(__name__)

_SUBSTANCE_TABLE = "spine_substance"
_CONTEXT_TABLE = "spine_context"

# Columns `write_authored_element` reads off each authored DB row.
_AUTHORED_SELECT = (
    "id, est_item_id, est_item_kind, phase, binding, layer, placement, "
    "serves, version_label, version_date, status, framing, body, sources, origin"
)

# The fields a human verifies on a substance version. Same one-way door as
# elements: sync proposes, a confirmed value wins and divergence flags. The
# distilled content (framing/body/status) plus the UI-set spine placement
# fields (layer/serves) and the UI archive flag — a confirmed edit in the MC-2
# card-dashboard must survive sync rather than being clobbered from disk.
_SUBSTANCE_TRACKED_FIELDS = (
    "framing", "body", "status", "layer", "serves", "archived",
)


def _normalize_serves(value) -> list[str]:
    """Normalize a `serves` value to a sorted list of strings for comparison.

    `serves` arrives as a tuple/list on disk and a JSON array from the DB; a
    confirmed `["b","a"]` and a disk `("a","b")` name the same set and must
    reconcile as EQUAL (no false drift flag). Sorting + str-coercion makes the
    equality check order- and list-vs-tuple-insensitive."""
    return sorted(str(x) for x in (value or []))

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


def _is_substance_file(md: Path) -> bool:
    """Cheap probe: does this ``.md`` have ``est_item_id`` in its frontmatter?

    During the spine transition a project's ``spine/`` tree holds BOTH new
    substance files (lowercase phase dirs, ``est_item_id`` present) and OLD
    shipped element files (capitalized layer dirs, no ``est_item_id``). Only the
    former are substance. A file whose frontmatter can't even be loaded is not
    ours either → skip it. We do NOT call the full strict `parse_substance`
    here: a file that IS a substance file but is otherwise malformed must still
    surface its error from the real parse below.
    """
    try:
        meta = frontmatter.load(str(md)).metadata
    except Exception:
        return False
    return "est_item_id" in meta


def _load_substance_items(project_dir: Path) -> list[tuple[WorkItemSubstance, str]]:
    """Parse every work-item substance file under ``spine/<phase>/*.md``.

    Skips ``_context/`` (project-level context, not substance) and any
    ``*.snapshots/`` dir (frozen snapshots have their own mirror). Also skips
    OLD-style element files that coexist in the tree during the spine
    transition — identified by the absence of ``est_item_id`` in frontmatter —
    so the shipped `spine_elements` files don't crash the substance mirror.
    Returns ``(item, rel_path)`` pairs, rel_path kept relative to project_dir
    for stable, portable storage.
    """
    out: list[tuple[WorkItemSubstance, str]] = []
    spine_root = project_dir / "spine"
    if not spine_root.is_dir():
        return out
    for md in sorted(spine_root.glob("*/*.md")):
        parts = md.relative_to(spine_root).parts
        # Skip _context, _authored, and snapshot dirs (shared predicate so this
        # and `spine_inbox._iter_substance_files` can't drift). _authored/ files
        # are a generated DB→disk MIRROR of authored (MC-2-owned) rows — reading
        # them back would flow disk→DB and flip the DB's est_item_kind=None to
        # the file's sentinel kind on the next sync.
        if is_skipped_spine_dir(parts):
            continue
        # Skip non-substance files (old element files, unrelated .md). A genuine
        # substance file that fails the full parse still raises below.
        if not _is_substance_file(md):
            continue
        item = parse_substance(md)
        out.append((item, str(md.relative_to(project_dir))))
    return out


def _rehome_authored_codes(client, *, project_id, project_code):
    """Re-home origin='authored' rows whose project_code drifted from the current
    code: update project_code + rewrite the id prefix. A rename, not a delete —
    authored bodies are MC-2-owned and must survive a code change. Returns the
    number of rows re-homed."""
    rows = (client.table(_SUBSTANCE_TABLE)
        .select("id, project_code")
        .eq("project_id", project_id).eq("origin", "authored").execute().data) or []
    n = 0
    for r in rows:
        if r.get("project_code") == project_code:
            continue
        old_id = r["id"]
        rest = old_id.split("/", 1)[1] if "/" in old_id else old_id  # strip leading "<old_code>/"
        new_id = f"{project_code}/{rest}"
        if new_id == old_id:
            continue
        # collision guard: a row already at the target id (shouldn't happen for
        # authored) — prefer the existing new-code row, drop the stale old one.
        exists = (client.table(_SUBSTANCE_TABLE).select("id").eq("id", new_id).execute().data)
        if exists:
            logger.warning("spine authored re-home collision; dropping stale %s (kept %s)", old_id, new_id)
            client.table(_SUBSTANCE_TABLE).delete().eq("id", old_id).execute()
            n += 1
            continue
        client.table(_SUBSTANCE_TABLE).update(
            {"id": new_id, "project_code": project_code}
        ).eq("id", old_id).execute()
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

    # Re-home authored rows whose project_code drifted from the current code
    # FIRST — before the disk-load + reap. The reap deliberately never touches
    # authored rows (MC-2 owns them), so on a code change their stale-code id
    # would otherwise strand forever. Renaming them here means by the time the
    # reap and the authored reverse-mirror run, authored rows already carry the
    # current code.
    _rehome_authored_codes(client, project_id=project_id, project_code=project_code)

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
            "id, framing, body, status, layer, serves, archived, "
            "field_states, review_flags, origin"
        )
        # Scope the reap on the STABLE project_id (uuid), not the mutable
        # project_code: the row id embeds the code, so a canonical-id rename
        # leaves old-code rows invisible to a code-scoped query — they strand
        # and double. project_id is on every row and never changes.
        .eq("project_id", project_id)
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
                cur = existing.get(field)
                new = row.get(field)
                # `serves` is a list; compare order- and type-insensitively so a
                # reordered/retyped-but-equal set doesn't false-flag drift. The
                # comparison logic lives here so `reconcile_field` stays generic.
                if field == "serves":
                    cur = _normalize_serves(cur)
                    new = _normalize_serves(new)
                value, state, flag = reconcile_field(
                    field,
                    cur,
                    field_states.get(field),
                    new,
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
        if existing.get("origin") == "authored":
            continue  # MC-2 owns authored rows; disk is downstream — never reap.
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

    # Reverse-mirror (DB→disk): regenerate spine/_authored/<slug>.md for every
    # origin='authored' element. Authored rows are MC-2-owned and may have no
    # disk file; this materializes them so they surface in the tree (the caller's
    # `git add -A` picks the files up). Best-effort: a mirror failure must NEVER
    # abort sync — the rows persist in MC-2 regardless.
    try:
        authored = (
            client.table(_SUBSTANCE_TABLE)
            .select(_AUTHORED_SELECT)
            .eq("project_code", project_code)
            .eq("origin", "authored")
            .execute()
            .data
        ) or []
        groups: dict[str, list[dict]] = {}
        for r in authored:
            groups.setdefault(r["est_item_id"], []).append(r)
        for est_item_id, group in groups.items():
            write_authored_element(
                project_dir, project_code=project_code,
                est_item_id=est_item_id, rows=group,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort authored reverse-mirror
        logger.warning(
            "authored reverse-mirror skipped for %s: %s",
            project_code, exc, exc_info=True,
        )

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
        .eq("project_id", project_id)
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
