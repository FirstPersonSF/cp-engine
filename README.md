---
Project: Context Protocol Engine
Provenance: Version 02 | 2026-05-07
Filename: README.md
Author: Drew + Tony + Claude
---

# Context Protocol Engine (`cp-engine`)

A versioned framework for First Person and Canonic CP corpora. The engine is one installable Python package — spec, sync logic, renderers, CLI, GitHub Action. Each tenant (`cp-1p`, `cp-firstpersonsf`, `cp-canonic`) is a thin GitHub repo that depends on this package and holds its own master CP, weekly CP, project CPs, and tenant config.

Framework updates flow one direction: cut a release here, bump the pin in each tenant, every tenant gets the change. Tenants never fork engine code.

## Spec

The canonical spec is at [`docs/specs/cp-engine-spec-v02.md`](docs/specs/cp-engine-spec-v02.md). v01 + amendments + the v02 architecture-change proposal are preserved in [`docs/specs/history/`](docs/specs/history/) for traceability.

## Status

**v0.1.0 — pre-release.** Package skeleton, CLI stubs, templates, and the canonical status module are in place. Sync logic, renderers, and the `cp init` interactive flow land in v0.1.

## Layout

```
cp-engine/
├── pyproject.toml                 ← installable package
├── src/cp_engine/                 ← Python module
│   ├── status.py                  ← canonical vocab + active subset (REAL)
│   ├── config.py                  ← .cp-engine.toml + .local.toml merger (stub)
│   ├── modes.py                   ← mode 1-4 contracts (REAL — used by render.py)
│   ├── sync.py                    ← MC-2 + GitHub-Issues backends (stub)
│   ├── render.py                  ← Jinja renderers (stub)
│   ├── summary.py                 ← one-line regen with ≤120-char cap (REAL helpers)
│   └── cli.py                     ← `cp` entry point (stub subcommands)
├── templates/                     ← Jinja2
│   ├── master-cp.md.j2
│   ├── weekly-cp.md.j2
│   ├── project-cp.md.j2
│   └── CLAUDE.md.j2
├── actions/sync/                  ← reusable GitHub Action (stub)
├── tests/                         ← pytest
└── docs/specs/
    ├── cp-engine-spec-v02.md
    └── history/
```

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

Drift detection is a CI check (planned for v0.2).

## Versioning

Semver. Tenants pin to a minor version (`engine = "~= 0.3"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration. See `CHANGELOG.md`.
