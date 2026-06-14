"""Project Shell slice 2 — mirror shell-element frontmatter into MC-2 rows.

Source of truth is the markdown file's frontmatter; this reconciles the
`shell_elements` table to match what's on disk for one project: upsert every
present element, delete rows whose element_id no longer exists on disk.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.shell import element_to_row, load_shell

_TABLE = "shell_elements"


def sync_shell_elements(
    client,
    *,
    project_id: str,
    project_dir: Path,
    tenant_root: Path,
) -> int:
    """Reconcile `shell_elements` rows for one project to match disk.

    Returns the number of elements upserted. A project with no `shell/` dir is
    a clean no-op (returns 0) but STILL reaps any stale rows it left behind."""
    elements = load_shell(project_dir)
    rows = [
        element_to_row(e, project_id=project_id, project_root=tenant_root)
        for e in elements
    ]
    present_ids = {r["element_id"] for r in rows}

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
