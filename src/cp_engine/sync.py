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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

from cp_engine.config import TenantConfig
from cp_engine.render import (
    count_exceptions_in_window,
    render_claude_md,
    render_dropbox_md,
    render_exceptions_readme,
    render_gitignore,
    render_master_cp,
    render_project_cp,
    render_linked_repo_md,
    render_repo_md,
    splice_managed_region,
)
# Re-exported here so existing `from cp_engine.sync import ProjectState, Issue`
# imports keep working — the data shapes live in cp_engine.state to break
# the sync ↔ render circular dependency.
from cp_engine.state import (  # noqa: F401
    INACTIVE_DIR_NAME,
    Issue,
    LinkedRepo,
    ProjectState,
    dir_slug,
    inactive_root,
    scope_for,
    scope_root,
    working_dir,
)


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

    def read_allocations(
        self,
        config: TenantConfig,
        week_start: str,
    ):
        """Return WeeklyAllocations for the given Monday week_start.

        Optional — backends may return an empty WeeklyAllocations if they
        don't track per-week per-person allocations.
        """
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
    files_deactivated: tuple[Path, ...]  # working dirs moved to <scope>/inactive/<code>/
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

    # Derive each project's one-line summary from the existing project CP
    # file's hand-written content (Quick Resume's "Current work:" line, or
    # the first non-placeholder paragraph in Current Work). Returns None
    # when the CP doesn't exist or has no human-written content yet — in
    # that case the master-CP renders an empty summary cell.
    projects = tuple(
        replace(p, one_line_summary=_derive_summary(config, p))
        for p in projects
    )

    # Read sprint allocations for last week (Monday-starting). v0.2.4 only
    # surfaces last week; "this week" is entered live during the partners'
    # review session, so it's incomplete at sync time and not useful here.
    last_week_start = _last_week_monday(sync_clock)
    try:
        allocations = backend.read_allocations(config, last_week_start.isoformat())
    except (AttributeError, NotImplementedError):
        # Backend doesn't implement allocations — that's fine. Render
        # without them.
        allocations = None

    files_written: list[Path] = []

    # Master CP — splice if exists, full-write if not. Timestamp-only diffs
    # don't trigger a write (would otherwise produce hourly noise commits).
    # `agenda` and `sprint-facts-strip` are owned by the post-sprint-files
    # second-pass below (they need parsed sprint files to compute their
    # content), so we skip them here to avoid wiping populated content
    # with an empty render. Step 2 (below) splices them with the parsed
    # sprint files in hand.
    master_path = config.root / "master-cp.md"
    exceptions_count = count_exceptions_in_window(config.root, days=7)
    new_master = render_master_cp(
        config,
        projects,
        last_sync=sync_clock,
        allocations=allocations,
        exceptions_count=exceptions_count,
    )
    first_pass_regions = tuple(
        r for r in _MASTER_REGIONS if r not in ("agenda", "sprint-facts-strip")
    )
    if _write_if_changed(
        master_path,
        new_master,
        splice_regions=first_pass_regions,
        cosmetic_regions=_MASTER_COSMETIC_REGIONS,
    ):
        files_written.append(master_path)

    # CLAUDE.md — fully generated; overwrite if changed.
    claude_path = config.root / "CLAUDE.md"
    new_claude = render_claude_md(config)
    if _write_if_changed(claude_path, new_claude, splice_regions=()):
        files_written.append(claude_path)

    # .gitignore — generated, idempotent. Tenants shouldn't hand-edit;
    # exceptions can layer in per-scope or per-project .gitignore files
    # if real cases emerge (see v0.3 design doc).
    gitignore_path = config.root / ".gitignore"
    if _write_if_changed(gitignore_path, render_gitignore(), splice_regions=()):
        files_written.append(gitignore_path)

    # Project CPs — v0.7 layout: each project gets a working directory at
    # <scope>/<dir_slug>/ where dir_slug encodes both the code and a
    # slugified name for human readability (e.g.
    # `ggl-5177-event-safety-playbook`). Pre-v0.7 layouts had an extra
    # `projects/` segment; `cp migrate-projects-flat` migrates them in
    # place. The slug is recomputed every sync, so a name change in MC-2
    # prompts a `git mv` of the dir on next sync. Filter internal projects
    # to match what the master CP surfaces.

    # Compute the set of (scope, code) pairs that should exist as live
    # working dirs after this sync. Internal projects don't get dirs.
    # Tracked by code (not slug) because the deactivation sweep needs to match
    # against project identity, which is invariant under name changes.
    live_dirs: set[tuple[str, str]] = {
        (scope_for(p.company_kind), p.code)
        for p in projects
        if not p.is_internal
    }

    for project in projects:
        if project.is_internal:
            continue
        scope = scope_for(project.company_kind)
        scope_dir = scope_root(config.root, scope)
        target_slug = dir_slug(project.code, project.name)
        project_dir = working_dir(config.root, scope, target_slug)

        # Find an existing dir for this project, even if its slug has drifted
        # from the current name (or hasn't been slugged yet — legacy
        # bare-code dirs from v0.3.0/v0.3.1).
        existing_live = _find_project_dir(scope_dir, project.code)
        existing_inactive = _find_project_dir(
            inactive_root(config.root, scope), project.code
        )

        if existing_live is not None and existing_live != project_dir:
            # Slug drift: rename live dir to current slug.
            project_dir.parent.mkdir(parents=True, exist_ok=True)
            existing_live.rename(project_dir)
            logger.info(
                "Renamed working dir %s → %s (name drift in MC-2).",
                existing_live.relative_to(config.root),
                project_dir.relative_to(config.root),
            )
            for path in sorted(project_dir.rglob("*")):
                if path.is_file():
                    files_written.append(path)
        elif existing_live is None:
            if existing_inactive is not None:
                # Reactivate: project came back to live. Restore the dir
                # (and apply any slug drift while we're at it).
                project_dir.parent.mkdir(parents=True, exist_ok=True)
                existing_inactive.rename(project_dir)
                logger.info(
                    "Reactivated inactive project %s (%s).",
                    project.code,
                    project_dir.relative_to(config.root),
                )
                for path in sorted(project_dir.rglob("*")):
                    if path.is_file():
                        files_written.append(path)
            else:
                project_dir.mkdir(parents=True, exist_ok=True)

        cp_path = project_dir / "cp.md"
        if not cp_path.exists():
            body = render_project_cp(config, project, tracked_issues=())
            cp_path.write_text(body)
            files_written.append(cp_path)

        # _dropbox.md — re-render on every sync so a URL change in MC-2
        # propagates without manual intervention. The renderer returns
        # None for projects without a Dropbox URL (most repos); skip those.
        dropbox_body = render_dropbox_md(project)
        dropbox_path = project_dir / "_dropbox.md"
        if dropbox_body is None:
            # Project shouldn't have _dropbox.md (no URL). If a stale one
            # exists from a prior URL, leave it — humans may have hand-edited.
            pass
        elif _write_if_changed(dropbox_path, dropbox_body, splice_regions=()):
            files_written.append(dropbox_path)

        # _repo.md — repo-source projects get a link to their GitHub repo,
        # mirroring _dropbox.md's role for engagements. Engagements get None
        # back from the renderer and skip this entirely.
        #
        # Build a flat user → path map for THIS repo from the committed
        # multi-user table. Empty dict (no users have it) renders as v0.3.3
        # shape; populated dict renders one **Local clone (User):** line each.
        clones_for_this_repo: dict[str, str] = {}
        if project.repo_name:
            for user, paths in config.local_repos_by_user.items():
                if project.repo_name in paths:
                    clones_for_this_repo[user] = paths[project.repo_name]
        repo_body = render_repo_md(
            project, local_clones_by_user=clones_for_this_repo or None
        )
        repo_path = project_dir / "_repo.md"
        if repo_body is None:
            pass
        elif _write_if_changed(repo_path, repo_body, splice_regions=()):
            files_written.append(repo_path)

        # _repo-<repo-name>.md per linked repo — engagements with repos in
        # MC-2 (repos.project_id pointing at the engagement) get one file
        # per linked repo, mirroring the standalone-repo _repo.md pattern.
        # Only meaningful for engagements; standalone-repo ProjectStates
        # have project.linked_repos == ().
        for linked in project.linked_repos:
            clones_for_linked: dict[str, str] = {}
            for user, paths in config.local_repos_by_user.items():
                if linked.repo_name in paths:
                    clones_for_linked[user] = paths[linked.repo_name]
            linked_body = render_linked_repo_md(
                project.name,
                linked,
                local_clones_by_user=clones_for_linked or None,
            )
            linked_path = project_dir / f"_repo-{linked.repo_name}.md"
            if _write_if_changed(linked_path, linked_body, splice_regions=()):
                files_written.append(linked_path)

    # Sprint files — per-project per-sprint markdown for the partners' weekly
    # review. Generated for every active project that the master CP also
    # surfaces. Engine-managed regions are refreshed every sync; hand-written
    # regions are preserved. Lazy-imported to break the sync ↔ sprints
    # circular dependency (sprints.py imports `_extract_region` from here).
    from cp_engine.sprints import (
        _short_md_date,
        current_sprint_week_iso,
        ensure_sprint_files_for_active_projects,
        is_in_sprint_window,
        parse_sprint_file,
        prior_sprint_week_iso,
        render_current_sprint_block,
        render_sprint_index,
        sprint_week_dates,
    )
    if is_in_sprint_window(sync_clock):
        # Pass the full project list; the orchestrator owns the active-filter
        # rule (engagement → is_active_status + not internal; repo → status ==
        # "Active"). Pre-filtering here would strip FPSF + Canonic repos,
        # which is what v0.8.0 did wrong — see _is_active_for_sprint.
        active_for_sprints = tuple(projects)
        sprint_paths = ensure_sprint_files_for_active_projects(
            active_projects=active_for_sprints,
            sprint_root=config.root / "sprints",
            now=sync_clock,
            per_project_data=_collect_sprint_per_project_data(projects, allocations),
        )
        files_written.extend(sprint_paths)

        # Splice the rendered "Current sprint" block into each project's
        # cp.md. New scaffolds already carry the `current-sprint` markers
        # from the template (see render_project_cp); legacy cp.md files
        # written before this region existed get the markers + body
        # injected via `_ensure_current_sprint_markers` so the splicer
        # has something to write into. Missing or unparseable sprint
        # files are skipped silently — this region is best-effort and
        # shouldn't break sync.
        week_iso = current_sprint_week_iso(sync_clock)
        # Collect parsed sprint files as we walk them so the post-loop
        # master-cp re-render (for the agenda rollup) doesn't have to
        # re-parse every file.
        parsed_files: list = []
        for project in active_for_sprints:
            sprint_path = config.root / "sprints" / week_iso / f"{project.code}.md"
            if not sprint_path.exists():
                continue
            scope = scope_for(project.company_kind)
            slug = dir_slug(project.code, project.name)
            cp_path = config.root / scope / slug / "cp.md"
            if not cp_path.exists():
                continue
            try:
                sf = parse_sprint_file(sprint_path)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "Skipping current-sprint splice for %s: %s",
                    project.code, exc,
                )
                continue
            parsed_files.append(sf)
            link_path = f"../../sprints/{week_iso}/{project.code}.md"
            block = render_current_sprint_block(sf, link_path=link_path)
            existing = cp_path.read_text()
            seeded = _ensure_current_sprint_markers(existing)
            new_body = splice_managed_region(seeded, "current-sprint", block)
            if new_body != existing:
                cp_path.write_text(new_body)
                if cp_path not in files_written:
                    files_written.append(cp_path)

        # Per-week sprint-index README — one row per active project,
        # written only when we actually generated sprint files this run
        # (avoids a stub README in tenants with no active work). The
        # README sits alongside the per-project sprint files at
        # `sprints/<week_iso>/README.md`.
        if parsed_files:
            week_start_iso, week_end_iso = sprint_week_dates(sync_clock)
            week_dates_str = (
                f"{_short_md_date(week_start_iso)} – {_short_md_date(week_end_iso)}"
            )
            index_body = render_sprint_index(
                week_iso=week_iso,
                week_dates=week_dates_str,
                sprint_files=parsed_files,
            )
            index_path = config.root / "sprints" / week_iso / "README.md"
            if _write_if_changed(index_path, index_body, splice_regions=()):
                files_written.append(index_path)

        # Re-render the master CP with the parsed sprint files so its
        # `agenda` and `sprint-facts-strip` regions pick up cross-project
        # escalated risks, stale asks, maturing horizon decisions, and
        # the aggregate sprint-totals strip. This is a second pass over
        # master-cp.md (the first happened at top-of-sync, before sprint
        # files were generated). We splice ONLY those two regions so the
        # no-op-resync semantics for unchanged regions stay intact.
        if parsed_files:
            new_master_with_agenda = render_master_cp(
                config,
                projects,
                last_sync=sync_clock,
                allocations=allocations,
                exceptions_count=exceptions_count,
                current_sprint_iso=week_iso,
                prior_sprint_iso=prior_sprint_week_iso(sync_clock),
                parsed_sprint_files=tuple(parsed_files),
                today=sync_clock.date(),
            )
            if (
                _write_if_changed(
                    master_path,
                    new_master_with_agenda,
                    splice_regions=("agenda", "sprint-facts-strip"),
                )
                and master_path not in files_written
            ):
                files_written.append(master_path)

    # Exceptions README — engine-managed listing of session captures from
    # unregistered repos. Only render when the directory exists (creating it
    # would commit an empty README into every tenant; we want the file to
    # appear only after a real exception lands).
    exceptions_dir = config.root / "exceptions"
    if exceptions_dir.exists():
        readme_path = exceptions_dir / "README.md"
        new_readme = render_exceptions_readme(config.root)
        if _write_if_changed(
            readme_path,
            new_readme,
            splice_regions=("exceptions-list",),
        ):
            files_written.append(readme_path)

    # Deactivation sweep — any live working dir whose code isn't in
    # `live_dirs` represents a project that fell out of sync's view (MC-2
    # status flipped, deleted, or is_internal=true). Move the whole
    # working dir to <scope>/inactive/<dir_slug>/. Reactivation is
    # symmetric: a live dir matching an inactive one gets restored.
    files_deactivated = _deactivate_stale_cps(config.root, live_dirs)

    return SyncResult(
        projects_seen=len(projects),
        files_written=tuple(files_written),
        files_deactivated=tuple(files_deactivated),
        no_op=not (files_written or files_deactivated),
    )


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


