"""Sync layer — reads source-of-truth state, writes engine-managed regions.

Two backends per spec v02 §4.1:
  - mc-2: reads MC-2's Postgres via Supabase API (for cp-1p, cp-firstpersonsf)
  - github-issues: reads GitHub Issues per project repo (for cp-canonic)

Tenants pick one backend in `.cp-engine.toml [sync]`. Mixed-backend
tenants are not supported in v02.

This module owns the **orchestration** (read → render → splice → write).
Backend implementations live in sibling modules:
  - sync_mc2.py   — MC-2 / Supabase reader
  - sync_github.py — GitHub Issues reader (stub in v0.1; real in v0.2)

v0.1 scope: sync touches only `master-cp.md`'s engine-managed regions
plus the regenerated `CLAUDE.md`. Project CPs are scaffolded from the
template if missing, but their engine-managed `tracked-issues` region
isn't populated until v0.2's github-issues backend lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

from cp_engine.config import TenantConfig
from cp_engine.render import (
    render_claude_md,
    render_master_cp,
    render_project_cp,
    splice_managed_region,
)
# Re-exported here so existing `from cp_engine.sync import ProjectState, Issue`
# imports keep working — the data shapes live in cp_engine.state to break
# the sync ↔ render circular dependency.
from cp_engine.state import Issue, ProjectState  # noqa: F401


# ──────────────────────────────────────────────────────────────────────
#  Errors
# ──────────────────────────────────────────────────────────────────────


class SyncError(Exception):
    """Base class for sync errors."""


class UnknownBackend(SyncError):
    """Tenant config specifies a backend that the engine doesn't support."""


class BackendUnavailable(SyncError):
    """Backend can't be reached (network, auth, missing config)."""


# ──────────────────────────────────────────────────────────────────────
#  Backend protocol
# ──────────────────────────────────────────────────────────────────────


class Backend(Protocol):
    """Anything that can read source-of-truth state for a tenant."""

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        """Return current state for every tracked project in the tenant."""
        ...


# Type for the factory that resolves a backend by name. Tests pass a fake
# factory; the CLI uses _default_backend_factory below.
BackendFactory = Callable[[str], Backend]


# ──────────────────────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one sync cycle for a tenant."""

    projects_seen: int
    files_written: tuple[Path, ...]   # generated/scaffolded files
    files_archived: tuple[Path, ...]  # moved from projects/ → projects/archived/
    no_op: bool                       # True iff nothing changed on disk


