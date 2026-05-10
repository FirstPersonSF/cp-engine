# Changelog

All notable changes to `cp-engine` are recorded here. The package follows [semver](https://semver.org/).

Tenants pin to a minor version (`engine = "~= 0.1"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration notes.

## v0.8.0 — 2026-05-10

### Added

- **Sprint files.** `cp sync` now writes a per-project sprint markdown file at `sprints/<YYYY-W##>/<project-code>.md` for every active, non-internal project. Each file scaffolds the partners' weekly review with both engine-managed sections (sprint facts, where-it-stands, carry-forward from the prior sprint) and hand-written sections (client communication, dependencies & risks, this sprint's plan, 4–8 week horizon, meeting notes). Hand-written content is preserved across re-syncs; only engine-managed regions refresh.
- **Master agenda + facts strip.** `master-cp.md` gains a top-of-page Agenda rollup (escalated risks, stale client asks > 7 days, horizon decisions due within 2 sprints) and a sprint-totals facts strip (total hours, per-person hours, active count, stale asks, escalated risks, decisions due, prior sprint). Both regions aggregate across all parsed sprint files.
- **Project CP "Current sprint" block.** Each project's `cp.md` gains a `current-sprint` engine-managed region linking to the active sprint file with a top-3 asks/risks summary. Legacy CPs without the marker get the markers seeded automatically.
- **Sprint column in master active tables.** Each active-project row in master CP now links to that project's sprint file alongside its CP link.
- **Auto-generated section summaries.** Each active section in master CP gets a one-line italic summary like _Three deals in flight_, _Four engagements in delivery_.
- **Sprint-index README.** `sprints/<YYYY-W##>/README.md` is engine-rendered each sync with a launch table: project · allocation · asks · risks · decisions due.
- **Mode 4 deepening contract.** `CLAUDE.md` documents that `deepen from transcript` writes meeting notes, decisions, new asks, outbound drafts, and risk updates into the sprint file (not project `cp.md`) when one exists for the current week.
- **`risk_categories` config.** Tenants can override the default risk-category vocabulary (`contract`, `pricing`, `people`, `technical`, `scope`, `timeline`) via a `[risk_categories]` block in `.cp-engine.toml`.
- **Per-project `contacts`.** `[[projects]]` blocks now accept a `contacts = [{name, role}, ...]` array surfaced in the sprint file's client-communication section.
- **`cp parse-sprint <path> [--json]`.** New CLI subcommand emits a parsed `SprintFile` as one-line summary or full JSON for downstream consumers (mc-2 integration, debugging, future tooling).

### Out of scope (deferred)

- mc-2 capacity overlay, web-rendered viewer, and any direct read path from mc-2 into the cp tenant repo. The sprint markdown is the source of truth; mc-2 integration is a separate exploration tracked in `docs/plans/2026-05-10-sprint-files-design.md`.
- Inline editing UI. Sprint files remain markdown-edited.
- Per-project sprint-hour budgets / project-level capacity.

### Tenant impact

- Pin `engine = "~= 0.8"` to pick up sprint-files generation.
- Existing `cp.md` files get a new `current-sprint` engine region inserted on next sync; hand-written content outside engine markers is preserved.
- Run a sync inside the sprint window (any day Mon–Sun of the upcoming sprint week) to populate `sprints/<current-week>/` for the first time. Re-syncs are idempotent and only refresh engine regions.

### Verification

- 286 tests passing across `tests/test_state.py`, `tests/test_sprints.py`, `tests/test_sync.py`, `tests/test_render.py`, `tests/test_config.py`, `tests/test_cli_parse_sprint.py`.
- Round-trip test: `render_sprint_scaffold` → `parse_sprint_file` confirms parser/template parity.
- Idempotency tests: re-running `ensure_sprint_file` on identical input produces identical bytes; sync no-op semantics preserved (engine regions only re-write when content changes).

See `docs/plans/2026-05-10-sprint-files-design.md` for design rationale and `docs/plans/2026-05-10-sprint-files-plan.md` for the phased implementation plan.

## v0.7.4 — 2026-05-09

### Fixed

- **Sprint allocation window now anchors on the upcoming sprint-planning Monday.** Previously, `_last_week_monday()` always returned the Monday of the calendar week BEFORE today's calendar week — meaning when sync ran on Saturday May 9, master-cp.md showed allocations for Apr 27 - May 3, even though Monday's sprint planning meeting (May 11) is meant to review the week of May 4 - May 10. Fix: anchor on the upcoming Monday (or today if today IS Monday) and look back 7 days. Window now matches what sprint planning will actually discuss.

### Behavior change

- **On Tue-Sun:** window is `[next_Monday - 7, next_Monday - 1]` (the week ending the day before Monday's sprint planning).
- **On Monday:** window is `[today - 7, today - 1]` (the week just ended, which is what the meeting reviews).
- **Before the fix:** all days returned the previous calendar week — too early by 7 days for Tue-Sat sprint prep.

### Verification

8 new tests in `test_sync.py` covering: Monday anchor, Tue-Sat anchor on next Monday, weekend prep, consistency Sat→Sun→Mon, post-meeting flip Mon→Tue, date-or-datetime input handling.

### Tenant impact

Runner picks up v0.7.4 on next sync via the existing `~= 0.3` pin. master-cp.md will start showing the corrected window on the next cron tick.

## v0.7.3 — 2026-05-09

### Changed

- **Content-only mode (`--working-dir`) now sweeps the entire working dir on commit.** Previously, `cp capture-session --working-dir <wd>` staged only the session file + `cp.md` — same narrow scope as source-repo mode. That left synthesis docs, transcripts, and hand-written notes the user added during the session uncommitted, which forced Claude to ask "should I commit X?" friction on every capture. Now the engine `git add`s everything trackable inside the working dir (filtered by `.gitignore`, so binaries and `.DS_Store` are auto-excluded).
- **Source-repo mode is unchanged.** The narrow scope (v0.4.3) still applies for source-code projects: only the session file + `cp.md` are committed, since the source repo is where hand-written work lives, not the cp working dir.
- **`/cp-summarize` slash command** explicitly tells Claude not to ask the user about untracked files in the working dir for Mode B — the engine has already decided. Just relay the CLI's "Also committed N other file(s)" output to the user as part of the summary.

### Added

- **`CaptureResult.extra_files_committed: tuple[Path, ...]`** — populated in content-only mode with the list of files swept up beyond the session file + cp.md. The CLI prints this as a one-line "Also committed N other file(s)" plus a relative-path list.
- **`cp_engine.capture_session._trackable_paths_under(working_dir, tenant_root)`** — internal helper that runs `git ls-files --others --exclude-standard --modified -- <working-dir>/` to enumerate files git would track if `git add`-ed. Filters by `.gitignore` for free.
- **4 new tests** for the sweep behavior: text content under the working dir gets committed, binaries are excluded by `.gitignore`, the sweep does NOT cross project boundaries (other projects' dirty state is left alone), and source-repo mode keeps its narrow scope (regression guard against accidentally broadening it).

### Why

For 1P engagements without a source code repo (`cp/1p/<engagement>/`), the working dir IS where all the work lives — synthesis docs, meeting transcripts, reference materials, the `cp.md` itself. Asking "should I commit X?" on every session is friction that makes no sense: the answer is always "yes, commit the text content, exclude the binaries." The `.gitignore` already enforces the binary exclusion; the new sweep just stops asking.

### Notes

- Smoke-tested against `cp/1p/ibx-5153-ai-campaign/` (real 1P engagement with `Reference Materials/` full of `.pptx` and `.docx` files plus one `.md`): the `.md` got swept up, the binaries didn't, the working dir is now clean.
- The behavior is scoped to one project's working dir at a time. Dirty state in other projects' working dirs is NOT pulled in (verified by test).

## v0.7.2 — 2026-05-09

### Added

- **`cp capture-session --working-dir <path>`** — new mode for **content-only projects** (1P engagements without a separate source code repo). Skips all source-repo + `.cp-link` resolution and writes the session summary directly to `<working-dir>/sessions/`. The cp tenant root is found by walking up for `.cp-engine.toml`, so `--cp-tenant` isn't needed. Mutually exclusive with `--source-repo`.
- **`cp_engine.capture_session.capture_session_in_working_dir`** — public Python API for the same. New `WorkingDirNotInTenant` exception when the supplied path isn't inside any cp tenant clone.
- **5 new tests** covering the content-only path: writes to `sessions/`, updates `cp.md`'s Last session line, rejects working dirs outside any tenant, rejects non-existent paths, and (end-to-end) commits exactly the session file + cp.md without sweeping unrelated dirty state into the commit.

### Changed

- **`/cp-summarize` slash command** auto-routes to the new mode. Detects three cases by comparing `pwd` to `git rev-parse --show-toplevel`:
  - **Mode A** — `pwd` is inside a source code repo (the common case). Original behavior; uses `--source-repo`.
  - **Mode B** — `pwd` is inside a cp tenant working dir (content-only project). New behavior; uses `--working-dir`.
  - **Mode C** — `pwd` is the cp tenant root itself. Tells the user to `cd` into a project dir.

### Why

Content-only projects (e.g. `1p/ibx-5153-ai-campaign/`) have no source code repo to anchor `.cp-link` against. The original `/cp-summarize` flow assumed every project has a code repo, so capturing a session from a content-only working dir required hand-driving the file write, the cp.md update, and the git commit. Now `/cp-summarize` Just Works regardless of project type.

### Notes

- Refactored the existing `capture_session()` to extract a shared `_write_to_working_dir()` helper. Behavior-preserving — both modes write the session file, update cp.md, and commit + push the same way.
- The `_session_commit_does_not_include_unrelated_uncommitted_state` discipline (v0.4.3) carries through unchanged: only the new session file and (if updated) the project's `cp.md` are staged.

## v0.7.1 — 2026-05-09

### Changed

- **`<scope>/archived/` renamed to `<scope>/inactive/`.** Projects often flip back to active (engagements paused and resumed, internal flag toggled, status changed) — "inactive" captures that better than "archived" (which suggests a one-way trip). The reactivation logic (already present since v0.4) is now visible in the directory name. Spread of changes:
  - `state.ARCHIVED_DIR_NAME` → `INACTIVE_DIR_NAME`; `archived_root()` → `inactive_root()`; `archived_dir()` → `inactive_dir()`.
  - `sync._archive_stale_cps` → `_deactivate_stale_cps`; `SyncResult.files_archived` → `files_deactivated`.
  - `cp sync` CLI output: "archived X" → "deactivated X".
  - `_repo.md` discovery in `link_local` skips both `inactive/` and the legacy `archived/` (so unmigrated tenants still work).
  - `cp migrate-projects-flat` now reads pre-v0.7 `<scope>/projects/archived/` as input and writes `<scope>/inactive/` as output (rename + flatten in one step).

### Cleared documentation debt

- **`actions/sync` GitHub Action** rewritten to mirror what `cp/.github/workflows/sync.yml` does inline: install `packaging`, run inline pin resolver, install cp-engine at the resolved tag, run `cp sync`, commit + push. The previous version had a hardcoded `pip install cp-engine` (no PyPI), referenced a `cp-sync` branch that doesn't exist, and had a `TODO: implement commit + push` left in. Now a future second tenant can `uses: FirstPersonSF/cp-engine/actions/sync@v0.7.1` instead of copy-pasting the inline resolver.
- **Reading-mode descriptions** in `cp_engine.modes` and the rendered `CLAUDE.md` updated from v0.2-era `projects/<code>.md` paths to v0.7's `<scope>/<dir_slug>/cp.md`. Mode 4's "every active project CP" glob is now `<scope>/*/cp.md` (so it naturally excludes `inactive/`).
- **Spec v02** had 11 stale `projects/<code>.md` references in the modes section, master-CP section, and architecture diagram. Bulk-rewrote to the v0.7 path shape. The v01 amendments and v02-architecture-change docs in `docs/specs/history/` are intentionally not touched (they're historical).

### Tenant migration

- **Existing tenants with no archived projects** (e.g. `cp` today): runner picks up v0.7.1 on the next sync via the existing `~= 0.3` pin; no manual action needed.
- **Existing tenants with archived projects in pre-v0.7 layout**: re-run `cp migrate-projects-flat`. The command now reads `<scope>/projects/archived/<dir>/` (legacy name) and writes `<scope>/inactive/<dir>/`.
- **Existing tenants in v0.7 layout with archived projects**: no automated path. Manual `git mv <scope>/archived <scope>/inactive` per scope. (Out of scope to automate for v0.7.1 — affects no current tenant.)

## v0.7.0 — 2026-05-09

### Changed

- **Working-tree layout drops the redundant `projects/` segment.** Project working dirs now live at `<tenant>/<scope>/<dir_slug>/` instead of `<tenant>/<scope>/projects/<dir_slug>/`. Archived projects move from `<scope>/projects/archived/<dir>/` to `<scope>/archived/<dir>/`. The old `projects/` segment was inherited from when the spec called for separate `cp-1p` / `cp-firstpersonsf` / `cp-canonic` tenants (where the inner `projects/` was the only child of the tenant root). With all three scopes consolidated into one `cp` tenant, the segment was empty container — every `firstpersonsf/projects/` had only `firstpersonsf/projects/cp-engine/` (etc.) inside it.
- **Path-construction is centralized in `cp_engine.state`.** New helpers `scope_root()`, `working_dir()`, `archived_root()`, `archived_dir()` are the single source of truth for working-tree paths. Sync, render, link-local, capture-session, and project-context all route through them.
- **`master-cp.md` table links** now point at `<scope>/<dir>/cp.md` instead of `<scope>/projects/<dir>/cp.md`. Re-rendered automatically on next `cp sync`.

### Added

- **`cp migrate-projects-flat`** — one-shot migration command. For each scope, `git mv`s every `<scope>/projects/<dir>` to `<scope>/<dir>` and `<scope>/projects/archived/` to `<scope>/archived/`. Removes the now-empty `projects/` parent. Rewrites `.cp-link` files in linked source repos so they point at the new paths. Idempotent: re-running on an already-migrated tree is a no-op. Refuses to run on a dirty working tree. Detects collisions (destination already exists) and aborts loudly rather than silently merging.
- **`cp_engine.migrate_flat`** module with full unit-test coverage (10 tests): clean-tree pre-flight, idempotency, single-scope move, multi-scope move, archive subdir handling, mixed already-migrated/not-yet-migrated state, collision detection (live + archive), git-history preservation under `git log --follow`.

### Tenant migration

Existing tenants must run `cp migrate-projects-flat` once to convert their working tree:

```sh
cd /path/to/cp-tenant
cp migrate-projects-flat
git status   # review the staged moves
git commit -m "v0.7 layout: drop projects/ segment from working dirs"
git push
```

The `[engine].version` constraint in `.cp-engine.toml` continues to admit v0.7 if it's `~= 0.3` (or any constraint that allows 0.7.x). The runner picks up v0.7 on next sync via `cp resolve-engine-pin` (added in v0.6); no workflow file edit needed.

### Why now

Single-user / single-tenant on this machine: cheap to migrate, easier to do before any second user adopts the system. The empty `projects/` directories were visible noise in every `ls`, and the path shape `firstpersonsf/projects/cp-engine/` made readers think there must be a sibling alongside `projects/` (there wasn't). v0.7 makes the layout match the conceptual model: scope dir → project dir, no intermediate.

## v0.6.0 — 2026-05-09

### Added

- **`scripts/release.py`** is the canonical way to cut a release. Reads the new version from the command line; bumps `pyproject.toml`, `src/cp_engine/__init__.py`, `plugin/plugin.json`, `.claude-plugin/marketplace.json` atomically; runs `pytest` and `python -m build`; commits, tags `v<X>`, and pushes. Pre-flight checks (clean working tree, on `main`, CHANGELOG section drafted, tag doesn't already exist) all pass before any file is touched. `--dry-run` runs the checks without modifying anything.
- **`SessionStart` hook in the plugin** auto-installs the matching `cp` CLI when the plugin and CLI versions drift. Runs on every Claude Code session in a project where the plugin is loaded; fast (~50ms) when versions match, runs `uv tool install --force --from git+...@v<plugin-version>` when they don't. Per spec v03: prints errors but never blocks session start — the next `/cp-summarize` will fail loud with `EngineVersionMismatch` if the install fails, which is the existing safety net.
- **`cp resolve-engine-pin`** CLI subcommand resolves a tenant's `[engine].version` constraint to the highest matching git tag on the engine repo. `--format` controls output (`tag`, `pip-spec`, `json`). Backed by a new `cp_engine.pin_resolver` module with full unit tests.
- **`cp_engine.pin_resolver`** module — `read_constraint`, `list_remote_tags`, `resolve`, `resolve_for_tenant`. Pure functions; no PyPI, no caching, no auth. Uses `git ls-remote --tags` to enumerate available versions.

### Changed

- **`src/cp_engine/__init__.py`'s `__version__`** is now bumped by the release script. Reading it from `importlib.metadata` was considered (single-source-of-truth via package metadata) but rejected because Python 3.14 silently returns `None` for missing/broken metadata, which would collide with `config.py`'s version-check logic.

### Tenant migration

- Tenants should update `.github/workflows/sync.yml` to install cp-engine via inline pin resolution instead of a hardcoded git tag. The `cp` tenant got this update as part of v0.6.0 release prep. Until updated, runners continue to install whatever tag the workflow file names — no breakage, just no longer self-updating.
- The plugin's new SessionStart hook activates on next `/plugin update cp-engine`. No action required; users get the auto-install behavior automatically once they've updated the plugin once.

### Why

Before v0.6: a release required manually bumping four version strings (pyproject, plugin.json, marketplace.json, CHANGELOG), tagging, asking every user to run two install commands (`/plugin update` AND `uv tool install --force`), and editing each tenant's `sync.yml`. Four places where versions could drift, no automated check that they hadn't. After v0.6: one script for release, one command (`/plugin update`) for users, automatic propagation to runners via the tenant pin. See `docs/specs/cp-engine-spec-v03-version-distribution.md` for the full reasoning.

## v0.5.2 — 2026-05-09

### Added

- **`/cp-context` slash command** is now functional (was a stub through v0.5.1). Run from inside a cp working dir, prints a 7-day timeline merging git commits from the linked source repo's local clone with session captures from the working dir's `sessions/` directory. Claude reads the timeline and synthesizes "what's been happening on this project?" without the user having to ask for raw output.
- **`cp project-context` CLI command** powers the slash command but is also useful standalone. Defaults to a 7-day window; `--days <N>` overrides. `--user <name>` picks a specific `[local-repos.<user>]` entry; without it the command picks the first user whose configured path exists on this machine (the natural "running on Drew's machine, find Drew's clone" heuristic).
- **`cp_engine.project_context`** module — `project_context(working_dir, user, days, now)` returns a `ContextResult` with `commits: tuple[CommitEntry, ...]` and `sessions: tuple[SessionEntry, ...]`. Pure-Python plumbing; the slash command's markdown is a thin wrapper.

### Notes

- When no local clone is reachable on this machine, `cp project-context` returns sessions only (commits empty) rather than erroring — gives the user *some* context even if the git history isn't available locally.
- One-liner extraction for session entries reuses the same `### What we did` heuristic that drives `cp.md` Last session line updates, so summaries stay consistent across surfaces.

## v0.5.1 — 2026-05-08

### Fixed

- **`cp capture-session` auto-recovers from push rejection.** Previously, when a `[cp-sync]` cron commit landed on origin between captures, `git push` was rejected as non-fast-forward and the command silently reported "(push skipped or failed)" while leaving the session commit only on the local clone. Now: detects the rejection, runs `git pull --rebase`, retries the push once. Real push failures (network, auth, hook rejection, rebase conflict) raise `PushFailed` with the underlying stderr.
- **CLI output distinguishes pushed / rebased+pushed / skipped / failed.** "(push skipped or failed)" was misleading because it conflated three different states. Now: pushed-and-clean → "Committed X and pushed."; rebased then pushed → "Committed X, rebased on top of upstream, and pushed."; commit=False or push=False → "Committed X (push skipped)."; failure → raises `PushFailed` (no longer reaches the success-output branch).

### Added

- **`CaptureResult.push_rebased: bool`** — True when the first push was rejected and the auto-rebase + retry succeeded. Lets callers distinguish a clean push from one that needed recovery.
- **`PushFailed` exception** — raised by `capture_session()` when push fails for a reason auto-rebase can't recover from. Includes the underlying git stderr in the message so the user knows what to fix.

## v0.5.0 — 2026-05-08

### Added

- **Committed multi-user `[local-repos.<user>]` schema in `.cp-engine.toml`.** Each tenant member declares their own per-machine clone paths in a section keyed by their name (`[local-repos.drew]`, `[local-repos.tony]`, etc.). The runner reads these (unlike the gitignored `.cp-engine.local.toml`'s `[local-repos]`), so the rendered `_repo.md` includes one `**Local clone (User):**` line per user who has the repo. Free-form user keys; no validation against a known users list.
- **`TenantConfig.local_repos_by_user`** — `Mapping[str, Mapping[str, str]]` exposing the new committed map. Outer key is user; inner is repo-name → path string. Paths are NOT resolved (they don't exist on the runner; they're for display).

### Changed

- **`render_repo_md` signature: `local_clone_path` → `local_clones_by_user`.** Old kwarg accepted a single `Path | None` for the current machine's clone path; new kwarg accepts a `dict[str, str] | None` mapping user → path. Sync now passes the per-user map for the current repo, derived from `config.local_repos_by_user`.
- **`_repo.md` template** renders one line per user: `**Local clone (Drew):** /...` then `**Local clone (Tony):** /...` etc., followed by `(per .cp-engine.toml [local-repos.<user>])`. The "for each user listed above..." prose replaces v0.4's single-clone wording.
- **CLAUDE.md template** updated to explain the two-tier model: committed `[local-repos.<user>]` for rendering and team-wide visibility, gitignored `[local-repos]` for `cp link-local` and `cp capture-session` self-healing.

### Why

v0.4's enrichment relied on the gitignored `[local-repos]`, which the GitHub Action runner can't see. The cron sync would always produce `_repo.md` files without local clone paths, overwriting any local-only enrichment on the next push. v0.5 separates "committed paths for display" from "per-machine paths for behavior" so both work.

### Migration

Existing tenants without `[local-repos.<user>]` sections render `_repo.md` in the v0.3.3 shape (no clone paths) — no breakage. To opt in, add a section per user to your tenant's `.cp-engine.toml` and run a sync.

## v0.4.3 — 2026-05-08

### Fixed

- **`[session]` commits no longer sweep up unrelated uncommitted state.** Previously `cp capture-session` ran `git add .` from the cp tenant root, which opportunistically committed any pre-existing dirty files in the tree alongside the actual session capture. Now it stages only the files this capture wrote: the new session file and (if updated) the project's `cp.md`. Pre-existing dirty state stays uncommitted for the human to handle.

  Caught in real use: a `/cp-summarize` run picked up 11 files of pre-existing sync output in addition to the intended 2-file session change, producing a `[session] cp-engine: ...` commit whose contents were mostly unrelated to the session.

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