# Engine-managed regions in master-cp.md. Order doesn't matter for splicing,
# but listing them here keeps the contract explicit.
_MASTER_REGIONS = (
    "last-sync-timestamp",
    "sprint-facts-strip",
    "agenda",
    "exceptions-summary",
    "active-pipeline",
    "active-1p",
    "active-fpsf",
    "active-canonic",
    "last-week-workload",
    "holding-subtable",
    "closed-recent",
)


def _last_week_monday(now: datetime) -> date:
    """Return the Monday that starts the 7-day window ending the day
    before the upcoming sprint-planning Monday.

    The rule is **"next Monday, unless today is Monday, in which case
    today"** — that's the upcoming sprint-planning meeting day. The
    7-day window before that Monday is what gets surfaced as last
    week's allocations.

    Examples (Window = May 4 - May 10, anchor = Monday May 11):
        Saturday May 9 2026   → Mon=May 11, anchor=May 11
        Sunday May 10 2026    → Mon=May 11, anchor=May 11
        Monday May 11 2026    → today IS Monday, anchor=May 11

    Examples after Monday's meeting (Window = May 11 - May 17, anchor = May 18):
        Tuesday May 12 2026   → Mon=May 18, anchor=May 18
        Friday May 15 2026    → Mon=May 18, anchor=May 18

    weekday(): Mon=0, Tue=1, ..., Sun=6
    """
    today = now.date() if isinstance(now, datetime) else now
    if today.weekday() == 0:
        anchor = today
    else:
        days_until_next_monday = (7 - today.weekday()) % 7
        anchor = today + timedelta(days=days_until_next_monday)
    return anchor - timedelta(days=7)

