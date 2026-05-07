# Changelog

All notable changes to `cp-engine` are recorded here. The package follows [semver](https://semver.org/).

Tenants pin to a minor version (`engine = "~= 0.3"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration notes.

## Unreleased

### Added

- `pyproject.toml` and the `cp_engine` package skeleton
- Canonical `cp_engine.status` module (vocabulary + `is_active_status` helper) — REAL implementation
- `cp_engine.modes` — the four reading-mode contracts as data
- `cp_engine.summary.enforce_summary_cap` — ≤120-char single-sentence enforcement for master-CP one-liners
- Jinja2 templates: `master-cp.md.j2`, `weekly-cp.md.j2`, `project-cp.md.j2`, `CLAUDE.md.j2`
- `actions/sync/action.yml` — reusable GitHub Action stub
- `cp` CLI scaffold via `click` (stubs for `init`, `sync`, `render`, `status`)
- `tests/test_status.py` — vocabulary smoke tests

### Spec

- v02 spec at `docs/specs/cp-engine-spec-v02.md`
- v01 + amendments + architecture-change preserved at `docs/specs/history/`

## v0.1.0 — _planned_

First versioned release. Will include:

- `cp_engine.config` — full `.cp-engine.toml` + `.local.toml` merger with fail-loud semantics
- `cp_engine.sync` — both backends (`mc-2` via Supabase API; `github-issues` via PyGithub)
- `cp_engine.render` — actual rendering + `splice_managed_region`
- `cp init` interactive
- `cp sync` and `cp render` end-to-end against a real tenant repo
