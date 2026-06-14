"""Project Shell slice 3 — named snapshots (Phase A).

Pure freeze logic: given a deliverable's working markdown, produce the frozen
snapshot file's content (verbatim body + augmented `snapshot:` frontmatter) and
the `shell_snapshots` index row. No disk/git I/O here — that's the CLI's job.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import frontmatter
import yaml


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


def row_from_frozen(path: Path, *, tenant_root: Path) -> dict | None:
    """Build a shell_snapshots row from a frozen snapshot file's frontmatter.

    Returns None if the file lacks a `snapshot:` block (not a snapshot file).
    The row shape MUST match what `build_snapshot` produces (minus rel_path,
    which is computed here from the file's actual location)."""
    try:
        meta = frontmatter.load(str(path)).metadata
    except yaml.YAMLError:
        return None
    snap = meta.get("snapshot")
    if not isinstance(snap, dict):
        return None
    deliverable_id = str(snap.get("of", ""))
    created = str(snap.get("created", ""))
    stem = path.stem
    try:
        rel = str(path.relative_to(tenant_root))
    except ValueError:
        rel = path.name
    return {
        "id": f"{deliverable_id}@{stem}",
        "deliverable_id": deliverable_id,
        # project_code is the deliverable id's first segment, matching the
        # freeze path so both write the same row (and reap stays scoped).
        "project_code": deliverable_id.split("/", 1)[0] if deliverable_id else "",
        "label": str(snap.get("label", "")),
        "reason": snap.get("reason"),
        "commit": snap.get("commit"),
        "rel_path": rel,
        "working_copy_dirty": bool(snap.get("working_copy_dirty", False)),
        "created": created,
    }


# --- deliverable resolution + git helpers (disk/git I/O, isolated) ---
#
# These touch the filesystem and shell out to git, so they live apart from the
# pure freeze logic above. The git helpers MUST NEVER raise — they return
# None/False on any failure (including "not a git repo") so snapshotting works
# even outside version control.


class DeliverableNotFound(Exception):
    pass


def resolve_deliverable_file(project_dir: Path, deliverable_id: str) -> Path:
    """Find the working markdown for a deliverable id under the project's
    Deliverables layer. Snapshots are deliverable-scoped, so only that layer
    is scanned. Malformed frontmatter is skipped rather than fatal."""
    deliv_dir = project_dir / "shell" / "Deliverables"
    if deliv_dir.is_dir():
        for md in sorted(deliv_dir.glob("*.md")):
            try:
                meta = frontmatter.load(str(md)).metadata
            except yaml.YAMLError:
                continue
            if str(meta.get("id")) == deliverable_id:
                return md
    raise DeliverableNotFound(
        f"No Deliverables element with id '{deliverable_id}' under {deliv_dir}"
    )


def git_head_commit(repo: Path) -> str | None:
    """Short HEAD SHA of the repo at `repo`, or None if unavailable.

    Never raises — a missing git binary or a non-repo path yields None, so
    snapshotting works outside version control."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return r.stdout.strip() or None


def working_copy_is_dirty(path: Path, repo: Path) -> bool:
    """True if `path` has uncommitted changes in the repo at `repo`.

    Never raises — any git failure (no repo, no git binary) yields False."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", str(path)],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return bool(r.stdout.strip())