# Regions whose contents change every sync by definition (timestamps, run IDs).
# A change isolated to these regions doesn't justify a write — otherwise every
# hourly cron would commit a one-line timestamp tick. The comparison ignores
# them; if anything else differs, we write (and the timestamp refreshes as a
# side effect).
_MASTER_COSMETIC_REGIONS = ("last-sync-timestamp",)


def _write_if_changed(
    path: Path,
    new_full_body: str,
    *,
    splice_regions: tuple[str, ...],
    cosmetic_regions: tuple[str, ...] = (),
) -> bool:
    """Write `new_full_body` to `path`, splicing region-by-region if the file
    already exists and `splice_regions` is non-empty. Otherwise overwrite.

    Regions listed in `cosmetic_regions` (e.g. last-sync-timestamp) are
    ignored when deciding whether the file changed: if every other region
    matches, the write is skipped even though the cosmetic region differs.

    Returns True iff the file changed on disk.
    """
    if path.exists() and splice_regions:
        existing = path.read_text()

        # Schema-evolution detection: if ANY required splice region is
        # missing from the existing file, this is a migration boundary
        # (e.g. v0.1 master-cp.md upgrading to v0.2's three sections).
        # Log + full-rewrite. The previous in-place splice would raise
        # MarkerMissing — we trade that for a deterministic recovery.
        #
        # Tradeoff: hand-written content that lived OUTSIDE the old
        # engine-managed regions is also preserved only if `new_full_body`
        # happens to include it. For master-cp.md the file is mostly
        # generated, so this is acceptable. Per-project CPs go through
        # a different code path (scaffold-once-then-leave-alone) so this
        # branch shouldn't fire for them.
        missing = [
            r for r in splice_regions
            if f"<!-- cp-engine:start {r} -->" not in existing
        ]
        if missing:
            logger.warning(
                "%s is missing engine-managed regions (%s). Treating as a "
                "schema-evolution boundary and rewriting from scratch. "
                "Back up any hand-written content outside the engine regions "
                "before re-running sync if that's a concern.",
                path,
                ", ".join(missing),
            )
            if existing == new_full_body:
                return False
            path.write_text(new_full_body)
            return True

        # The "actual" merged content we'd write — splices in every region
        # using the freshly-rendered body.
        merged = existing
        for region in splice_regions:
            merged = splice_managed_region(
                merged, region, _extract_region(new_full_body, region)
            )
        if merged == existing:
            return False

        # Did anything *non-cosmetic* change? Build a comparison candidate
        # that splices the new content for non-cosmetic regions only,
        # leaving cosmetic regions matching `existing`. If that equals
        # existing, the only diff is in cosmetic regions → skip write.
        if cosmetic_regions:
            comparison = existing
            for region in splice_regions:
                if region in cosmetic_regions:
                    continue
                comparison = splice_managed_region(
                    comparison, region, _extract_region(new_full_body, region)
                )
            if comparison == existing:
                return False

        path.write_text(merged)
        return True

    # File doesn't exist OR no regions to splice — full write.
    if path.exists() and path.read_text() == new_full_body:
        return False
    path.write_text(new_full_body)
    return True


