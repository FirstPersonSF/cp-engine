# Changelog

All notable changes to `cp-engine` are recorded here. The package follows [semver](https://semver.org/).

Tenants pin to a minor version (`engine = "~= 0.1"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration notes.

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
