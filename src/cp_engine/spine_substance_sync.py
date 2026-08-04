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
from cp_engine.mc2_db import Tables
from cp_engine.spine_sync import _has_confirmed_field, _merge_flag, reconcile_field
from cp_engine.substance import (
    WorkItemSubstance,
    is_skipped_spine_dir,
    parse_substance,
    version_number,
)

logger = logging.getLogger(__name__)

_SUBSTANCE_TABLE = Tables.SPINE_SUBSTANCE
_CONTEXT_TABLE = Tables.SPINE_CONTEXT

# Columns `write_authored_element` reads off each authored DB row.
_AUTHORED_SELECT = (
    "id, est_item_id, est_item_kind, phase, binding, layer, placement, "
    "serves, version_label, version_date, status, framing, body, sources, "
    "origin, scope, archived"
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


def _substance_est_item_id(md: Path) -> str | None:
    """Cheap probe: this ``.md``'s frontmatter ``est_item_id``, or None.

    During the spine transition a project's ``spine/`` tree holds BOTH new
    substance files (lowercase phase dirs, ``est_item_id`` present) and OLD
    shipped element files (capitalized layer dirs, no ``est_item_id``). Only the
    former are substance. A file whose frontmatter can't even be loaded is not
    ours either → None. We do NOT call the full strict `parse_substance`
    here: a file that IS a substance file but is otherwise malformed is
    skip-and-reported by the real parse below.
    """
    try:
        meta = frontmatter.load(str(md)).metadata
    except Exception:
        return None
    eid = meta.get("est_item_id")
    return str(eid) if eid else None


def _load_substance_items(
    project_dir: Path,
) -> tuple[list[tuple[WorkItemSubstance, str]], list[dict]]:
    """Parse every work-item substance file under ``spine/<phase>/*.md``.

    Skips ``_context/`` (project-level context, not substance) and any
    ``*.snapshots/`` dir (frozen snapshots have their own mirror). Also skips
    OLD-style element files that coexist in the tree during the spine
    transition — identified by the absence of ``est_item_id`` in frontmatter —
    so the shipped `spine_elements` files don't crash the substance mirror.

    A genuine substance file that fails the strict parse (e.g. a hand-edited
    version header with a bad status) is SKIPPED AND REPORTED, not raised —
    one malformed file must not abort the whole project's mirror (issue #90;
    the sap-5171 'synthesis' status stranded 12 days of sibling reconciles).

    Returns ``(items, malformed)``: items as ``(item, rel_path)`` pairs
    (rel_path relative to project_dir for stable, portable storage), malformed
    as ``{"rel_path", "est_item_id", "error"}`` dicts for the caller to report
    and for the reap shield.
    """
    out: list[tuple[WorkItemSubstance, str]] = []
    malformed: list[dict] = []
    spine_root = project_dir / "spine"
    if not spine_root.is_dir():
        return out, malformed
    for md in sorted(spine_root.glob("*/*.md")):
        parts = md.relative_to(spine_root).parts
        # Skip _context, _authored, and snapshot dirs (shared predicate so this
        # and `spine_inbox._iter_substance_files` can't drift). _authored/ files
        # are a generated DB→disk MIRROR of authored (MC-2-owned) rows — reading
        # them back would flow disk→DB and flip the DB's est_item_kind=None to
        # the file's sentinel kind on the next sync.
        if is_skipped_spine_dir(parts):
            continue
        # Skip non-substance files (old element files, unrelated .md).
        est_item_id = _substance_est_item_id(md)
        if est_item_id is None:
            continue
        rel_path = str(md.relative_to(project_dir))
        try:
            item = parse_substance(md)
        except ValueError as exc:
            logger.warning(
                "spine substance file %s is malformed — skipped from the "
                "mirror (its DB rows are shielded from the reap); fix the "
                "file so it reconciles: %s", rel_path, exc,
            )
            malformed.append({
                "rel_path": rel_path,
                "est_item_id": est_item_id,
                "error": str(exc),
            })
            continue
        out.append((item, rel_path))
    return out, malformed


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


def sync_spine_substance(
    client,
    *,
    project_id: str,
    project_code: str,
    project_dir: Path,
    estimate=None,
    now: datetime | None = None,
    malformed_out: list[dict] | None = None,
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

    Malformed substance files are skipped, not fatal (issue #90): their parse
    errors append to ``malformed_out`` (when given) for the caller to report,
    and their existing rows are SHIELDED from the reap — a file that fails to
    parse is present-but-broken, not vanished.

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
    # Snapshots are CLI-written and code-prefixed too; re-home them on the same
    # project_id key so a code change doesn't strand them (mig 078).
    _rehome_snapshot_codes(client, project_id=project_id, project_code=project_code)
    # Relations too — the healer that was missing while 45 short-code edges
    # accumulated (mig 129 cleaned the backlog; this keeps it clean).
    _rehome_relation_codes(client, project_id=project_id, project_code=project_code)

    parsed, malformed = _load_substance_items(project_dir)
    if malformed_out is not None:
        malformed_out.extend(malformed)
    skipped_est_ids = {m["est_item_id"] for m in malformed}
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
            "id, est_item_id, framing, body, status, layer, serves, archived, "
            "field_states, review_flags, origin, version_label"
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

    # Authored-live shield (#113): when MC-2 holds a LIVE authored version of an
    # element (add_spine_version wrote e.g. v7 and demoted the distilled v6),
    # the substance FILE on disk still claims its own top version is live — the
    # disk→DB reconcile would flip the superseded v6 back to 'live' and the
    # element would carry TWO live rows (the sap-5174 e94d0a03 double-listing).
    # Authored rows are MC-2-owned and disk is downstream of them (same rule as
    # the reap's authored exemption), so a disk 'live' older than the newest
    # authored live version is stale by construction → mirror it superseded.
    # Applied BEFORE the per-field reconcile so an already-flipped row heals on
    # the next sync (cur='live', new='superseded' → unconfirmed disk-wins).
    authored_live_max: dict[str, int] = {}
    for r in prior:
        if (r.get("origin") == "authored" and r.get("status") == "live"
                and not r.get("archived")):
            eid = r.get("est_item_id")
            n = version_number(r.get("version_label"))
            if eid and n > authored_live_max.get(eid, -1):
                authored_live_max[eid] = n

    for row in rows:
        shielded = (row.get("status") == "live"
                    and version_number(row.get("version_label"))
                    < authored_live_max.get(row.get("est_item_id"), -1))
        if shielded:
            row["status"] = "superseded"
            logger.warning(
                "authored-live shield (#113): element %s disk %s claims live "
                "but authored v%d is live — mirroring the disk version "
                "superseded (the substance file is stale; re-distill or "
                "update it)",
                row.get("est_item_id"), row.get("version_label"),
                authored_live_max.get(row.get("est_item_id"), -1),
            )
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
            # The shield outranks a CONFIRMED status (#113 follow-up): two
            # live version rows on one element is an invariant violation, not
            # a field preference — and the authored live version IS the newer
            # human action, so keeping the confirmed 'live' here would leave
            # the element double-live permanently (reconcile discards the
            # supersede every sync). The field stays confirmed and the drift
            # review_flag reconcile just emitted stays raised, so the override
            # is visible on the review surface, not silent.
            if shielded and row.get("status") == "live":
                row["status"] = "superseded"
                logger.warning(
                    "authored-live shield (#113): element %s %s status is "
                    "confirmed-live; overriding to superseded anyway (a "
                    "strictly newer authored live version exists)",
                    row.get("est_item_id"), row.get("version_label"),
                )
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
        # Reap shield (issue #90): a malformed file's rows are absent from
        # present_ids because the file failed to PARSE, not because it
        # vanished — reaping (or source-missing-flagging) them here would turn
        # a typo'd version header into data loss. Match on est_item_id, with
        # an id-prefix fallback for legacy rows predating the column.
        if existing.get("est_item_id") in skipped_est_ids or any(
            row_id.startswith(f"{project_code}/{eid}/")
            for eid in skipped_est_ids
        ):
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

    # Reverse-mirror (DB→disk): regenerate spine/_authored/<slug>.md for every
    # origin='authored' element. Authored rows are MC-2-owned and may have no
    # disk file; this materializes them so they surface in the tree (the caller's
    # `git add -A` picks the files up). Best-effort: a mirror failure must NEVER
    # abort sync — the rows persist in MC-2 regardless.
    try:
        authored = (
            client.table(_SUBSTANCE_TABLE)
            .select(_AUTHORED_SELECT)
            .eq("project_id", project_id)
            .eq("origin", "authored")
            .execute()
            .data
        ) or []
        # Account-scoped rows (promoted stakeholders — mig 104) mirror under
        # the ACCOUNT directory, not this project's spine/: they are company
        # facts this project merely originated. Split them out here; they are
        # handled below with the sibling promotions.
        groups: dict[str, list[dict]] = {}
        for r in authored:
            if (r.get("scope") or "project") == "account":
                continue
            groups.setdefault(r["est_item_id"], []).append(r)
        # Steps (mig 119) mirror into the element's frontmatter. Fetch the whole
        # project's trail once, grouped by est_item_id, and hand each element its
        # own steps. Best-effort with the rest of the reverse-mirror.
        steps_by_item: dict[str, list[dict]] = {}
        try:
            step_rows = (
                client.table(Tables.SPINE_STEPS)
                .select("est_item_id, position, title, status, step_date, note")
                .eq("project_id", project_id)
                .execute()
                .data
            ) or []
            for s in step_rows:
                steps_by_item.setdefault(s["est_item_id"], []).append(s)
        except Exception as exc:  # noqa: BLE001 — steps table may not exist yet
            logger.warning("step reverse-mirror fetch skipped for %s: %s",
                           project_code, exc)
        for est_item_id, group in groups.items():
            slug = (est_item_id.split("/", 1)[1]
                    if "/" in est_item_id else est_item_id)
            # Element-level archive (retire) → skip the mirror entirely and
            # drop any stale file the element left behind. Mirrors the account
            # mirror's archived-skip below; without it a retired element (zero
            # live versions) trips write_authored_element's one-live guard and
            # aborts the whole loop, stranding every later element's mirror.
            if all(r.get("archived") for r in group):
                (project_dir / "spine" / "_authored" / f"{slug}.md").unlink(
                    missing_ok=True)
                continue
            # Per-element best-effort: a single malformed element (e.g. a
            # lingering zero/two-live state) must not abort the rest of the
            # sweep — log it and move on.
            try:
                write_authored_element(
                    project_dir, project_code=project_code,
                    est_item_id=est_item_id, rows=group,
                    steps=steps_by_item.get(est_item_id),
                )
            except Exception as exc:  # noqa: BLE001 — one bad element
                logger.warning(
                    "authored element mirror skipped for %s / %s: %s",
                    project_code, est_item_id, exc,
                )
    except Exception as exc:  # noqa: BLE001 — best-effort authored reverse-mirror
        logger.warning(
            "authored reverse-mirror skipped for %s: %s",
            project_code, exc, exc_info=True,
        )

    # Account mirror (DB→disk): the COMPANY's account-scoped elements land in
    # `<account-dir>/_stakeholders/<slug>.md` (the account dir is the working
    # dir's parent under the v0.9.0 layout). Fetched by company_id so sibling
    # projects' promotions appear too, and each project's sync converges on
    # the same files (idempotent). Archived elements are skipped; a stale
    # project-side mirror file from before promotion is removed. Best-effort:
    # never aborts sync.
    try:
        _mirror_account_elements(client, project_id=project_id,
                                 project_code=project_code,
                                 project_dir=project_dir)
    except Exception as exc:  # noqa: BLE001 — best-effort account mirror
        logger.warning(
            "account mirror skipped for %s: %s", project_code, exc, exc_info=True,
        )

    return len(rows)


def _mirror_account_elements(client, *, project_id: str, project_code: str,
                             project_dir: Path) -> int:
    """Mirror the project's company account-scoped authored elements to
    `<account-dir>/_stakeholders/`. Returns how many elements were written."""
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

    rows = (
        client.table(_SUBSTANCE_TABLE)
        .select(_AUTHORED_SELECT)
        .eq("company_id", company_id)
        .eq("scope", "account")
        .eq("origin", "authored")
        .execute()
        .data
    ) or []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["est_item_id"], []).append(r)

    stakeholders_dir = project_dir.parent / "_stakeholders"
    written = 0
    expected: set[str] = set()
    for est_item_id, group in groups.items():
        # Element-level archive (retire) → skip the mirror entirely.
        if all(r.get("archived") for r in group):
            continue
        write_authored_element(
            project_dir, project_code=project_code,
            est_item_id=est_item_id, rows=group,
            out_dir=stakeholders_dir,
        )
        written += 1
        # Pre-promotion mirror file under this project's spine/_authored/ is
        # now stale (the element moved up the scope ladder) — remove it.
        slug = est_item_id.split("/", 1)[1] if "/" in est_item_id else est_item_id
        expected.add(f"{slug}.md")
        stale = project_dir / "spine" / "_authored" / f"{slug}.md"
        stale.unlink(missing_ok=True)

    # Reap (the mirror's other half): a file whose element left the account
    # scope — demoted back to its provenance project, or retired — must not
    # linger in `_stakeholders/`. The dir is engine-owned (created only by
    # this mirror), so reconciling every `*.md` not in this fetch's expected
    # set is safe. A demoted element reappears as a normal `_authored/` file
    # on its provenance project's next sync; nothing is lost, only re-filed.
    if stakeholders_dir.is_dir():
        for stale_file in stakeholders_dir.glob("*.md"):
            if stale_file.name not in expected:
                stale_file.unlink(missing_ok=True)
    return written


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