_CURRENT_SPRINT_START = "<!-- cp-engine:start current-sprint -->"
_CURRENT_SPRINT_END = "<!-- cp-engine:end current-sprint -->"


def _ensure_current_sprint_markers(body: str) -> str:
    """Inject empty `current-sprint` markers if the cp.md body lacks them.

    The project-CP template (post-Task 19) emits these markers on first
    scaffold, but cp.md files that were created before the region
    existed don't have them — and `splice_managed_region` requires both
    markers to be present. This helper adds them as a no-content block
    immediately before the `tracked-issues` start marker (the natural
    insertion point per the template), or appends them at end-of-file
    as a last resort. Idempotent: a body that already carries the
    start marker is returned unchanged.
    """
    if _CURRENT_SPRINT_START in body:
        return body
    region_block = f"{_CURRENT_SPRINT_START}\n{_CURRENT_SPRINT_END}\n\n"
    anchor = "<!-- cp-engine:start tracked-issues -->"
    if anchor in body:
        return body.replace(anchor, region_block + anchor, 1)
    # No tracked-issues marker either — fall back to appending. The
    # splicer's only contract is that BOTH markers exist; it doesn't
    # care where in the file they sit.
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n" + region_block


def _collect_sprint_per_project_data(projects, allocations) -> dict:
    """Per-project context fed into sprint-file scaffolding.

    v0.8.0 stub: returns empty dict, so all sprint files render with the
    renderer's default fallbacks (sessions_this_week=0, no commits, no
    recent-session paragraph). Later tasks will expand this to aggregate
    session metadata, recent commits, open issues, and last-sprint-hours
    from the existing data sources.
    """
    return {}


