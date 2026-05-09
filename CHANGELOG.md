# Changelog

All notable changes to `cp-engine` are recorded here. The package follows [semver](https://semver.org/).

Tenants pin to a minor version (`engine = "~= 0.1"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration notes.

## v0.4.2 — 2026-05-08

### Fixed

- **`actions/sync` composite action runs on Node.js 24.** Bumped `actions/checkout` from v4 to v6 and `actions/setup-python` from v5 to v6. GitHub deprecates Node.js 20 on its runners June 2 2026; v6 of both actions runs on Node.js 24 and is the future-proof pin.

## v0.4.1 — 2026-05-08

### Fixed

- **`cp capture-session` now enforces the cp tenant's engine pin.** Previously the command bypassed `config.load()` (it doesn't need the merged config to do its job), which meant a stale cp-engine binary would silently produce wrong-format output against a newer-pinned tenant. The check now runs after destination resolution but before any writes — a stale install fails loud with `EngineVersionMismatch` and no half-formed session file is created.
- **`EngineVersionMismatch` message includes upgrade instructions.** Lists the system-wide (`uv tool install --force --from <repo> cp-engine`) and project-local (`uv pip install -e <repo>`) options. Prompted by a real failure: a `.venv/bin/cp` got stuck at v0.1.0 across many engine releases, and the slash command silently dispatched to it.

### Added

- **`config.enforce_engine_version_for_tenant(tenant_root)`** — public lightweight helper that reads just `[engine].version` from `.cp-engine.toml` and runs the existing constraint check. Used by `capture_session()`; available for any future command that needs to validate the install without loading the full merged config.

## v0.4.0 — 2026-05-08

### Added

- **Two-way local linkage between cp tenants and source repos.** A new `[local-repos]` section in `.cp-engine.local.toml` maps GitHub repo names to local clone paths (per-machine, gitignored). The engine reads this map to enrich `_repo.md` and to discover where each tracked repo lives on disk.
- **`cp link-local` CLI command.** Reads `[local-repos]`, walks the cp tenant tree to locate matching `_repo.md` files, writes `<source-repo>/.cp-link` containing the absolute path of the corresponding cp working dir, and adds `.cp-link` to `<source-repo>/.git/info/exclude`. Idempotent. Validates that each configured path is a git repo whose `origin` remote matches the entry name.
- **`cp capture-session` CLI command.** Writes a session summary back to the cp tree from inside a source repo. Resolves the cp working dir via `<source-repo>/.cp-link`, self-heals if the linked path is stale (re-resolves from the source repo's git remote against the cp tenant's `_repo.md` files), writes `<wd>/sessions/<YYYY-MM-DD>-<HHMM>-<user>.md` with a counter-suffix on collision, updates `cp.md`'s `**Last session:**` line without touching other Quick Resume content, then commits and pushes the cp clone. Falls back to writing `<cp-tenant>/exceptions/` when the source repo isn't a tracked project.
- **`/cp-summarize` Claude Code slash command** at `plugin/commands/cp-summarize.md`. Thin wrapper over `cp capture-session` that drafts the session summary using a loose template (Session header, What we did, Decisions, Open threads, Next), writes it to a temp file, and shells out to the CLI. Distributed via the marketplace manifest at `.claude-plugin/marketplace.json`: install with `/plugin marketplace add FirstPersonSF/cp-engine` then `/plugin install cp-engine@cp-engine`.
- **Engine-managed exceptions README.** When `<cp-tenant>/exceptions/` exists, sync regenerates `exceptions/README.md` with a splice region (`exceptions-list`) listing the last 30 days of exception files (newest first, parsed from filename or mtime).
- **Master-cp `exceptions-summary` line.** When the tenant has 1+ exception in the last 7 days, master-cp.md surfaces a one-line "**Exceptions:** {N} this week" pointer. Region is always present so the splicer can find it; body is empty when the count is zero.
- **`_repo.md` enriched with local clone path.** When `[local-repos]` has an entry for a project's repo name, the rendered file surfaces `**Local clone:** <absolute path>` and points Claude at the local clone for activity questions. Without the entry, the v0.3.3 shape is preserved.
- **CLAUDE.md template gains a "Local-link traversal" section** explaining both directions of the link (cp → source via `_repo.md`, source → cp via `.cp-link`) and pointing at `/cp-summarize`.

### Changed

- **`render_master_cp` accepts `exceptions_count: int = 0`** for the new surface line. `count_exceptions_in_window` (also new) does the lookup; sync.py wires it.
- **`render_repo_md` accepts `local_clone_path: Path | None = None`** for the enriched output. Sync.py looks up via `config.local_repos.get(project.repo_name)`.
- **`TenantConfig` gains `local_repos: Mapping[str, Path]`** (defaults to empty MappingProxyType). Tenants without `[local-repos]` continue to work unchanged.

### Distribution

The Claude Code plugin lives at `/plugin/` inside this repo and is excluded from both the wheel and the sdist via `[tool.hatch.build.targets.sdist].exclude`. Install via `/plugin add github:FirstPersonSF/cp-engine?path=plugin`; updates pull from the same repo.

### Notes

- The slash command's design is **MC-2-free at runtime** — both the linked path resolution and the self-heal walk read filesystem only (cp tenant's committed `.cp-engine.toml` and rendered `_repo.md` files).
- `[local-repos]` is keyed by GitHub repo name, separate from the existing `[repos]` table (which keys by project code). This lets users link non-project repos like `cp-engine` itself or unregistered libraries.

## v0.2.4 — 2026-05-08

### Added

- **Sprint allocations from MC-2.** Sync now reads `public.sprint_allocations` for last week (Monday-starting) and renders in two places:
  - **Per-row allocation line** appended to each non-internal project row in the active sections, formatted as `_Last week: Tony 4h, Marcello 8h (12h total)._`. Skips entirely if zero hours that week. Renders via `<br>` inside the Project cell so it stays in markdown table.
  - **Per-person workload rollup** as a new section at the bottom of master-cp.md. Includes ALL allocations (engagements + internal admin) split into two columns. Surfaces who's spending time on what without polluting per-row data.
- **`MC2Backend.read_allocations(config, week_start)`** — new method returning `WeeklyAllocations` (per-project + per-person rollup).
- **New state shapes:** `PersonHours`, `ProjectAllocation`, `PersonRollup`, `WeeklyAllocations`.
- New engine-managed region: `last-week-workload`.

### Fixed

- **`splice_managed_region` produces matching output for empty bodies.** Previously, splicing an empty body produced `start\n\nend` (two newlines) while a fresh full-write produced `start\nend` (one newline). After splice, second-sync no-op detection failed for any region whose body could be empty (e.g. workload section with no allocations). Now both paths produce `start\nend` for empty bodies.

### Notes

"This week" is intentionally NOT surfaced — it's incomplete on Monday morning when sync runs and gets entered live during the partners' review session itself. Only last week is shown, providing a clean "what just happened" view.

## v0.2.3 — 2026-05-08

### Added

- **Auto-summary from project CP content.** Sync now derives each project's master-CP one-line summary by reading the project CP's hand-written sections — first preference: `## Quick Resume`'s "Current work:" line; fallback: first non-placeholder paragraph in `## Current Work`. Pure Python heuristic, no LLM call. Returns None when content is still placeholder; the column activates the moment a human writes content. ≤120 char cap enforced.
- **`summary.derive_from_project_cp(file_path)`** — new public API that drives the auto-summary.
- **1P split into Pipeline + Active subtables.** Pipeline section first (Status=Deal, sorted by stage progression Inquiry → Negotiation → Contract), Active second (Status=Open). Each section's columns are optimized for its question — Pipeline shows Stage; Active drops Status and Stage as redundant.

### Changed

- New engine-managed region `active-pipeline` in master-cp.md.
- The `active-1p` region now contains only Open engagements (was: all client work). Existing tenants will see schema-evolution recovery fire on first v0.2.3 sync (the existing master-cp.md gets full-rewritten because `active-pipeline` doesn't exist in v0.2.2-rendered files).

## v0.2.2 — 2026-05-08

### Added

- **`cp refresh-pristine`** CLI command. Re-scaffolds project CPs that still contain the placeholder marker (i.e. never been hand-edited) using the current template. Edited CPs are NEVER touched. Use this once after upgrading the engine when you want the template improvements to land in already-scaffolded files. `--dry-run` shows what would change without writing.
- **`project-facts` engine-managed region** in project CPs. Sits near the top, surfaces Code / Status / Owner / Stage / Budget / Client (engagement) or Code / Status / Owner / GitHub / Description (repo) plus Last touched. Means humans opening a project CP see the metadata without needing the master CP loaded.

### Fixed

- **Doubled H1 in project CPs.** Previously `# GGL-5168 GGL 5168 Activation — Project CP` (code prepended to name that already contained the same prefix). Now `# GGL 5168 Activation — Project CP`. Repo CPs were even worse: `# MC-2 mc-2 — Project CP` → now `# mc-2 — Project CP`.
- **Engagement-shaped sections on repo CPs.** Repos got a "Stakeholders" section that doesn't fit. Repos now get "Committers" instead; engagements keep "Stakeholders".
- **Stale Provenance line on existing CPs.** Now updated when `cp refresh-pristine` runs against pristine files.

### Notes for tenants on v0.2.x

After upgrading the engine, run `cp refresh-pristine` once to refresh the template shape across pristine project CPs. Engaged-with CPs (anyone has filled in Quick Resume, Decisions, etc.) are left alone permanently — that's hand-written content the engine never touches.

## v0.2.1 — 2026-05-08

### Fixed

- **Schema-evolution recovery in `_write_if_changed`.** When the existing master-cp.md is missing one or more expected splice regions (typically because the engine version bumped and added new regions like v0.1's `active-table` → v0.2's `active-1p`/`active-fpsf`/`active-canonic`), sync now full-rewrites instead of raising `MarkerMissing`. Logs a warning so the recovery is visible. Caught when upgrading the cp tenant from v0.1.4 → v0.2.0 — the existing master-cp.md had v0.1 markers that didn't match v0.2's region names.

## v0.2.0 — 2026-05-08

### Added

- **`sync_mc2` reads two source streams.** Engagement projects (`public.projects`) AND standalone repos (`public.repos WHERE project_id IS NULL`). Both unify into `tuple[ProjectState, ...]` returned from `read_projects()`. Repos linked to engagements are intentionally excluded — their info enriches the parent engagement's project CP, not the master index.
- **`ProjectState` gains `source` + `company_kind` discriminators** plus engagement-only fields (`deal_stage`, `budget`) and repo-only fields (`github_org`, `repo_name`, `description`).
- **Master CP renders three sections** grouped by `companies.kind`:
  - 1P — client engagements, engagement-shape table (Code | Project | Status | Stage | Owner | Budget | Last touched | Summary | CP)
  - First Person — self-fpsf repos, repo-shape table (Repo | Status | Owner | Description | Last touched | GitHub | CP)
  - Canonic — self-canonic repos, same repo-shape
- Engine-managed regions renamed: `active-table` → `active-1p`, plus new `active-fpsf` and `active-canonic` regions.

### Changed

- The "tenant" model is conceptually collapsed: one CP repo serves all three audiences (1P / FPSF / Canonic), filtered at render time rather than at repo level. The `[tenant]` block in `.cp-engine.toml` still exists but is generic.
- Smoke-tested against the real MC-2 with 21 engagements + 5 standalone repos rendered correctly.

### Migration notes for tenant repos pinned `~= 0.1`

This is a minor bump (0.1.x → 0.2.0) and tenants pinned to `~= 0.1` will NOT auto-upgrade. Tenants should:
1. Bump pin to `~= 0.2` in `.cp-engine.toml` `[engine].version`
2. Bump GitHub Action workflow to `pip install "git+https://github.com/FirstPersonSF/cp-engine.git@v0.2.0"`
3. On next sync, the master CP regenerates with three sections. Existing project CPs are untouched.

## v0.1.4 — 2026-05-08

### Fixed

- `sync` no longer writes `master-cp.md` when the only change between syncs is the `last-sync-timestamp` region. Previously the hourly cron produced a one-line commit every run even when MC-2 hadn't changed — 24 noise commits per day. The engine now compares non-cosmetic regions only; timestamp refreshes piggyback on real changes, never stand alone. When MC-2 *has* changed, the new sync clock is written alongside the real diff (no stale-timestamp risk).

### Changed

- `_write_if_changed` (internal) gains a `cosmetic_regions` parameter for regions whose contents differ every sync by definition. Currently only `last-sync-timestamp` is cosmetic; the parameter is generic so future timestamp-shaped regions can opt in.

## v0.1.3 — 2026-05-07

### Fixed

- `config.load` no longer raises `LocalConfigMissing` when committed `.cp-engine.toml` has no `[[projects]]` entries — there's nothing to map, so the local file is treated as optional. This unblocks CI runners (gitignored local file) and mc-2-backend tenants that read their project list from MC-2 directly rather than from committed config. Surfaced when standing up cp-1p's hourly Action.

## v0.1.2 — 2026-05-07

### Fixed

- `sync` now archives project CPs whose source-of-truth project has dropped out of view (archived in MC-2, deleted, or flipped to `is_internal=true`). The CP file moves from `projects/<code>.md` to `projects/archived/<code>.md`. Hand-edited content is preserved (move/rename, not regenerate). If `projects/archived/<code>.md` already exists, the engine logs a warning and skips rather than overwriting. ([cp-1p#1] surfaced this when 30+ legacy projects were bulk-archived in MC-2.)

### Changed

- `SyncResult` gains a `files_archived: tuple[Path, ...]` field. `no_op` is now true iff both `files_written` and `files_archived` are empty. CLI `cp sync` reports archived files in the same output block as written files.

## v0.1.1 — 2026-05-07

### Fixed

- `sync` no longer scaffolds project CPs for `is_internal=true` projects. The renderer was already correctly excluding them from `master-cp.md`, but the orchestrator was creating CP files for them — leaving inconsistent state on disk vs. what the rendered master CP showed. Surfaced while standing up cp-1p.

## v0.1.0 — 2026-05-07

First release. The framework for First Person and Canonic CP corpora.

### What works

- `cp_engine.config`: `.cp-engine.toml` + `.cp-engine.local.toml` merger with fail-loud semantics, engine_version constraint enforcement (via `packaging.SpecifierSet`), symlink resolution for Dropbox-backed working dirs, drift warnings for orphan local entries
- `cp_engine.render`: full renderers (`master-cp`, `weekly-cp`, `project-cp`, `CLAUDE.md`) plus `splice_managed_region` with HTML-comment markers and impossible-to-misuse splice semantics (raises on missing/duplicate/inverted markers)
- `cp_engine.sync`: orchestrator with Backend `Protocol`; `mc-2` backend reads MC-2's `projects` table via Supabase using company-prefixed `<prefix>-<number>` canonical IDs (legacy rows without `company_id` fall back to `<number>`)
- `cp_engine.init`: interactive `cp init` writes `.cp-engine.local.toml`, strict path validation with up to 3 retries, tomlkit round-trip preserves user comments
- `cp_engine.status`: canonical vocabulary `Deal | Open | Holding | Closed | Archived` with per-status active flag map; mirror of `mc-2/backend/src/status.py`
- `cp_engine.modes`: the four reading-mode contracts (index-only / single-project / sprint / weekly review) as data
- `cp_engine.summary`: ≤120-char enforcement for master-CP one-liners
- CLI: `cp init`, `cp sync`, `cp render`
- Templates: `master-cp.md.j2`, `weekly-cp.md.j2`, `project-cp.md.j2`, `CLAUDE.md.j2`
- GitHub Action skeleton at `actions/sync/action.yml`

### Tested

- 87 unit tests
- End-to-end smoke test against the real MC-2 database (57 projects)

### Spec

- Canonical: `docs/specs/cp-engine-spec-v02.md`
- v01 + amendments + architecture-change preserved at `docs/specs/history/`

### Deferred to v0.2

- `github-issues` backend (for `cp-canonic`)
- `cp status` (read-only diff preview)
- CI drift check between `mc-2/status.ts`, `mc-2/status.py`, `cp_engine/status.py`
- Per-issue `tracked-issues` content in project CPs
