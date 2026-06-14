"""Project Shell slice 3 — named snapshots (Phase A).

Pure freeze logic: given a deliverable's working markdown, produce the frozen
snapshot file's content (verbatim body + augmented `snapshot:` frontmatter) and
the `shell_snapshots` index row. No disk/git I/O here — that's the CLI's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import frontmatter


def slugify_label(label: str) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, trim."""
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s or "snapshot"


@dataclass(frozen=True)
class Snapshot:
    filename: str
    frozen_text: str
    row: dict[str, object]


def build_snapshot(
    *,
    working_text: str,
    deliverable_id: str,
    project_code: str,
    label: str,
    reason: str | None,
    commit: str | None,
    working_copy_dirty: bool,
    created: date,
) -> Snapshot:
    """Freeze `working_text` into a named snapshot (content + index row)."""
    slug = slugify_label(label)
    created_iso = created.isoformat()
    # slug is lossy: two distinct same-day labels can collapse to one stem.
    # Same-day collision resolution is the CLI's job (like rel_path below).
    stem = f"{created_iso}-{slug}"
    filename = f"{stem}.md"
    snap_id = f"{deliverable_id}@{stem}"

    post = frontmatter.loads(working_text)
    post.metadata["snapshot"] = {
        "of": deliverable_id,
        "label": label,
        "reason": reason,
        "created": created_iso,
        "commit": commit,
        "working_copy_dirty": working_copy_dirty,
    }
    frozen_text = frontmatter.dumps(post) + "\n"

    row = {
        "id": snap_id,
        "deliverable_id": deliverable_id,
        "project_code": project_code,
        "label": label,
        "reason": reason,
        "commit": commit,
        "rel_path": None,  # filled by the CLI once the file is placed
        "working_copy_dirty": working_copy_dirty,
        "created": created_iso,
    }
    return Snapshot(filename=filename, frozen_text=frozen_text, row=row)