def _derive_summary(config: TenantConfig, project: ProjectState) -> str | None:
    """Read project CP from disk; return its one-line summary or None.

    The CP may not exist yet (new project), or may live under a different
    slug than the freshly-computed one (name drift in MC-2 not yet
    reconciled by the rename in sync_tenant). Locate it by code via
    `_find_project_dir`. Returns None when the CP doesn't exist or has
    no human-written content yet.
    """
    from cp_engine.summary import derive_from_project_cp

    scope = scope_for(project.company_kind)
    existing = _find_project_dir(scope_root(config.root, scope), project.code)
    if existing is None:
        return None
    return derive_from_project_cp(existing / "cp.md")


_SCOPE_DIRS: tuple[str, ...] = ("1p", "firstpersonsf", "canonic")


def _dir_code(name: str) -> str:
    """Extract the project code from a working-dir name.

    Working dirs follow the `<code>` or `<code>-<slug>` pattern. The code
    itself can contain hyphens (`ggl-5177`, `mc-2`), so a simple split on
    the first hyphen would mangle them. Heuristic: project codes from
    engagements look like `<prefix>-<digits>` (e.g. `ggl-5177`); from
    repos they're a freeform slug (`mc-2`, `storyos`, `unf-forge`). The
    sync compares against the actual project list rather than parsing —
    this helper is only used by `_deactivate_stale_cps` to surface dirs
    whose code isn't in `live_dirs`. To make that work, we accept either
    of two forms:

      - bare code: dir name == project code (`ggl-5177`)
      - slugged: dir name starts with `<project code>-` (`ggl-5177-...`)

    The caller iterates known live codes and matches; a dir that doesn't
    match any known code is treated as stale.
    """
    return name


