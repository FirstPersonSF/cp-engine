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
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

from cp_engine.claude_settings import install_into_tenant
from cp_engine.config import TenantConfig
from cp_engine.render import (
    count_exceptions_in_window,
    render_account_cp,
    render_account_facts_body,
    render_account_projects_body,
    render_claude_md,
    render_dropbox_md,
    render_exceptions_readme,
    render_gitignore,
    render_master_cp,
    render_project_cp,
    render_linked_repo_md,
    render_project_strip_bodies,
    render_repo_md,
    render_sprint_week,
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
    SprintCommit,
    account_scope_for,
    company_slug,
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

    def spine_client(self) -> object:
        """Return the Supabase client used to read projects.

        Used by the spine mirror (slice 2) to reconcile a project's
        `spine/` frontmatter into MC-2's `spine_elements` table without
        re-resolving credentials. Returns the live client instance.
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
    dry_run: bool = False,
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

    # Read sprint allocations for the prior COMPLETED week (Monday-starting).
    # Bug #11: `_last_week_monday` returns the *upcoming sprint-planning*
    # window's Monday — which, at the start of a sprint week, is THIS week and
    # has no logged hours yet, silently emptying the workload section. Use the
    # same "Monday of the previous calendar week" rule prep-planning uses so
    # the two surfaces agree and last week's real hours show.
    last_week_start = _prior_completed_week_monday(sync_clock)
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
        today=sync_clock.date(),
    )
    first_pass_regions = tuple(
        r for r in _MASTER_REGIONS
        if r not in ("agenda", "sprint-facts-strip", "slack-rollup")
    )
    if _write_if_changed(
        master_path,
        new_master,
        splice_regions=first_pass_regions,
        cosmetic_regions=_MASTER_COSMETIC_REGIONS,
        dry_run=dry_run,
    ):
        files_written.append(master_path)

    # CLAUDE.md — fully generated; overwrite if changed.
    claude_path = config.root / "CLAUDE.md"
    new_claude = render_claude_md(config)
    if _write_if_changed(claude_path, new_claude, splice_regions=(), dry_run=dry_run):
        files_written.append(claude_path)

    # .gitignore — generated, idempotent. Tenants shouldn't hand-edit;
    # exceptions can layer in per-scope or per-project .gitignore files
    # if real cases emerge (see v0.3 design doc).
    gitignore_path = config.root / ".gitignore"
    if _write_if_changed(
        gitignore_path, render_gitignore(), splice_regions=(), dry_run=dry_run
    ):
        files_written.append(gitignore_path)

    # .claude/ — engine-managed SessionStart hook that self-heals a stale
    # `cp` CLI vs the tenant's [engine].version pin. Idempotent and
    # non-fatal; preserves any tenant-authored settings/hooks. Skipped under
    # dry-run (it mutates the filesystem directly rather than via
    # _write_if_changed).
    if not dry_run:
        files_written.extend(install_into_tenant(config.root))

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
    # `account_scope_for` gives the full parent path including the account
    # layer for clients (e.g. "1p/google"), so live_dirs reflect where the
    # project actually lives on disk.
    live_dirs: set[tuple[str, str]] = {
        (account_scope_for(p), p.code)
        for p in projects
        if not p.is_internal
    }

    for project in projects:
        if project.is_internal:
            continue
        scope = account_scope_for(project)
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

        if dry_run:
            # Report what would be created; perform no filesystem mutation.
            # (Slug-drift renames / reactivations are reported coarsely as the
            # target cp.md; a precise per-file diff isn't needed for `cp status`.)
            cp_path = project_dir / "cp.md"
            if existing_live is None and not cp_path.exists():
                files_written.append(cp_path)
            continue

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

        # Quick Resume engine-region migration (v0.11.0+, Lever 5).
        # Wrap any unwrapped `## Quick Resume` section in markers so the
        # ingest verbs (current_work / next_up / blockers) have a region
        # to write into. Idempotent: already-wrapped files are unchanged;
        # files without a Quick Resume section pass through. Cutover-
        # sync semantic — runs once per project after the v0.11.0 upgrade,
        # then stays a no-op forever.
        if cp_path.exists():
            existing_qr = cp_path.read_text()
            wrapped = _ensure_quick_resume_markers(existing_qr)
            if wrapped != existing_qr:
                cp_path.write_text(wrapped)
                if cp_path not in files_written:
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

        # Mirror the project's spine into MC-2 (slice 2). Best-effort:
        # a spine mirror failure must never abort the sprint/CP sync that
        # surrounds it. Only this block is wrapped — the surrounding sync is
        # NOT best-effort.
        if project.mc2_id:
            from cp_engine.estimate import fetch_estimate
            from cp_engine.spine_substance_sync import (
                sync_spine_context,
                sync_spine_substance,
            )
            from cp_engine.spine_sync import (
                sync_spine_elements,
                sync_spine_snapshots,
            )

            client = backend.spine_client()
            try:
                sync_spine_elements(
                    client,
                    project_id=project.mc2_id,
                    project_dir=project_dir,
                    tenant_root=config.root,
                    now=sync_clock,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort element mirror
                logger.warning(
                    "spine-element mirror skipped for %s: %s",
                    project.code, exc, exc_info=True,
                )
            try:
                sync_spine_snapshots(
                    client,
                    project_code=project.code,
                    project_dir=project_dir,
                    tenant_root=config.root,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort snapshot mirror
                logger.warning(
                    "spine-snapshot mirror skipped for %s: %s",
                    project.code, exc, exc_info=True,
                )
            # Substance + context mirror (Phase 2). Fetch the live estimate
            # once per project to bind substance versions; if the estimator
            # schema is unreachable, pass estimate=None so substance still
            # mirrors as `unbound` rather than failing the whole sync. A
            # genuinely missing estimate (returns None) flows through the same
            # unbound path. Best-effort like the element/snapshot mirrors.
            try:
                try:
                    estimate = fetch_estimate(client, project.mc2_id)
                except Exception as exc:  # noqa: BLE001 — estimator unreachable
                    logger.warning(
                        "estimate fetch failed for %s (substance → unbound): %s",
                        project.code, exc, exc_info=True,
                    )
                    estimate = None
                sync_spine_substance(
                    client,
                    project_id=project.mc2_id,
                    project_code=project.code,
                    project_dir=project_dir,
                    estimate=estimate,
                    now=sync_clock,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort substance mirror
                logger.warning(
                    "spine-substance mirror skipped for %s: %s",
                    project.code, exc, exc_info=True,
                )
            try:
                sync_spine_context(
                    client,
                    project_id=project.mc2_id,
                    project_code=project.code,
                    project_dir=project_dir,
                    now=sync_clock,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort context mirror
                logger.warning(
                    "spine-context mirror skipped for %s: %s",
                    project.code, exc, exc_info=True,
                )
            # Regenerate the project's `_sources.md` manifest from its ingested
            # assets (ambient-project-sources). Best-effort like the spine
            # mirrors — a manifest failure must never abort sync. The written
            # `_sources.md`/`.cache.json` are picked up by sync's normal
            # `git add -A` commit.
            try:
                from cp_engine.asset_ingest import resolve_project_folders_by_id
                from cp_engine.project_sources import write_sources_manifest

                folders = resolve_project_folders_by_id(client, project.mc2_id)
                if folders is not None:
                    write_sources_manifest(
                        client,
                        project_dir,
                        folders.project_id,
                        folders.company_id,
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort sources manifest
                logger.warning(
                    "sources manifest skipped for %s: %s",
                    project.code, exc, exc_info=True,
                )

    # Account CP scaffolding (v0.8.17+, 1p-only) — for every account that
    # has ≥1 active client project, scaffold `1p/<company-slug>/cp.md`
    # from the account template and re-splice the `account-facts` and
    # `projects` engine regions on every sync. FPSF/Canonic already nest
    # by self-company at the scope level and don't get this layer.
    #
    # Account list is derived from the projects we already have — same
    # source of truth as the rest of master-cp, no separate backend
    # query. company_kind == "client" is the gate; internal client
    # projects are excluded (they don't get a project dir under 1p/, so
    # they shouldn't contribute to an account dir either).
    accounts_to_active_projects: dict[tuple[str, str], list[ProjectState]] = {}
    for project in projects:
        if project.is_internal or project.company_kind != "client":
            continue
        slug = company_slug(project.company_name)
        # Display name falls back to the slug if company_name is missing;
        # company_slug already normalizes None to "unknown".
        display = project.company_name or slug
        accounts_to_active_projects.setdefault((slug, display), []).append(project)

    for (slug, display), account_projects in accounts_to_active_projects.items():
        if dry_run:
            break  # account/sprint/deactivation passes are write-heavy; the
            # dry-run report covers master-cp + CLAUDE + new project CPs.
        account_dir = config.root / "1p" / slug
        account_cp_path = account_dir / "cp.md"
        account_projects_tuple = tuple(account_projects)

        first_scaffold = not account_cp_path.exists()
        if first_scaffold:
            # First scaffold: full render from the template. The account
            # dir already exists (the project loop above created
            # `1p/<slug>/<dir>/` on the way to placing project working
            # dirs), so we only need to write the cp.md itself.
            account_dir.mkdir(parents=True, exist_ok=True)
            scaffold_body = render_account_cp(slug, display, account_projects_tuple)
            account_cp_path.write_text(scaffold_body)

        # Re-splice the two engine-managed regions even on first scaffold
        # so first-render and re-render are byte-stable — otherwise small
        # whitespace differences between the template's `{{ block }}`
        # spacing and the splicer's strip-and-rejoin make the next sync
        # re-write the file unnecessarily (and breaks the no-op promise).
        # Handwritten content outside the markers is byte-stable.
        existing = account_cp_path.read_text()
        new_body = existing
        for region, body in (
            ("account-facts", render_account_facts_body(display, account_projects_tuple)),
            ("projects", render_account_projects_body(account_projects_tuple)),
        ):
            try:
                new_body = splice_managed_region(new_body, region, body)
            except Exception as exc:
                logger.warning(
                    "Skipping %s splice for account %s: %s", region, slug, exc,
                )
        if first_scaffold or new_body != existing:
            if new_body != existing:
                account_cp_path.write_text(new_body)
            if account_cp_path not in files_written:
                files_written.append(account_cp_path)

    # Sprint files — per-project per-sprint markdown for the partners' weekly
    # review. Generated for every active project that the master CP also
    # surfaces. Engine-managed regions are refreshed every sync; hand-written
    # regions are preserved. Lazy-imported to break the sync ↔ sprints
    # circular dependency (sprints.py imports `_extract_region` from here).
    from cp_engine.sprints import (
        _monday_of,
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
    if is_in_sprint_window(sync_clock) and not dry_run:
        # Pass the full project list; the orchestrator owns the active-filter
        # rule (engagement → is_active_status + not internal; repo → status ==
        # "Active"). Pre-filtering here would strip FPSF + Canonic repos,
        # which is what v0.8.0 did wrong — see _is_active_for_sprint.
        active_for_sprints = tuple(projects)
        sprint_paths = ensure_sprint_files_for_active_projects(
            active_projects=active_for_sprints,
            sprint_root=config.root / "sprints",
            now=sync_clock,
            # Sprint-start floor for the "Recent activity" commit walk.
            # Use the *calendar* Monday of `sync_clock`, NOT the planning
            # Monday — `sprint_week_dates` rolls Wed-Sun to next week's
            # Monday (for sprint-file labeling), which would put the
            # commit floor in the future for half the week and yield an
            # always-empty "Commits (last 7 days)" section. The renderer's
            # own header is "last 7 days", so anchoring at the calendar
            # Monday keeps the displayed window consistent with what the
            # heading promises.
            per_project_data=_collect_sprint_per_project_data(
                config,
                projects,
                allocations,
                sprint_start=_monday_of(sync_clock),
            ),
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
            scope = account_scope_for(project)
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

            # `_week.md` — week-scope handwritten notes (themes, attendance,
            # meta). Created on first sync of a new week and never touched
            # again — handwritten content is sacred, like weekly-cp.md. The
            # weekly-cp.md `themes-strip` engine-managed region reads from
            # this file's `## Themes` section.
            week_path = config.root / "sprints" / week_iso / "_week.md"
            if not week_path.exists():
                week_label = week_iso.split("-W", 1)[-1] if "-W" in week_iso else week_iso
                week_body = render_sprint_week(
                    week_iso=week_iso,
                    week_label=f"W{week_label}",
                    week_dates=week_dates_str,
                )
                week_path.write_text(week_body)
                files_written.append(week_path)

        # Phase 1.2 (v0.8.5) — project cp.md strip regions. For each active
        # project, aggregate its sprint-file content into the four engine-
        # managed regions (inbound, recent-decisions, open-asks, stakeholders)
        # and splice them in. Runs once with the full parsed_files context
        # so a project's strips can pull from all of its sprint history
        # represented in the current sync.
        if parsed_files:
            from cp_engine.aggregators import aggregate_project_strips
            from cp_engine.sprints import _is_active_for_sprint
            today_for_strips = sync_clock.date()
            parsed_tuple = tuple(parsed_files)
            for project in projects:
                if not _is_active_for_sprint(project):
                    continue
                scope = account_scope_for(project)
                slug = dir_slug(project.code, project.name)
                cp_path = config.root / scope / slug / "cp.md"
                if not cp_path.exists():
                    continue
                strips = aggregate_project_strips(
                    project.code, parsed_tuple, today_for_strips
                )
                bodies = render_project_strip_bodies(strips)
                existing = cp_path.read_text()
                # Seed markers for tenants whose cp.md was scaffolded before
                # v0.8.5 (they have current-sprint markers but no strip
                # markers). Append the four strip regions in-place after
                # tracked-issues if missing.
                seeded = _ensure_strip_markers(existing)
                new_body = seeded
                for region, body in bodies.items():
                    try:
                        new_body = splice_managed_region(new_body, region, body)
                    except Exception as exc:
                        logger.warning(
                            "Skipping %s splice for %s: %s",
                            region, project.code, exc,
                        )
                if new_body != existing:
                    cp_path.write_text(new_body)
                    if cp_path not in files_written:
                        files_written.append(cp_path)

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
                    splice_regions=(
                        "agenda",
                        "sprint-facts-strip",
                        "slack-rollup",
                    ),
                )
                and master_path not in files_written
            ):
                files_written.append(master_path)

        # Phase 1.2 (v0.8.5) — weekly-cp.md strip regions. Only fires when
        # weekly-cp.md exists (it's scaffolded once per tenant; sync no
        # longer "never touches it" — it now maintains the three engine-
        # managed regions inside while preserving handwritten content).
        weekly_path = config.root / "weekly-cp.md"
        if parsed_files and weekly_path.exists():
            from cp_engine.aggregators import aggregate_tenant_strips
            from cp_engine.render import render_weekly_strip_bodies
            from cp_engine.sprints import (
                parse_themes_from_week_file,
                prior_sprint_week_iso as _prior_iso,
            )
            # Collect themes from current + prior week's _week.md (covers
            # the 2-week tenant_themes window without scanning the whole
            # sprints/ tree).
            themes: list = []
            for w in (week_iso, _prior_iso(sync_clock)):
                wpath = config.root / "sprints" / w / "_week.md"
                themes.extend(parse_themes_from_week_file(wpath))
            tenant_strips = aggregate_tenant_strips(
                tuple(parsed_files), tuple(themes), sync_clock.date()
            )
            bodies = render_weekly_strip_bodies(tenant_strips)
            existing_weekly = weekly_path.read_text()
            seeded_weekly = _ensure_weekly_strip_markers(existing_weekly)
            new_weekly = seeded_weekly
            for region, body in bodies.items():
                try:
                    new_weekly = splice_managed_region(new_weekly, region, body)
                except Exception as exc:
                    logger.warning(
                        "Skipping %s splice on weekly-cp.md: %s", region, exc,
                    )
            if new_weekly != existing_weekly:
                weekly_path.write_text(new_weekly)
                if weekly_path not in files_written:
                    files_written.append(weekly_path)

    # Exceptions README — engine-managed listing of session captures from
    # unregistered repos. Only render when the directory exists (creating it
    # would commit an empty README into every tenant; we want the file to
    # appear only after a real exception lands).
    exceptions_dir = config.root / "exceptions"
    if exceptions_dir.exists():
        readme_path = exceptions_dir / "README.md"
        new_readme = render_exceptions_readme(config.root, now=sync_clock)
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
    files_deactivated = (
        () if dry_run else _deactivate_stale_cps(config.root, live_dirs)
    )

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
    "slack-rollup",
    "exceptions-summary",
    "active-pipeline",
    "active-1p",
    "active-fpsf",
    "active-fpsf-initiatives",
    "active-canonic",
    "active-canonic-initiatives",
    "last-week-workload",
    "holding-subtable",
    "closed-recent",
)


def _prior_completed_week_monday(now: datetime) -> date:
    """Monday of the previous calendar week relative to `now`.

    Matches prep-planning's `today - timedelta(days=today.weekday() + 7)`.
    Used to surface last week's logged allocations (bug #11) — always a
    completed week, never the just-started current one. On Tue 2026-06-09
    (weekday 1) → 2026-06-01.
    """
    today = now.date()
    return today - timedelta(days=today.weekday() + 7)


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


def _would_change(
    path: Path,
    new_full_body: str,
    *,
    splice_regions: tuple[str, ...],
    cosmetic_regions: tuple[str, ...] = (),
) -> bool:
    """Return True iff `_write_if_changed` would write — without writing.

    Mirrors the change-detection in `_write_if_changed`: a missing file would
    be created; an existing file is compared region-by-region (ignoring
    cosmetic-only diffs and refreshing the provenance header) the same way.
    Backs `cp status` (bug #9).
    """
    if not path.exists():
        return True
    if not splice_regions:
        return path.read_text() != new_full_body

    existing = path.read_text()
    missing = [
        r for r in splice_regions
        if f"<!-- cp-engine:start {r} -->" not in existing
    ]
    if missing:
        return existing != new_full_body

    try:
        merged = existing
        for region in splice_regions:
            merged = splice_managed_region(
                merged, region, _extract_region(new_full_body, region)
            )
        merged = _refresh_provenance_header(merged, new_full_body)
    except Exception:  # noqa: BLE001 — match _write_if_changed's fallback
        return existing != new_full_body

    if merged == existing:
        return False
    if cosmetic_regions:
        try:
            comparison = existing
            for region in splice_regions:
                if region in cosmetic_regions:
                    continue
                comparison = splice_managed_region(
                    comparison, region, _extract_region(new_full_body, region)
                )
            if comparison == existing:
                return False
        except Exception:  # noqa: BLE001
            pass
    return True


def _write_if_changed(
    path: Path,
    new_full_body: str,
    *,
    splice_regions: tuple[str, ...],
    cosmetic_regions: tuple[str, ...] = (),
    dry_run: bool = False,
) -> bool:
    """Write `new_full_body` to `path`, splicing region-by-region if the file
    already exists and `splice_regions` is non-empty. Otherwise overwrite.

    Regions listed in `cosmetic_regions` (e.g. last-sync-timestamp) are
    ignored when deciding whether the file changed: if every other region
    matches, the write is skipped even though the cosmetic region differs.

    When `dry_run` is True, computes whether the file *would* change but
    performs no write — used by `cp status` (bug #9). Returns True iff the
    file changed (or would change, under dry_run).
    """
    if dry_run:
        return _would_change(
            path, new_full_body,
            splice_regions=splice_regions, cosmetic_regions=cosmetic_regions,
        )
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
        #
        # ``splice_managed_region`` raises ``MarkerDuplicated`` /
        # ``MarkerInverted`` when the existing body has hand-edits that
        # broke a region's marker pair (e.g. a leftover merge-conflict
        # marker or someone hand-pasted the same region twice). Without
        # a guard the entire sync_tenant run aborts before sprint files,
        # account CPs, and the weekly strip get written. Mirror the
        # per-strip-region pattern below by catching and falling back to
        # a full rewrite — the same content the splice would have
        # produced, just clobbering the broken region instead of trying
        # to surgically replace it.
        try:
            merged = existing
            for region in splice_regions:
                merged = splice_managed_region(
                    merged, region, _extract_region(new_full_body, region)
                )
            # Bug #10: the anchor `Provenance:` header lives outside every
            # engine-managed region, so the region splice above never touches
            # it — the file looked stale (old version/date) even right after a
            # clean regen. Refresh it from the freshly-rendered body so the
            # header always reports the running engine version + sync date.
            merged = _refresh_provenance_header(merged, new_full_body)
        except Exception as exc:  # noqa: BLE001 — sync continuation > region fidelity
            logger.warning(
                "%s splice failed (%s); falling back to full rewrite. "
                "Hand-edited content outside engine-managed regions may be "
                "lost — back up before re-running sync if that's a concern.",
                path, exc,
            )
            if existing == new_full_body:
                return False
            path.write_text(new_full_body)
            return True

        if merged == existing:
            return False

        # Did anything *non-cosmetic* change? Build a comparison candidate
        # that splices the new content for non-cosmetic regions only,
        # leaving cosmetic regions matching `existing`. If that equals
        # existing, the only diff is in cosmetic regions → skip write.
        if cosmetic_regions:
            try:
                comparison = existing
                for region in splice_regions:
                    if region in cosmetic_regions:
                        continue
                    comparison = splice_managed_region(
                        comparison, region, _extract_region(new_full_body, region)
                    )
                if comparison == existing:
                    return False
            except Exception as exc:  # noqa: BLE001
                # Cosmetic-only-diff check failed for the same reason
                # (broken markers); fall through to writing ``merged``.
                logger.debug(
                    "%s cosmetic-region splice failed (%s); writing merged body.",
                    path, exc,
                )

        path.write_text(merged)
        return True

    # File doesn't exist OR no regions to splice — full write.
    if path.exists() and path.read_text() == new_full_body:
        return False
    path.write_text(new_full_body)
    return True


def _refresh_provenance_header(merged: str, new_full_body: str) -> str:
    """Replace the anchor `Provenance: Version <NN> | <date>` line in `merged`
    with the one from the freshly-rendered `new_full_body`.

    The anchor header sits above all engine-managed regions, so the region
    splice never updates it; without this the version/date stamp stays frozen
    at whatever was last written (bug #10). Only the first match is touched —
    the anchor is the file's first `Provenance:` line. No-op if either side
    lacks the line.
    """
    pattern = re.compile(r"^Provenance: Version [^\n]*$", re.MULTILINE)
    fresh = pattern.search(new_full_body)
    if fresh is None:
        return merged
    return pattern.sub(fresh.group(0), merged, count=1)


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


_STRIP_REGIONS = (
    "inbound-strip",
    "recent-decisions-strip",
    "open-asks-strip",
    "stakeholders-strip",
)


def _ensure_strip_markers(body: str) -> str:
    """Inject empty markers for the four v0.8.5 project cp.md strip regions.

    Mirrors `_ensure_current_sprint_markers` but for the inbound-strip,
    recent-decisions-strip, open-asks-strip, and stakeholders-strip
    regions added in Phase 1.2. cp.md files scaffolded before v0.8.5
    don't have these markers; the splicer requires both start + end to
    exist, so we seed empty blocks first.

    Insertion anchor: immediately after the `tracked-issues` end marker
    (matches the template ordering — strips come after tracked-issues
    and before the handwritten Quick Resume). Falls back to end-of-file
    if no anchor found.

    Idempotent: regions already present are skipped.
    """
    anchor = "<!-- cp-engine:end tracked-issues -->"
    new_blocks = []
    for region in _STRIP_REGIONS:
        start = f"<!-- cp-engine:start {region} -->"
        if start in body:
            continue
        end = f"<!-- cp-engine:end {region} -->"
        new_blocks.append(f"{start}\n{end}\n")
    if not new_blocks:
        return body
    block = "\n".join(new_blocks) + "\n"
    if anchor in body:
        # Insert *after* the tracked-issues end marker so strips line up
        # with the template ordering.
        return body.replace(anchor, anchor + "\n\n" + block.rstrip("\n"), 1)
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n" + block


_QUICK_RESUME_START = "<!-- cp-engine:start quick-resume -->"
_QUICK_RESUME_END = "<!-- cp-engine:end quick-resume -->"


def _ensure_quick_resume_markers(body: str) -> str:
    """Wrap the project cp.md's `## Quick Resume` section in engine
    markers if absent.

    Pre-v0.11.0 the Quick Resume section was hand-written and had no
    markers. v0.11.0 makes the section engine-managed: auto-ingest
    writes `**Current work:**`, `**Next up:**`, `**Blockers:**` lines
    inside the region on every meeting that touches the project. The
    write functions require the markers to exist; this helper inserts
    them on the cutover sync.

    Behavior:
      - Body already has the start marker → return unchanged (idempotent).
      - Body has `## Quick Resume` heading → insert start marker on the
        line BEFORE the heading and end marker on the line BEFORE the
        next `## ` heading (or at end of file).
      - Body has no `## Quick Resume` heading → return unchanged
        (defensive; not all CPs have this section).

    The section's content (the four `**Label:**` lines) is preserved
    verbatim across the wrap. Subsequent auto-ingest plans rewrite
    individual lines inside the region.
    """
    if _QUICK_RESUME_START in body:
        return body
    heading = "## Quick Resume"
    heading_pos = body.find(heading)
    if heading_pos == -1:
        return body
    # Find the end of the section: next `## ` heading after the Quick
    # Resume heading, or end-of-file.
    search_from = heading_pos + len(heading)
    next_heading_match = re.search(r"^## ", body[search_from:], re.MULTILINE)
    if next_heading_match:
        section_end = search_from + next_heading_match.start()
        # Trim trailing blank lines just before the next heading so the
        # end marker sits flush with the section content.
        before_next = body[:section_end].rstrip("\n") + "\n"
        after_next = body[section_end:]
        return (
            before_next[:heading_pos]
            + f"{_QUICK_RESUME_START}\n"
            + before_next[heading_pos:]
            + f"{_QUICK_RESUME_END}\n\n"
            + after_next
        )
    # Quick Resume is the last section → end marker at end of file.
    trimmed = body.rstrip("\n") + "\n"
    return (
        trimmed[:heading_pos]
        + f"{_QUICK_RESUME_START}\n"
        + trimmed[heading_pos:]
        + f"{_QUICK_RESUME_END}\n"
    )


_WEEKLY_STRIP_REGIONS = (
    "themes-strip",
    "decisions-strip",
    "carry-forward-strip",
)


def _ensure_weekly_strip_markers(body: str) -> str:
    """Inject empty markers for the three weekly-cp.md strip regions.

    Existing tenants have weekly-cp.md without these markers. Sync needs
    to add them on first v0.8.5 sync without touching handwritten content.

    Insertion anchor: immediately before `## Active research` (the natural
    position per the new template — strips between the handwritten Quick
    Resume / Decisions and the handwritten Active research). Falls back
    to end-of-file if no anchor.

    Idempotent: regions already present are skipped.
    """
    new_blocks = []
    for region in _WEEKLY_STRIP_REGIONS:
        start = f"<!-- cp-engine:start {region} -->"
        if start in body:
            continue
        end = f"<!-- cp-engine:end {region} -->"
        new_blocks.append(f"{start}\n{end}\n")
    if not new_blocks:
        return body
    block = "\n".join(new_blocks) + "\n"
    anchor = "## Active research"
    if anchor in body:
        return body.replace(anchor, block + "\n" + anchor, 1)
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n" + block


# Field separator (US, ASCII 0x1F) between git-log fields. Subjects can
# contain tabs, pipes, colons, etc. — US is reserved for this purpose and
# never appears in real subjects.
_GIT_LOG_FIELD_SEP = "\x1f"

# Cap recent-commits list per project so a busy week (50+ commits) doesn't
# bloat the sprint file. Partners scanning the section don't need every
# commit; the top N most recent is plenty.
_SPRINT_RECENT_COMMITS_CAP = 20

# Cap commit subject length so each rendered bullet stays one line in a
# typical terminal/editor view. The full subject is in `git log` anyway.
_SPRINT_COMMIT_SUBJECT_CAP = 80


def _recent_commits_for_repo(
    repo_path: Path, *, sprint_start: date
) -> tuple[SprintCommit, ...]:
    """Walk `repo_path`'s git log for commits at-or-after `sprint_start`.

    Returns an empty tuple on any error (missing path, not a git repo,
    `git log` non-zero) so callers can skip silently. This is best-effort
    context for the sprint file — a broken clone must not break sync.
    """
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return ()
    fmt = _GIT_LOG_FIELD_SEP.join(("%h", "%s", "%an", "%ad"))
    # `git log --since=YYYY-MM-DD` (bare date) is interpreted as "now on
    # that date" — it silently filters out commits with timestamps
    # earlier in the same day. Appending an explicit `00:00:00` pins it
    # to midnight, so a commit dated `<sprint_start>T09:00` is included.
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--since={sprint_start.isoformat()} 00:00:00",
                f"--pretty=format:{fmt}",
                "--date=short",
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.debug("git log failed for %s: %s", repo_path, exc)
        return ()
    commits: list[SprintCommit] = []
    for line in result.stdout.splitlines():
        parts = line.split(_GIT_LOG_FIELD_SEP)
        if len(parts) != 4:
            continue
        sha_short, subject, author, when_short = parts
        if len(subject) > _SPRINT_COMMIT_SUBJECT_CAP:
            subject = subject[: _SPRINT_COMMIT_SUBJECT_CAP - 1].rstrip() + "…"
        commits.append(
            SprintCommit(
                sha_short=sha_short,
                subject=subject,
                author=author,
                when_short=when_short,
            )
        )
        if len(commits) >= _SPRINT_RECENT_COMMITS_CAP:
            break
    return tuple(commits)


def _collect_sprint_per_project_data(
    config: TenantConfig,
    projects,
    allocations,
    *,
    sprint_start: date,
) -> dict:
    """Per-project context fed into sprint-file scaffolding.

    Walks each project's local git clone (resolved via
    `config.local_repos[project.code]`) for commits at-or-after
    `sprint_start`, capped at `_SPRINT_RECENT_COMMITS_CAP`. Projects
    without a local clone or without commits in the window are simply
    absent from the result dict — `ensure_sprint_files_for_active_projects`
    falls back to `()` for missing keys.

    Other per-project keys (`sessions_this_week`, `last_session_*`,
    `open_issues`, `last_sprint_hours_line`) are not yet aggregated; the
    renderer applies safe defaults for the keys we omit. `allocations` is
    accepted for forward-compat with the upcoming hours-line work.
    """
    del allocations  # reserved for future hours-line aggregation
    out: dict[str, dict] = {}
    for project in projects:
        repo_path = config.local_repos.get(project.code)
        if repo_path is None:
            continue
        commits = _recent_commits_for_repo(
            repo_path, sprint_start=sprint_start
        )
        if not commits:
            continue
        out[project.code] = {"recent_commits": commits}
    return out


def _derive_summary(config: TenantConfig, project: ProjectState) -> str | None:
    """Read project CP from disk; return its one-line summary or None.

    The CP may not exist yet (new project), or may live under a different
    slug than the freshly-computed one (name drift in MC-2 not yet
    reconciled by the rename in sync_tenant). Locate it by code via
    `_find_project_dir`. Returns None when the CP doesn't exist or has
    no human-written content yet.
    """
    from cp_engine.summary import derive_from_project_cp

    scope = account_scope_for(project)
    existing = _find_project_dir(scope_root(config.root, scope), project.code)
    if existing is None:
        return None
    return derive_from_project_cp(existing / "cp.md")


_SCOPE_DIRS: tuple[str, ...] = ("1p", "firstpersonsf", "canonic")

# Scopes whose project dirs live under an extra per-account layer
# (1p/<company>/<dir_slug>/). Non-client scopes (firstpersonsf, canonic)
# already nest by self-company at the scope level, so they have no extra
# layer.
_ACCOUNT_NESTED_SCOPES: frozenset[str] = frozenset({"1p"})


def _project_parent_dirs(tenant_root: Path, scope: str) -> list[Path]:
    """Directories that directly contain project working dirs for `scope`.

    For account-nested scopes (`1p`), that's every per-account subdir
    (`1p/google/`, `1p/infoblox/`, ...). The `inactive/` subdir of the
    scope is skipped — it's a parking lot for whole inactive accounts,
    not a project parent. (Per-project inactives live one level deeper,
    inside their account: `1p/<company>/inactive/<dir>/`.)

    For non-nested scopes (`firstpersonsf`, `canonic`), the scope root
    itself is the project parent — projects live directly under it.

    Returns an empty list when the scope dir doesn't exist yet.
    """
    scope_dir = scope_root(tenant_root, scope)
    if not scope_dir.exists():
        return []
    if scope not in _ACCOUNT_NESTED_SCOPES:
        return [scope_dir]
    parents: list[Path] = []
    for child in scope_dir.iterdir():
        if not child.is_dir() or child.name == INACTIVE_DIR_NAME:
            continue
        parents.append(child)
    return parents


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
    # `live_dirs` is keyed by the FULL account scope ("1p/google" for
    # clients, "firstpersonsf"/"canonic" for self-companies) — the same
    # path returned by account_scope_for(). Group live codes by that key
    # so each project parent dir below can match against its own slice.
    live_codes_by_account_scope: dict[str, set[str]] = {}
    for scope, code in live_dirs:
        live_codes_by_account_scope.setdefault(scope, set()).add(code)

    for scope in _SCOPE_DIRS:
        for parent in _project_parent_dirs(tenant_root, scope):
            # The account-scope key for this parent: "1p/google" for a
            # client per-company subdir; "firstpersonsf" / "canonic" for
            # a self-company scope root.
            account_scope = str(parent.relative_to(tenant_root))
            live_codes = live_codes_by_account_scope.get(account_scope, set())
            # Per-project inactive bin sits next to the live dirs — for
            # clients, that's `1p/<company>/inactive/`; for self-company
            # scopes, `firstpersonsf/inactive/` (unchanged).
            inactive_bin = parent / INACTIVE_DIR_NAME

            for path in parent.iterdir():
                if not path.is_dir():
                    continue
                if path.name == INACTIVE_DIR_NAME:
                    continue

                # A dir is "live" if its name matches a known live project
                # code in either form (bare or `<code>-<slug>`).
                if any(
                    path.name == code or path.name.startswith(f"{code}-")
                    for code in live_codes
                ):
                    continue

                # Stale. Move the whole dir.
                inactive_bin.mkdir(exist_ok=True)
                target = inactive_bin / path.name
                if target.exists():
                    # v0.1.2 collision rule: never silently overwrite.
                    # Skip and warn — a human can resolve by renaming
                    # or merging the duplicate.
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
