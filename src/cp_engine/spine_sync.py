"""Project Spine slice 2 — mirror spine-element frontmatter into MC-2 rows.

Source of truth is the markdown file's frontmatter; this reconciles the
`spine_elements` table to match what's on disk for one project: upsert every
present element, delete rows whose element_id no longer exists on disk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cp_engine.spine import element_to_row, load_spine

_TABLE = "spine_elements"

# The only fields a human can confirm in MC-2; sync reconciles these rather
# than overwriting. Everything else on the row is proposed/overwrite.
_TRACKED_FIELDS = ("status", "stage", "target_date", "serves", "depends_on")


def reconcile_field(field, current_value, current_field_state, new_value, now_iso):
    """Decide stored value + field state + optional review_flag for one tracked field.

    Absent/proposed state ⇒ accept the new derived value. Confirmed + same ⇒
    no-op. Confirmed + different ⇒ keep the confirmed value and emit a
    review_flag. Never clobber a confirmed value — this is the one-way door
    that makes MC-2 the authoritative spine.
    """
    state = current_field_state or "proposed"
    if state != "confirmed":
        return new_value, "proposed", None
    if new_value == current_value:
        return current_value, "confirmed", None
    flag = {"field": field, "was": current_value, "now": new_value, "at": now_iso}
    return current_value, "confirmed", flag


def _merge_flag(review_flags, field, flag, *, source=None):
    """Return review_flags with at most one open flag per (field, source).

    A persistent confirmed/disk conflict would otherwise append a fresh flag on
    every sync (sync runs on every auto-ingest push + SessionStart), growing the
    column without bound and burying the review surface. We keep one current
    flag per field PER PRODUCER: drop any prior flag for the same field that
    shares the incoming flag's `source`, then add the new one (if any).

    Source-awareness matters because two independent producers now write flags:
    reconcile (`source` absent) and the sweep (`source="sweep"`). Keying on
    field alone would let each producer's clean-up wipe the other's flag every
    cycle. When `flag` is None (a clean-up call), pass the producer's `source`
    explicitly so only that producer's stale flag for the field is pruned.
    """
    incoming_source = flag.get("source") if flag is not None else source

    def _same_producer(f):
        return f.get("field") == field and f.get("source") == incoming_source

    pruned = [f for f in review_flags if not _same_producer(f)]
    if flag is not None:
        pruned.append(flag)
    return pruned


def _has_confirmed_field(row, tracked_fields=_TRACKED_FIELDS):
    """True if any tracked field on this MC-2 row is human-confirmed."""
    states = row.get("field_states") or {}
    return any(states.get(f) == "confirmed" for f in tracked_fields)


def sync_spine_elements(
    client,
    *,
    project_id: str,
    project_dir: Path,
    tenant_root: Path,
    now: datetime | None = None,
) -> int:
    """Reconcile `spine_elements` rows for one project to match disk.

    For the five human-confirmable fields (`status`, `stage`, `target_date`,
    `serves`, `depends_on`) this is a RECONCILE, not an overwrite: a confirmed
    value in MC-2 wins over the disk-derived value, and the divergence is
    recorded as a `review_flag` (see `reconcile_field`). All other columns are
    written as-is. `confirmed_by`/`confirmed_at` are human-only and are never
    included in the payload.

    Returns the number of elements upserted. A project with no `spine/` dir is
    a clean no-op (returns 0) but STILL reaps any stale rows it left behind."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()

    elements = load_spine(project_dir)
    rows = [
        element_to_row(e, project_id=project_id, project_root=tenant_root)
        for e in elements
    ]
    present_ids = {r["element_id"] for r in rows}

    # Fetch existing rows once so we can reconcile the tracked fields against
    # the human-verified spine. Explicit columns only (never select('*')).
    prior = (
        client.table(_TABLE)
        .select(
            "element_id, status, stage, target_date, serves, depends_on, "
            "field_states, review_flags"
        )
        .eq("project_id", project_id)
        .execute()
        .data
    ) or []
    existing_by_id = {r["element_id"]: r for r in prior}

    for row in rows:
        existing = existing_by_id.get(row["element_id"])
        if existing is None:
            # New element: nothing to reconcile against, start clean.
            row["field_states"] = {}
            row["review_flags"] = []
            continue
        field_states = dict(existing.get("field_states") or {})
        review_flags = list(existing.get("review_flags") or [])
        for field in _TRACKED_FIELDS:
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
        client.table(_TABLE).upsert(rows, on_conflict="element_id").execute()

    # Reap orphans: rows for this project whose element_id vanished from disk.
    # A row carrying ANY confirmed field is part of the human-verified spine —
    # deleting it because its markdown source vanished would clobber confirmed
    # state, the exact thing the inversion forbids. Such rows are NOT deleted;
    # instead we flag that the source went missing so a human can resolve it.
    for element_id, existing in existing_by_id.items():
        if element_id in present_ids:
            continue
        if _has_confirmed_field(existing):
            review_flags = _merge_flag(
                list(existing.get("review_flags") or []),
                "source",
                {"field": "source", "was": "present", "now": "missing",
                 "at": now_iso},
            )
            client.table(_TABLE).update(
                {"review_flags": review_flags}
            ).eq("element_id", element_id).execute()
        else:
            client.table(_TABLE).delete().eq(
                "element_id", element_id
            ).execute()

    return len(rows)


_SNAPSHOTS_TABLE = "spine_snapshots"


def sync_spine_snapshots(
    client,
    *,
    project_code: str,
    project_dir: Path,
    tenant_root: Path,
    project_id: str | None = None,
) -> int:
    """Reconcile spine_snapshots rows for one project to match disk.

    Scans every spine/<Layer>/*.snapshots/*.md, upserts a row per snapshot
    file, reaps rows whose file vanished (scoped per-project). Returns count
    upserted. `project_id` (the stable MC-2 uuid) is stamped onto each mirrored
    row so code-rehome can key on it (mig 078)."""
    from cp_engine.spine_snapshot import row_from_frozen

    rows = []
    spine_root = project_dir / "spine"
    if spine_root.is_dir():
        for snap_dir in sorted(spine_root.glob("*/*.snapshots")):
            for md in sorted(snap_dir.glob("*.md")):
                row = row_from_frozen(
                    md, tenant_root=tenant_root, project_id=project_id
                )
                if row is not None:
                    rows.append(row)
    present_ids = {r["id"] for r in rows}

    if rows:
        client.table(_SNAPSHOTS_TABLE).upsert(rows, on_conflict="id").execute()

    # Reap orphans: rows for this project whose snapshot file vanished.
    existing = (
        client.table(_SNAPSHOTS_TABLE)
        .select("id")
        .eq("project_code", project_code)
        .execute()
        .data
    ) or []
    for row in existing:
        if row["id"] not in present_ids:
            client.table(_SNAPSHOTS_TABLE).delete().eq("id", row["id"]).execute()

    return len(rows)