def _find_project_dir(parent: Path, code: str) -> Path | None:
    """Locate the working dir for a given project code under `parent`.

    `parent` is either a scope root (`<tenant>/<scope>/`) for live dirs
    or an inactive root (`<tenant>/<scope>/inactive/`) for inactive dirs.

    Matches any of:
      - <parent>/<code>            (legacy v0.3.0/v0.3.1, bare code)
      - <parent>/<code>-<slug>     (current, slugged)

    Returns the first match found, or None. If multiple matches exist
    (shouldn't happen but defensive), prefers the bare-code form so the
    rename logic in sync_tenant moves it to the slugged form.

    Skips `inactive/` (when scanning a scope root) so that bin is
    never confused with a project named "inactive".
    """
    if not parent.exists():
        return None

    bare = parent / code
    if bare.is_dir():
        return bare

    prefix = f"{code}-"
    for path in parent.iterdir():
        if path.is_dir() and path.name != INACTIVE_DIR_NAME and path.name.startswith(prefix):
            return path
    return None


def _deactivate_stale_cps(
    tenant_root: Path,
    live_dirs: set[tuple[str, str]],
) -> list[Path]:
    """Move whole working dirs for stale projects into <scope>/inactive/.

    Triggered when a project drops out of sync's view (MC-2 status
    changed, deleted, or is_internal=true). Hand-edited content survives
    because we move (rename) the whole directory rather than overwrite
    anything. Reactivation is symmetric: a project that comes back to
    live state has its dir restored from inactive/ on the next sync.

    Walks each `<scope>/` for top-level subdirs (skipping the `inactive/`
    subdir itself). A dir whose embedded code isn't in `live_dirs[scope]`
    is moved to `<scope>/inactive/<dir_name>/`, preserving the slug
    suffix.

    Working-dir naming is `<code>` (legacy) or `<code>-<slug>` (current);
    we extract the code by checking each known-live code against the dir
    name. Anything that doesn't match a live code is treated as stale.

    Returns the list of new inactive directory paths.
    """
    moved: list[Path] = []
    live_codes_by_scope: dict[str, set[str]] = {}
    for scope, code in live_dirs:
        live_codes_by_scope.setdefault(scope, set()).add(code)

    for scope in _SCOPE_DIRS:
        scope_dir = scope_root(tenant_root, scope)
        if not scope_dir.exists():
            continue
        inactive_bin = inactive_root(tenant_root, scope)
        live_codes = live_codes_by_scope.get(scope, set())

        for path in scope_dir.iterdir():
            if not path.is_dir():
                continue
            if path.name == INACTIVE_DIR_NAME:
                continue

            # A dir is "live" if its name matches a known live project code
            # in either form (bare or `<code>-<slug>`). Scan known codes.
            if any(
                path.name == code or path.name.startswith(f"{code}-")
                for code in live_codes
            ):
                continue

            # Stale. Move the whole dir.
            inactive_bin.mkdir(exist_ok=True)
            target = inactive_bin / path.name
            if target.exists():
                # v0.1.2 collision rule: never silently overwrite. Skip
                # and warn — a human can resolve by renaming or merging
                # the duplicate.
                logger.warning(
                    "Skipping deactivation of %s: %s already exists. "
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