def sync_tenant(
    config: TenantConfig,
    *,
    backend_factory: BackendFactory | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """Run one sync cycle for the tenant.

    1. Resolve backend by name (`config.sync.backend`).
    2. Read every tracked project's state.
    3. Render master-cp.md from the read state and write/splice to disk.
    4. Render CLAUDE.md and write to disk (full overwrite — generated file).
    5. For each project not yet on disk, render & write its project CP from
       template. Existing project CPs are left untouched (v0.1).
    6. Return a SyncResult listing every file actually changed.

    Args:
        config: loaded TenantConfig (from cp_engine.config.load).
        backend_factory: optional backend resolver. Defaults to the real
            registry (mc-2 → sync_mc2.MC2Backend). Tests pass a fake.
        now: optional override for the sync timestamp. Defaults to UTC now.
    """
    factory = backend_factory or _default_backend_factory
    sync_clock = now or datetime.now(timezone.utc)

    backend = factory(config.sync.backend)
    projects = backend.read_projects(config)

    files_written: list[Path] = []

    # Master CP — splice if exists, full-write if not.
    master_path = config.root / "master-cp.md"
    new_master = render_master_cp(config, projects, last_sync=sync_clock)
    if _write_if_changed(master_path, new_master, splice_regions=_MASTER_REGIONS):
        files_written.append(master_path)

    # CLAUDE.md — fully generated; overwrite if changed.
    claude_path = config.root / "CLAUDE.md"
    new_claude = render_claude_md(config)
    if _write_if_changed(claude_path, new_claude, splice_regions=()):
        files_written.append(claude_path)

    # Project CPs — scaffold the missing ones; leave existing alone.
    # Filter internal projects to match what the master CP surfaces:
    # is_internal=true projects belong in their own tenant, not in client/
    # public tenants. This keeps file-on-disk state consistent with the
    # rendered master CP.
    projects_dir = config.root / "projects"
    projects_dir.mkdir(exist_ok=True)

    # Compute the set of "live" project codes — what should exist as a
    # CP file in projects/ after this sync. Internal projects don't get
    # CPs in this tenant.
    live_codes = {p.code for p in projects if not p.is_internal}

    for project in projects:
        if project.is_internal:
            continue
        project_path = projects_dir / f"{project.code}.md"
        if not project_path.exists():
            body = render_project_cp(config, project, tracked_issues=())
            project_path.write_text(body)
            files_written.append(project_path)

    # Archive sweep — any file in projects/<code>.md whose <code> is no
    # longer in `live_codes` represents a project that was archived in
    # MC-2, deleted, or flipped to is_internal. Move it to projects/archived/.
    files_archived = _archive_stale_cps(projects_dir, live_codes)

    return SyncResult(
        projects_seen=len(projects),
        files_written=tuple(files_written),
        files_archived=tuple(files_archived),
        no_op=not (files_written or files_archived),
    )


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


# Engine-managed regions in master-cp.md. Order doesn't matter for splicing,
# but listing them here keeps the contract explicit.
_MASTER_REGIONS = (
    "last-sync-timestamp",
    "active-table",
    "holding-subtable",
    "closed-recent",
)


def _write_if_changed(
    path: Path,
    new_full_body: str,
    *,
    splice_regions: tuple[str, ...],
) -> bool:
    """Write `new_full_body` to `path`, splicing region-by-region if the file
    already exists and `splice_regions` is non-empty. Otherwise overwrite.

    Returns True iff the file changed on disk (skip writes when current
    contents already match what we'd produce).
    """
    if path.exists() and splice_regions:
        existing = path.read_text()
        merged = existing
        for region in splice_regions:
            new_region_body = _extract_region(new_full_body, region)
            merged = splice_managed_region(merged, region, new_region_body)
        if merged == existing:
            return False
        path.write_text(merged)
        return True

    # File doesn't exist OR no regions to splice — full write.
    if path.exists() and path.read_text() == new_full_body:
        return False
    path.write_text(new_full_body)
    return True


def _archive_stale_cps(projects_dir: Path, live_codes: set[str]) -> list[Path]:
    """Move CPs for projects no longer surfaced by sync into projects/archived/.

    Triggered when a project is archived in MC-2, deleted, or flipped to
    is_internal=true. Hand-edited content survives because we move (rename)
    rather than overwrite.

    Files already in projects/archived/ are left alone — they're already
    archived. Only top-level projects/<code>.md files are candidates.

    Returns the list of paths that were moved (the new archived location).
    """
    archive_dir = projects_dir / "archived"
    moved: list[Path] = []

    for path in projects_dir.iterdir():
        if path.is_dir():
            continue  # skip projects/archived/ subdir itself
        if path.suffix != ".md":
            continue
        code = path.stem
        if code in live_codes:
            continue

        # Stale. Move it.
        archive_dir.mkdir(exist_ok=True)
        target = archive_dir / path.name
        if target.exists():
            # Conservative: never silently overwrite existing archived
            # content. v0.1.2 logs and skips; a future version may add
            # counter-suffix collision handling.
            logger.warning(
                "Skipping archive of %s: %s already exists. "
                "Resolve the conflict by hand (rename or merge).",
                path,
                target,
            )
            continue

        path.rename(target)
        moved.append(target)

    return moved


def _extract_region(full_body: str, region: str) -> str:
    """Pull the body inside a region's start/end markers from a freshly-rendered
    full body. Used to feed splice_managed_region with just the new region
    content (so the existing file's hand-written content stays untouched).
    """
    start_marker = f"<!-- cp-engine:start {region} -->"
    end_marker = f"<!-- cp-engine:end {region} -->"
    start_idx = full_body.index(start_marker) + len(start_marker)
    end_idx = full_body.index(end_marker)
    return full_body[start_idx:end_idx].strip("\n")


def _default_backend_factory(name: str) -> Backend:
    """Resolve a backend name to an instance. Lazy-imports backend modules
    so e.g. `cp init` doesn't pay the cost of importing supabase."""
    if name == "mc-2":
        from cp_engine.sync_mc2 import MC2Backend
        return MC2Backend()
    if name == "github-issues":
        # v0.2 — placeholder
        raise UnknownBackend(
            "github-issues backend not implemented yet (lands in v0.2)"
        )
    raise UnknownBackend(f"Unknown sync backend: {name!r}")
