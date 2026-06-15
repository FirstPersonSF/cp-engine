"""Project Shell slice 2 — mirror shell-element frontmatter into MC-2 rows.

Source of truth is the markdown file's frontmatter; this reconciles the
`shell_elements` table to match what's on disk for one project: upsert every
present element, delete rows whose element_id no longer exists on disk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cp_engine.shell import element_to_row, load_shell

_TABLE = "shell_elements"

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


def sync_shell_elements(
    client,
    *,
    project_id: str,
    project_dir: Path,
    tenant_root: Path,
    now: datetime | None = None,
) -> int:
    """Reconcile `shell_elements` rows for one project to match disk.

    For the five human-confirmable fields (`status`, `stage`, `target_date`,
    `serves`, `depends_on`) this is a RECONCILE, not an overwrite: a confirmed
    value in MC-2 wins over the disk-derived value, and the divergence is
    recorded as a `review_flag` (see `reconcile_field`). All other columns are
    written as-is. `confirmed_by`/`confirmed_at` are human-only and are never
    included in the payload.

    Returns the number of elements upserted. A project with no `shell/` dir is
    a clean no-op (returns 0) but STILL reaps any stale rows it left behind."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()

    elements = load_shell(project_dir)
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
            if flag is not None:
                review_flags.append(flag)
        row["field_states"] = field_states
        row["review_flags"] = review_flags

    if rows:
        client.table(_TABLE).upsert(rows, on_conflict="element_id").execute()

    # Reap orphans: rows for this project whose element_id vanished from disk.
    existing = (
        client.table(_TABLE)
        .select("element_id")
        .eq("project_id", project_id)
        .execute()
        .data
    ) or []
    for row in existing:
        if row["element_id"] not in present_ids:
            client.table(_TABLE).delete().eq(
                "element_id", row["element_id"]
            ).execute()

    return len(rows)


_SNAPSHOTS_TABLE = "shell_snapshots"


def sync_shell_snapshots(
    client,
    *,
    project_code: str,
    project_dir: Path,
    tenant_root: Path,
) -> int:
    """Reconcile shell_snapshots rows for one project to match disk.

    Scans every shell/<Layer>/*.snapshots/*.md, upserts a row per snapshot
    file, reaps rows whose file vanished (scoped per-project). Returns count
    upserted."""
    from cp_engine.shell_snapshot import row_from_frozen

    rows = []
    shell_root = project_dir / "shell"
    if shell_root.is_dir():
        for snap_dir in sorted(shell_root.glob("*/*.snapshots")):
            for md in sorted(snap_dir.glob("*.md")):
                row = row_from_frozen(md, tenant_root=tenant_root)
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
