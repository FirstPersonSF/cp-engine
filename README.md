---
Project: Context Protocol Engine
Provenance: Spec v02 + v03 (version distribution) | 2026-05-09
Filename: README.md
Author: Drew + Tony + Claude
---

# Context Protocol Engine (`cp-engine`)

A versioned framework for First Person and Canonic CP corpora. The engine is one installable Python package — spec, sync logic, renderers, CLI, GitHub Action. Each tenant (currently just `cp`; the spec leaves room for `cp-firstpersonsf` and `cp-canonic` to split out later) is a thin GitHub repo that depends on this package and holds its own master CP, weekly CP, project CPs, and tenant config.

Framework updates flow one direction: cut a release here, bump the pin in each tenant, every tenant gets the change. Tenants never fork engine code.

## Spec

The canonical spec is at [`docs/specs/cp-engine-spec-v02.md`](docs/specs/cp-engine-spec-v02.md). v01 + amendments + the v02 architecture-change proposal are preserved in [`docs/specs/history/`](docs/specs/history/) for traceability.

## Status

**v0.7.0.** Sync, renderers, working-tree layout (flattened in v0.7 — drops the redundant `projects/` segment), sprint allocations, session capture (`/cp-summarize`), project-context timeline (`/cp-context`), and the v0.6 release-distribution overhaul (scripted releases, plugin-owned CLI auto-install, tenant-pin-driven runner) are all live. See `CHANGELOG.md` for the version history and `docs/specs/cp-engine-spec-v03-version-distribution.md` for the v0.6 design.

## Capturing sessions back to cp

cp-engine ships a Claude Code plugin at `/plugin/` so a developer can wrap a session in a source repo (e.g. `mc-2`, `cp-engine`, `storyos`) and have a summary land in the corresponding cp working directory automatically.

### One-time per-machine setup

1. Clone the cp tenant (e.g. `cp`) and run `cp init` to populate `.cp-engine.local.toml`.
2. Add a `[local-repos]` table to that file, mapping each source repo's GitHub name to its local clone path:

   ```toml
   [local-repos]
   "mc-2"      = "/Users/you/Documents/Python/mc-2"
   "cp-engine" = "/Users/you/Documents/Python/cp-engine"
   "storyos"   = "/Users/you/Documents/Python/storyos"
   ```

3. From inside the cp tenant clone, run `cp link-local`. This writes a `.cp-link` file into each configured source repo (containing the absolute path of its cp working dir) and adds `.cp-link` to that repo's `.git/info/exclude`.
4. Install the slash command plugin via the cp-engine marketplace:

   ```
   /plugin marketplace add FirstPersonSF/cp-engine
   /plugin install cp-engine@cp-engine
   ```

   (The marketplace and the plugin both happen to be named `cp-engine` —
   that's the marketplace name from `.claude-plugin/marketplace.json`.)

   Since v0.6, the plugin ships a `SessionStart` hook that auto-installs
   the matching `cp` CLI via `uv tool install` when versions drift. So a
   first-time install pulls both the slash commands and the CLI in one
   step; subsequent `/plugin update cp-engine` runs propagate to the
   CLI automatically. (If `uv` isn't on your PATH, the hook prints a
   one-line install command instead of failing the session.)

### Daily use

Inside any tracked source repo, finish your session with:

```
/cp-summarize
```

The command drafts a session summary, writes it to `<cp-working-dir>/sessions/<YYYY-MM-DD>-<HHMM>-<user>.md`, updates that project's `cp.md` "Last session:" line, then commits and pushes the cp clone.

If the current repo isn't tracked in the cp tenant, the summary lands in `<cp-tenant>/exceptions/` and gets surfaced on the next `cp sync` via the engine-managed `exceptions/README.md` plus a one-line "Exceptions ({N} this week)" pointer in `master-cp.md`.

## Layout

### This repo (the engine)

```
cp-engine/
├── pyproject.toml                ← installable package
├── scripts/
│   └── release.py                ← canonical release flow (v0.6)
├── src/cp_engine/
│   ├── status.py                 ← status vocab + active-subset flags
│   ├── state.py                  ← shared dataclasses + path helpers
│   ├── modes.py                  ← reading-mode contracts
│   ├── config.py                 ← .cp-engine.toml + .local.toml loader
│   ├── sync.py                   ← MC-2 + GitHub-Issues backends, archive sweep
│   ├── sync_mc2.py               ← MC-2-specific backend implementation
│   ├── render.py                 ← Jinja renderers + managed-region splicer
│   ├── summary.py                ← one-line summary regeneration
│   ├── init.py                   ← interactive `.cp-engine.local.toml` writer
│   ├── refresh.py                ← refresh-pristine project CPs
│   ├── migrate.py                ← v0.2 → v0.3 migration (legacy)
│   ├── migrate_flat.py           ← v0.6 → v0.7 layout migration (drops projects/)
│   ├── link_local.py             ← write .cp-link files into source repos
│   ├── capture_session.py        ← /cp-summarize backend
│   ├── project_context.py        ← /cp-context backend (commits + sessions timeline)
│   ├── pin_resolver.py           ← resolve [engine].version → highest matching git tag (v0.6)
│   ├── cli.py                    ← `cp` entry point
│   └── templates/                ← Jinja2 (master-cp, weekly-cp, project-cp,
│                                   CLAUDE.md, _repo.md, _dropbox.md)
├── plugin/                       ← Claude Code plugin
│   ├── plugin.json
│   ├── commands/                 ← /cp-summarize, /cp-context
│   └── hooks/                    ← SessionStart auto-install hook (v0.6)
├── actions/sync/                 ← reusable GitHub Action
├── tests/                        ← pytest (228 tests as of v0.7)
└── docs/specs/
    ├── cp-engine-spec-v02.md     ← canonical spec
    ├── cp-engine-spec-v03-version-distribution.md  ← v0.6 release-flow design
    └── history/                  ← v01 + amendments + v02 architecture proposal
```

### Tenant working tree (post-v0.7)

```
<tenant root>/                    ← e.g. ~/Documents/Python/cp
├── .cp-engine.toml               ← committed config (engine pin, project list)
├── .cp-engine.local.toml         ← gitignored, per-machine
├── master-cp.md
├── weekly-cp.md
├── CLAUDE.md
├── 1p/<dir_slug>/cp.md           ← client engagements
├── firstpersonsf/<dir_slug>/cp.md  ← First Person internal tooling
├── canonic/<dir_slug>/cp.md      ← Canonic
└── exceptions/                   ← summaries from untracked source repos
```

Pre-v0.7 layouts had an extra `projects/` segment under each scope; `cp migrate-projects-flat` moves them to the v0.7 shape in place.

## Local development

```bash
uv pip install -e ".[dev]"
pytest
```

## Three sources of truth for status vocab

The status enum (`Deal | Open | Holding | Closed | Archived`) and the active-subset flag map live in three places that **must stay in sync**:

- `mc-2/frontend/src/lib/status.ts` (UI)
- `mc-2/backend/src/status.py` (MC-2 backend, `active_jobs_sync.py`)
- `cp-engine/src/cp_engine/status.py` (this repo — used by `cp-canonic` whose sync backend is GitHub Issues, not MC-2)

Drift detection is a CI check (still planned; not yet wired up).

## Versioning

Semver. Tenants pin to a minor version (`engine = "~= 0.3"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration. See `CHANGELOG.md`.
