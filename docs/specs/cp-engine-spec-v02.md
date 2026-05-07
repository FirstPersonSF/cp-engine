---
Project: Context Protocol Engine
Provenance: Version 02 | 2026-05-07
Filename: cp-engine-spec-v02.md
Author: Drew + Tony + Claude
---

# Context Protocol Engine — Architecture Spec v02

> A versioned framework plus per-tenant corpora for First Person and Canonic. The framework (`cp-engine`) is one installable Python package — spec, sync logic, renderers, CLI, GitHub Action. Each tenant (`cp-1p`, `cp-firstpersonsf`, `cp-canonic`) is a thin GitHub repo holding only its own master CP, weekly CP, project CPs, and tenant config. Framework updates flow one direction via versioned releases — no copy-paste.

This is the canonical spec. v01 + amendments + the v02 architecture-change proposal are folded in here and superseded; their original artifacts are preserved in `docs/specs/history/` for traceability.

---

## Related Documents

- **Bootstrap (v2):** `/_claude/bootstraps/context-protocol-bootstrap-v02.md` — the operational discipline this spec inherits (word-count thresholds, archive rotation, research-output discipline, three-tier memory model).
- **Spec-writing guide:** `/_claude/personal/tasker/docs/bootstraps/spec-writing-guide.md`.
- **Working precursor:** `/_claude/1P/ggl-repos/` — the proof-of-concept that demonstrates the multi-level CP pattern in practice.
- **Sprint protocol:** `/_claude/bootstraps/clickup-sprint-protocol-bootstrap-v01.md` — Canonic's sprint-tracking mechanism, which this spec sits *above* as a summary layer.
- **Historical specs:** `docs/specs/history/cp-engine-spec-v01.md`, `cp-engine-spec-v01-amendments.md`, `cp-engine-spec-v02-architecture-change.md` — superseded by this file.

---

## What changed from v01

Three accepted v01 amendments and one v02 architectural change are folded into this document. Anyone trying to follow the lineage:

| Source | Status | Where in v02 |
|---|---|---|
| v01 base spec | Superseded by v02; in `history/` | Whole document |
| v01 Amendment 1 — status vocab + active subset + `isActiveStatus` helper | Locked, folded | §3 status vocabulary |
| v01 Amendment 2 — four loading modes + master/weekly CP split | Locked, folded; Tony's mode-switching clarification answered | §6 loading modes |
| v01 Amendment 3 — auto-generated one-line summaries | Locked, folded; Tony's length cap answered | §7.3 deepening pass |
| v02 architecture-change proposal — framework + tenants | Locked, folded | §2 framework/tenant split |

---

## Pre-Flight Questions (resolved before v02 close)

All resolved in conversation 2026-05-06 → 2026-05-07. Audit trail kept here.

### Q1 — MC-2 status vocabulary and active subset

**Resolution.** Final vocabulary is `Deal | Open | Holding | Closed | Archived`. Active subset = `Deal ∪ Open` AND `is_internal=false`. `is_internal` is an orthogonal boolean (lifecycle vs. ownership are two different axes). `Internal` is **not** a status value — it was a v01-draft conflation that the amendment dropped.

The vocabulary lives in code, not config: `frontend/src/lib/status.ts` (canonical TS) and `backend/src/status.py` (Python mirror) in the MC-2 repo. The `cp-engine` framework ships its own copy in `cp_engine.status` for renderers and sync that don't depend on MC-2 directly. **All three must stay in sync.** See §3.

### Q2 — Single repo or split

**Resolution.** Split: one framework package (`cp-engine`) plus N tenant repos. Three tenants on day one: `cp-1p`, `cp-firstpersonsf`, `cp-canonic`. See §2.

### Q3 — Reading discipline / loading modes

**Resolution.** Four explicit loading modes (index-only / single-project / sprint / weekly review), enforced by a generated `CLAUDE.md` in each tenant. The gatekeeper rule forbids auto-globbing outside *connected tenant repos* (not single repo — Brandon and Marcello routinely co-load `cp-1p` + `cp-canonic`). See §6.

### Q4 — Mode-switching mechanics (Tony's clarification on Amendment 2)

**Resolution.** Mode is a property of the session at any moment — it can change. Two switching paths:

- **Explicit verb required to expand.** A session in mode 1 (index-only) escalates only by user phrase. Recognized phrases: `also load <code>` (additive — moves to mode 2 keeping mode 1 reads), `switch to <code>` (replace — mode 2 only), `run weekly review` (full mode 4), `update sprint` (mode 3).
- **`run weekly review` mid-session loads on top, doesn't discard.** A user already in mode 2 (working on `ggl-5168`) who then types `run weekly review` gets mode 4 layered over the existing context. The just-loaded `ggl-5168` stays in context; weekly mode adds the rest. Use `wrap up` to close the weekly-review block; the prior mode persists if the session continues.

This applies inside a single tenant. Cross-tenant escalation requires explicitly opening another tenant repo (Claude Desktop GitHub MCP) or cloning it locally — not a phrase. See §6.

### Q5 — One-line summary length cap (Tony's clarification on Amendment 3)

**Resolution.** Hard cap: **≤120 characters, single sentence, no markdown.** The renderer enforces it; over-length is truncated with `…` and logged. Rationale per Tony: master-CP-as-menu only works if rows stay scannable.

### Q6 — Per-machine repo paths

**Resolution.** Two-file config: committed `.cp-engine.toml` defines projects by GitHub coordinates; gitignored `.cp-engine.local.toml` defines per-machine paths. Engine fails loudly if a project is in the committed file but missing from local. See §5.

### Q7 — Sync identity and target

**Resolution.** A bot identity (`cp-engine-bot[bot]`) commits to a `cp-sync` branch with auto-merge to `main` if checks pass. See §4.5.

### Q8 — Cross-org GitHub auth

**Resolution.** GitHub App preferred (per-org install scoping, no PAT-rotation drift). PAT acceptable as a v0.1 placeholder; migrate to App before v1.0. See §4.6.

### Q9 — `mc-2` placement inside `cp-firstpersonsf`

**Resolution.** Regular project (one CP file at `projects/mc-2.md`) for v02. Promote to an `initiatives/` sub-structure later if a second initiative-sized thing appears in the same tenant. YAGNI applied. See §2.4.

---

## Builder's Contract

### Hard Constraints (must be followed)

- **MC-2 is the sole writer of project status.** Nobody — human or AI — edits the Status column of `master-cp.md` by hand or via Claude. Status changes go through MC-2's project edit page, which fires the sync. Hand-edits inside engine-managed regions are reverted by the next sync. (See §4.)
- **AI must always reference projects by code + human-readable name.** `GGL-5176 London Safety Video Phase I`, not `GGL-5176` alone. (See §3.4.)
- **CP filenames carry only the code.** `ggl-5176.md`, not `ggl-5176-london-safety-video.md`. The human-readable name lives in the file's H1 title and the master-CP table. (See §3.3.)
- **Zoom transcripts are never committed.** Transcripts pasted into the deepening pass are ephemeral session input only. Only the *extracted CP edits* are committed. (See §7.3.)
- **Word-count discipline per bootstrap v2.** Every CP file is checked on wrap-up: >2,500 words triggers a duplication audit; >3,500 words forces archive rotation before commit. (See §3.5.)
- **File anchors required on every `.md` file.** Per global `CLAUDE.md`. (See §3.4.)
- **Two-pass partners'-review flow is mandatory, not optional.** Live pass (lightweight tags during the meeting) → deepening pass (post-meeting transcript ingestion) → wrap-up commit. Skipping the deepening pass is forbidden because it's the step that captures the discussion's substance. (See §7.)
- **No status field cross-write.** MC-2 writes status to GitHub; nothing in this system writes status back to MC-2. Tenant repos are downstream consumers, not peers. (See §4.4.)
- **Tenants never fork engine code.** A tenant needing new behavior opens an issue against `cp-engine`, ships a versioned change, bumps the pin. Copy-paste is prohibited. (See §2.2.)
- **CLAUDE.md is generated, never hand-edited.** A `CLAUDE.md` change is a `cp-engine` change. (See §2.5.)
- **Engine-managed regions are sacred.** The engine writes only between HTML-comment markers (`<!-- cp-engine:start … -->` / `<!-- cp-engine:end … -->`). Outside the markers is human territory and never touched by automation. (See §4.3.)
- **No auto-globbing outside connected tenant repos.** The generated `CLAUDE.md` enforces mode discipline. Reading project-source repos requires explicit opt-in by phrase or path. (See §6.4.)

### Builder's Implementation Decisions (guidance, not mandates)

- **GitHub App vs. PAT.** App preferred. PAT acceptable for v0.1 sync; must be migrated before v1.0. (See §4.6.)
- **Diff-presentation UI for the deepening pass.** Recommended: Claude emits a structured diff in chat (per-CP, per-section), human approves before commit. (See §7.3.)
- **Branching strategy for tenant repos.** Recommended: push directly to `main` for routine CP edits. Branch + PR for structural changes (new project, schema migration, automation tweak). The `cp-sync` branch is auto-managed by the engine — humans don't push to it. (See §8.3.)
- **Master CP active-subset display.** Recommended: a single Active table, a `<details>`-collapsed Holding subtable, a `<details>`-collapsed Closed-recent (30 days) list. Builder may rearrange if a different layout proves easier to scan. (See §3.1.)
- **Per-CP "Active research" section.** Recommended for any project that's commissioned Perplexity / Claude Research artifacts. Optional otherwise. (See §3.4.)

---

## §1 System Overview

### 1.1 The four kinds of artifact

```
   THE FRAMEWORK                     THE TENANTS                       THE CONSUMERS
   ─────────────────                 ───────────────                   ─────────────────
   cp-engine (one repo)              cp-1p, cp-firstpersonsf,          Humans (4 founders) +
                                     cp-canonic (three repos)           Claude sessions

   Owns:                             Owns:                             Read:
   - The spec (this file)            - .cp-engine.toml (config)        - master-cp.md (mode 1)
   - cp_engine.* Python modules      - master-cp.md (rendered)         - one project CP (mode 2)
   - Jinja templates                 - weekly-cp.md (rendered)         - sprint-cp.md (mode 3)
   - The cp CLI                      - projects/*.md (mixed)            - everything (mode 4)
   - Reusable GitHub Action          - CLAUDE.md (generated)
   - Versioned releases              - .github/workflows/sync.yml      Write:
                                                                       - hand-written sections
   Versioned: yes (semver).          Versioned: tracks engine pin.      of project CPs
   Forks: prohibited (it's a         Forks: prohibited (always
   package, not a template).         consume the engine via pin).      MC-2 writes via sync only.
```

### 1.2 The four principles

1. **MC-2 is the project registry for `cp-1p` and `cp-firstpersonsf`.** The set of FPSF projects, their codes, names, and statuses is canonical there. Master CPs are *projections* of MC-2's state. Canonic projects are configured in `cp-canonic/.cp-engine.toml` directly — there's no MC-2-equivalent registry for those.
2. **The tenant repo is the narrative layer.** What we discussed, what we decided, what's blocked, what's next — captured in CP files. MC-2 doesn't try to replicate it.
3. **All collaborators read and write tenant repos.** No tool asymmetry: Marcello and Brandon use Claude Desktop with GitHub MCP, which gives them write access on parity with Tony and Drew's Claude Code workflow. Visibility is per-tenant via GitHub repo permissions (§2.4).
4. **The framework changes via versioned release, never via tenant fork.** Bug in the master-CP renderer? Fix it in `cp-engine`, cut a release, bump the pin in each tenant. Every tenant sees the fix on its next sync.

### 1.3 Memory tier mapping

Per bootstrap v2's three-tier model, applied per tenant:

| Tier | What lives here | Update cadence |
|------|-----------------|----------------|
| **Hot** | `CLAUDE.md` at the tenant root (routing, mode rules, status enum, gatekeeper rule). Always loaded. | Generated by engine; changes via `cp render`. |
| **Warm** | `master-cp.md` + `weekly-cp.md` + each project CP (`projects/<code>.md`). Loaded per session via mode rules. | Updated every partners' review + ad-hoc during the week. Word-count gated. |
| **Cold** | `archive/` directories within the tenant. Loaded on demand only. | Created by archive rotation when a CP exceeds 3,500 words. |

### 1.4 The week's rhythm

```
Weekly partners' review  →  Driver opens Claude Code in the relevant tenant repo
(typically Monday;          (cp-1p for FPSF jobs review; cp-firstpersonsf for
shifts with conflicts)      internal-tooling review). Types "run weekly review"
                            → mode 4 loads. Live pass (light tags during meeting).
                            Meeting ends. Transcript posted.
                            "deepen from transcript" → diff for review.
                            "wrap up" → word-count checks, push.

Between reviews          →  Anyone can update any project's CP at any time. Mid-cycle
                            edits don't trigger automation. MC-2 status changes auto-
                            sync to the relevant tenant's master-cp.md hourly via the
                            tenant's GitHub Action (or on-demand via cp sync).

Next review              →  Cycle repeats. Diff against last review's commit shows
                            what shifted (and what was silent).
```

**Cadence over date discipline.** The partners' review is *weekly* — the rhythm matters, not the exact day. Tony's update from v01 carries forward.

**Distinct from the StoryOS / Mission Control sprint protocol.** Drew operates a separate sprint protocol (`Canonic-OS/storyos` v03+) on a Tuesday-open / Monday-close cadence. That ritual is distinct from the partners' review — different audience, different altitude, different artifacts. The two systems coexist on different rhythms.

---

## §2 The Framework / Tenant Split

### 2.1 What `cp-engine` owns

A single GitHub repo (the existing `FirstPersonSF/context-protocol`, renamed/extracted to `cp-engine` per §10), versioned with semver, installable as a Python package.

```
cp-engine/
├── pyproject.toml                       ← installable: pip install cp-engine
├── README.md
├── CHANGELOG.md
├── docs/
│   └── specs/
│       ├── cp-engine-spec-v02.md        ← this file
│       └── history/                      ← v01, amendments, architecture change
├── src/cp_engine/
│   ├── __init__.py
│   ├── status.py                         ← MC_STATUSES, MC_STATUS_ACTIVE, is_active_status
│   ├── config.py                         ← merges .cp-engine.toml + .cp-engine.local.toml
│   ├── modes.py                          ← mode 1-4 loader contracts
│   ├── sync.py                           ← reads GH Issues, writes engine-managed regions
│   ├── render.py                         ← master-cp / weekly-cp / project-cp / CLAUDE.md
│   └── summary.py                        ← one-line summary regen (≤120 chars)
├── cli/cp                                ← cp init / sync / render / status
├── templates/                            ← Jinja2
│   ├── master-cp.md.j2
│   ├── weekly-cp.md.j2
│   ├── project-cp.md.j2
│   └── CLAUDE.md.j2
├── actions/sync/                         ← reusable GitHub Action: cp-engine/sync@v0
└── tests/
```

### 2.2 What a tenant repo owns

```
cp-<tenant>/
├── .cp-engine.toml                       ← committed: tenant config + project list
├── .cp-engine.local.toml                 ← gitignored: per-machine repo paths
├── .gitignore                            ← includes .cp-engine.local.toml
├── .github/workflows/sync.yml            ← invokes cp-engine/sync@v0 on cron
├── CLAUDE.md                             ← generated, do not hand-edit
├── master-cp.md                          ← generated regions + hand-written
├── weekly-cp.md                          ← generated regions + hand-written
├── projects/
│   ├── ggl-5168.md                       ← project CPs
│   ├── ggl-5176.md
│   └── ...
├── archive/                              ← word-count-rotation outputs
└── canonic/                              ← only in cp-canonic; sprint summary lives here
    ├── sprint-cp.md
    └── archive/
```

The framework is a dependency. Tenants never fork it.

### 2.3 Critical rule: tenants never fork engine code

If `cp-canonic` needs new behavior `cp-engine` doesn't support, the path is:
1. Open issue against `cp-engine` describing the use case
2. Ship a versioned change in `cp-engine`
3. Bump the pin in `cp-canonic/.cp-engine.toml`'s `[engine]` block
4. Re-run `cp render`; new behavior takes effect

Copy-pasting a script into the tenant repo is prohibited. The moment any tenant has its own divergent renderer or sync logic, the multi-tenant model collapses.

### 2.4 The three day-one tenants

| Tenant | What it tracks | Visible to |
|---|---|---|
| `cp-1p` | First Person *jobs* (client engagements). Project codes like `ggl-5168`, `ibx-5153`, `pph-5182`. MC-2 is the project registry. | Drew, Tony, Brandon, Marcello |
| `cp-firstpersonsf` | First Person *internal* tooling and components. Includes `mc-2` itself, plus `1p-webflow-builder`, `1p-component-library`, `1p-dashboard`, `hex-brand-zoner`, etc. MC-2 is the project registry. | Drew, Tony |
| `cp-canonic` | Canonic / personal repos (`storyos`, `unf-forge`, `canonic-website`, etc.). No MC-2 dependency — projects configured in `.cp-engine.toml` directly. | Drew, Tony, Brandon, Marcello |

`mc-2` lives inside `cp-firstpersonsf` as a normal project (one CP file). Promote to an `initiatives/` sub-structure later if a second initiative-sized thing appears.

Visibility is enforced by GitHub repo permissions, not by the engine. Each `cp-*` is a private repo with an explicit collaborator list.

### 2.5 CLAUDE.md is generated, not extended

The engine's `CLAUDE.md.j2` template renders a per-tenant `CLAUDE.md` from `.cp-engine.toml`. Tenants never hand-edit this file. A tenant-specific behavior need is a template change in `cp-engine`, parameterized through tenant config.

The generated CLAUDE.md is what enforces:
- The four loading modes (§6)
- The gatekeeper rule (no auto-globbing outside connected tenant repos)
- The trigger-phrase reference
- The status vocabulary on the read side

If you find yourself wanting to hand-edit `CLAUDE.md`, file an issue against `cp-engine` instead.

### 2.6 Versioning discipline

`cp-engine` uses semver:
- **Major** — breaking spec change (file format, status vocabulary, mode contract).
- **Minor** — backwards-compatible new capability (new mode, new template field).
- **Patch** — bug fix only.

Tenants pin to a minor version: `engine = "~= 0.3"`. Patch updates flow automatically; minors require explicit bump; majors require migration. The framework's `CHANGELOG.md` carries migration notes for every major.

---

## §3 Status Vocabulary, Names, and File Anchors

### 3.1 Status vocabulary (locked)

Five values, three sources of truth (kept in sync — the engine has a CI check):

| Status | Active subset member? | Notes |
|---|---|---|
| `Deal` | yes | Pre-engagement / pipeline. Replaces v01's proposed `Potential`. Sub-vocabulary `deal_stage` (`Inquiry / Negotiation / Contract / Won / Lost`) lives in MC-2 only — not surfaced in master CP. |
| `Open` | yes | Active engagement. Renamed from `Active` in MC-2. |
| `Holding` | no | Live state (project could resume). Surfaced in master CP as a `<details>`-collapsed subtable. |
| `Closed` | no | Engagement complete. Renamed from `Complete` in MC-2. |
| `Archived` | no | Older than 30 days post-Close, or explicitly archived. Surfaced in master CP only via `<details>`-collapsed list. |

`Internal` is **not** a status value. The orthogonal `is_internal` boolean carries that axis (lifecycle vs. ownership are distinct).

**Active subset = `Deal ∪ Open` AND `is_internal=false`.** Used by sync to decide which projects appear in the master CP's Active table.

### 3.2 The three sources of truth

| File | Owner | Consumed by |
|---|---|---|
| `mc-2/frontend/src/lib/status.ts` | MC-2 frontend | UI: tabs, badges, color maps, the `isActiveStatus` helper, the `MC_STATUS_ACTIVE` flag map |
| `mc-2/backend/src/status.py` | MC-2 backend | Backend: `active_jobs_sync.py`, future master-CP sync, any Python code reading projects |
| `cp-engine/src/cp_engine/status.py` | Framework | Renderers, the engine's own GitHub-Issues sync (for tenants that don't depend on MC-2 — i.e. `cp-canonic`) |

All three carry the same `MC_STATUSES`, `MC_STATUS_ACTIVE`, and `is_active_status` helper. **Drift detection is a CI check** in `cp-engine` that diffs the engine's copy against MC-2's exported vocabulary on every PR. Out-of-sync = red CI.

Per-status flag (rather than a flat `ACTIVE_STATUSES` set) so adding a future status doesn't require touching every consumer — adding a row to `MC_STATUS_ACTIVE` and giving it a boolean is sufficient.

### 3.3 Project codes and folder structure

A project's identity is its code: `ggl-5168`, `ibx-5153`, `mc-2`, `storyos`. Codes are lowercase, hyphenated, stable for the project's lifetime.

The CP filename **never includes the human-readable project name**. Names rot (renames happen); codes are stable. Filenames are exactly `<code>.md`.

Master-CP rows include both code and name. Project CPs include the name in their H1 and anchor block. The name comes from MC-2's `full_job_name` trigger for FPSF tenants, or from `.cp-engine.toml` for Canonic.

### 3.4 File anchor block

Every `.md` file in a tenant repo starts with the standard anchor block (per Tony's global `CLAUDE.md`):

```markdown
---
Project: <code+name OR Tenant Name OR Context Protocol Engine>
Provenance: Version <NN> | <YYYY-MM-DD>
Filename: <filename>.md
Author: <T.Welch | Drew | Marcello | Brandon | Claude | combinations>
---
```

Renderers populate the anchor block on file creation. Project field uses the full code+name for project CPs (`Project: GGL-5168 Playbooks (Activation)`), the tenant name for `master-cp.md` / `weekly-cp.md` / `CLAUDE.md` (`Project: cp-1p`), and `Context Protocol Engine` for spec docs in `cp-engine`.

**AI must always reference projects by code + human-readable name.** "Updates on GGL-5168?" is forbidden; "Updates on **GGL-5168 Playbooks (Activation)**?" is required. This is encoded in the generated `CLAUDE.md`.

### 3.5 Word-count gating

Per bootstrap v2:
- >2,500 words → duplication audit on next wrap-up
- >3,500 words → archive rotation before commit
- archive files land in the tenant's `archive/` directory, three-digit sequence (`ggl-5168-001.md`, `ggl-5168-002.md`)

The engine implements both checks; `cp render` and `wrap up` enforce them.

---

## §4 The Sync Layer

### 4.1 Two flavors of sync

The engine supports two source-of-truth backends, chosen per tenant in `.cp-engine.toml`:

| Backend | Used by | Reads from |
|---|---|---|
| `mc-2` | `cp-1p`, `cp-firstpersonsf` | MC-2's Postgres database (via Supabase API) for status, name, owner, deadline. Active subset filter applied: `mc_status ∈ {Deal, Open}` AND `is_internal=false`. |
| `github-issues` | `cp-canonic` | GitHub Issues on each tracked project's source repo. Status read from a configured label or project field. |

Tenants pick one backend. Mixed-backend tenants are not supported in v02 (YAGNI; revisit if needed).

### 4.2 What sync does (per cycle)

For each project in the tenant's config:

1. Read source-of-truth fields (status, name, owner, last-touched, deadline)
2. Filter to active subset (per backend's filter rule)
3. Reconcile into the engine-managed region of `projects/<code>.md`
4. Re-render `master-cp.md` (Active / Holding / Closed-recent tables)
5. Re-render `CLAUDE.md` (in case status vocab or modes shifted with engine version)
6. Push to a `cp-sync` branch with bot identity
7. Auto-merge to `main` if checks pass

`weekly-cp.md` is **not touched** by sync — it's pure human territory. Sync touching it would defeat the file-split purpose.

### 4.3 Engine-managed regions

Project CPs mix generated and hand-written content. The engine writes only between HTML-comment markers:

```markdown
<!-- cp-engine:start tracked-issues -->
| # | Title | Status | Owner | Updated |
|---|-------|--------|-------|---------|
| #42 | Auth bug | Open | drew | 2026-05-06 |
<!-- cp-engine:end tracked-issues -->

## Notes (hand-written, engine never touches)

The auth bug is blocked on Brandon's API change…
```

Outside the markers is sacred. The engine validates that markers are present and balanced before writing; missing markers in a CP that should have them surfaces as a sync error.

Engine-managed sections in v02:
- **`tracked-issues`** in each project CP (status, owner, deadline)
- **`active-table`** in `master-cp.md` (the Active subset)
- **`holding-subtable`** in `master-cp.md` (collapsed `<details>`)
- **`closed-recent`** in `master-cp.md` (collapsed `<details>`, last 30 days)
- **`one-line-summary`** column in `master-cp.md`'s Active table (regenerated by deepening pass — see §7.3)
- **`last-sync-timestamp`** in `master-cp.md` header

### 4.4 No write-back

Sync is strictly one-way. Nothing in this system writes status back to MC-2 or to GitHub Issues. If a partners'-review discussion concludes "let's pause GGL-5168," the action item is *go to MC-2 (or Issues) and change the status there* — not edit the markdown.

If a human accidentally edits an engine-managed region, the next sync overwrites it. (No auto-revert on detection — git diff makes it visible if someone wants to investigate.)

### 4.5 Sync identity and target branch

The GitHub Action runs with a bot identity (`cp-engine-bot[bot]` if a GitHub App; a dedicated `cp-engine-bot` user with PAT for v0.1).

Sync commits go to the `cp-sync` branch, which auto-merges to `main` if:
- The diff is non-empty (no-op syncs don't touch git)
- All workflow checks pass on the `cp-sync` branch
- No merge conflict with `main`

Conflicts (rare — engine-managed regions are isolated) page the bot identity into a held PR for human review. Humans never push directly to `cp-sync` — it's bot territory.

Commit format:

```
[cp-sync] <tenant> — <N> projects updated
Source: <mc-2 | github-issues>
Engine: cp-engine v0.X.Y
Updated: <code>, <code>, ...
```

### 4.6 Auth model

**Recommended:** GitHub App per tenant, installed on the tenant repo + every project repo it tracks. App credentials stored as repo secrets in the tenant repo. Mirrors the pattern in `mc-2/backend/src/dropbox_client.py` etc.

**Acceptable for v0.1:** PAT scoped to read-issues across tracked orgs + write to the tenant repo. Must be migrated to App before v1.0 because PATs don't scope per-org cleanly across `FirstPersonSF` + `CanonicOS` + (future) personal.

**Failure handling:** transient GitHub failure → Action retries (built-in). Persistent failure → opens an issue in the tenant repo titled `cp-sync: stalled <date>` so it's visible without a separate dashboard.

**Idempotency:** sync action computes target file state from source-of-truth; running it twice produces the same commit (or no-op).

### 4.7 Initial backfill

On first deployment of a tenant:
1. Configure `.cp-engine.toml` with the project list
2. Run `cp init` locally (each collaborator)
3. Run `cp sync` once to populate everything from scratch
4. Commit and push the initial state

After that, the GitHub Action takes over the cron.

---

## §5 Tenant Configuration

### 5.1 The two config files

```toml
# .cp-engine.toml  (committed — tenant config + project list)
[tenant]
name = "1p"
display = "First Person Jobs"

[engine]
version = "~= 0.3"   # semver constraint; pinned per minor

[sync]
backend = "mc-2"     # or "github-issues"
cron    = "0 * * * *"  # hourly

# MC-2 backend config (omitted for github-issues backend)
[sync.mc_2]
supabase_project_ref = "<env: MC2_SUPABASE_PROJECT>"
# active subset filter applied automatically per §3.1

[[projects]]
code   = "ggl-5168"
github = "FirstPersonSF/ggl-5168-events-calendar"  # source repo for tracked-issues

[[projects]]
code   = "ibx-5153"
github = "FirstPersonSF/ibx-5153-ai-campaign"

# … one [[projects]] block per tracked project
```

```toml
# .cp-engine.local.toml  (gitignored — per-machine paths)
[repos]
"ggl-5168" = "~/Documents/Python/ggl-5136-events-calendar"
"ibx-5153" = "~/Documents/Python/ibx-5153-ai-campaign"
# … one row per project (each user's machine fills this in via cp init)
```

The committed file is the source of truth for *which projects exist*; the local file is the source of truth for *where they live on this machine*.

### 5.2 The `cp init` flow

Walks new collaborators through populating `.cp-engine.local.toml`:

```
$ cp init
cp-engine v0.3.1 — initializing local config for tenant: 1p

Found 6 projects in .cp-engine.toml.

  ggl-5168 (FirstPersonSF/ggl-5168-events-calendar)
  Local path? [skip] ~/Documents/Python/ggl-5168-events-calendar

  ibx-5153 (FirstPersonSF/ibx-5153-ai-campaign)
  Local path? [skip] /Users/drewf/Dropbox/work/ibx-5153

  mc-2 (FirstPersonSF/mc-2)
  Local path? [skip] (no access — skipping)

  …

Wrote .cp-engine.local.toml. cp sync will pick up these paths.
```

Skipped projects are recorded as `"<code>" = ""` so the engine knows the user *intentionally* didn't configure them (Brandon shouldn't fail-loudly on `mc-2` he can't access).

### 5.3 Path resolution

The engine resolves symlinks via `pathlib.Path.resolve()` so Dropbox-backed working dirs work transparently:

```
~/Documents/Python/storyos    →    ~/Dropbox/work/canonic/storyos    (symlink)
```

Engine sees the resolved path. Git operations work. The user organizes their filesystem however they want.

### 5.4 Failure modes

The engine fails loudly (not silently) when:
- A project in `.cp-engine.toml` is missing from `.cp-engine.local.toml` (and not explicitly skipped)
- A configured local path doesn't exist
- A configured GitHub repo isn't accessible (auth missing or revoked)
- A required `engine` version constraint isn't satisfied

Silent skipping is the kind of thing that bites three weeks later.

---

## §6 Reading Modes

The engine's generated `CLAUDE.md` enforces four explicit reading modes. Each session is in exactly one mode at any moment (modes can change mid-session via §6.3).

### 6.1 The four modes

| Mode | Files loaded | Files explicitly NOT loaded | Triggered by |
|---|---|---|---|
| **1. Index-only (default)** | `master-cp.md` | All of `projects/`, `weekly-cp.md`, `canonic/` | Any session opened in the tenant repo without a scoping trigger phrase. |
| **2. Single-project** | `master-cp.md` + `projects/<code>.md` | Other project CPs, `weekly-cp.md` | `update <code>`, `check status <code>`, opening a session in a tracked project repo (auto-loads its CP) |
| **3. Sprint** | `master-cp.md` + `canonic/sprint-cp.md` | All of `projects/`, `weekly-cp.md` | `update sprint`, `check status sprint`. **Only valid in tenants with a sprint surface (`cp-canonic`).** |
| **4. Weekly review** | `master-cp.md` + `weekly-cp.md` + every active project CP + `canonic/sprint-cp.md` if present | (none — this mode is the heavyweight one) | `run weekly review` |

### 6.2 The gatekeeper rule

The generated `CLAUDE.md` ends with:

> Do NOT auto-glob, read, or search any path outside the connected tenant
> repositories unless the user explicitly references it by path or by repo
> name. Project source repos (those configured in `.cp-engine.toml [[projects]]`)
> are opt-in: the user must say "also load <code>" or "switch to <code>",
> or open a session inside that repo's directory.

"Connected tenant repositories" matters because Brandon and Marcello routinely co-load `cp-1p` + `cp-canonic` in a single Claude desktop-app session. The rule is relative to the *set* of currently connected tenants, not a single repo.

### 6.3 Mode-switching mechanics (resolves Tony's Q on Amendment 2)

A session's mode can change. Two recognized patterns:

**Explicit replace.** User phrases that switch the active mode entirely:
- `switch to <code>` → mode 2 with `<code>` (replaces any previous mode-2 target)
- `switch to sprint` → mode 3
- `run weekly review` → mode 4

**Layered/additive.** User phrases that add context without discarding:
- `also load <code>` → adds `projects/<code>.md` to whatever's already loaded; remains mode 2-ish
- `also load sprint` → adds `canonic/sprint-cp.md`

**`run weekly review` mid-session loads on top, doesn't discard.** A user already in mode 2 (working on `ggl-5168`) who then types `run weekly review` gets mode 4 layered over the existing context. The just-loaded `ggl-5168` stays in context; weekly mode adds the rest. Use `wrap up` to close the weekly-review block; the prior mode persists if the session continues.

**Cross-tenant escalation requires explicit action**, not a phrase. To work across `cp-1p` + `cp-canonic`, the user opens both tenant repos (via Claude desktop GitHub MCP) or clones both locally. Mode is per-tenant; cross-tenant is a connection event, not a mode shift.

### 6.4 What's loadable in each surface

Across the two Claude surfaces:

| Surface | Modes 1-4 work? | Project source loadable? | Sync runnable? |
|---|---|---|---|
| Claude Code (local) | Yes | Yes — by phrase or by opening a session in the project repo | Yes — `cp sync` |
| Claude Desktop (GitHub MCP) | Yes — reads CP files from GitHub | Only if the project repo is also GitHub-MCP-connected | No — sync is automated via the tenant's GitHub Action |

The corpus is the same across surfaces; only the loading mechanics differ.

### 6.5 Trigger phrases (full list)

Implemented via the generated `CLAUDE.md`. Any AI session that reads `CLAUDE.md` honors them.

| Phrase | Mode | Action |
|---|---|---|
| (no phrase) | 1 | Default. Read `master-cp.md` only. |
| `update <code>` | 2 | Open `projects/<code>.md` for editing. |
| `check status <code>` | 2 | Read `projects/<code>.md`; summarize without editing. |
| `update sprint` | 3 | Open `canonic/sprint-cp.md` for editing. (cp-canonic only.) |
| `check status sprint` | 3 | Read `canonic/sprint-cp.md`; summarize without editing. |
| `run weekly review` | 4 | Begin live pass of partners'-review workflow (§7.1). |
| `also load <code>` | additive | Layer `projects/<code>.md` onto current mode. |
| `switch to <code>` | replace → 2 | Discard previous mode; load `projects/<code>.md`. |
| `deepen from transcript` | (during 4) | Begin deepening pass (§7.3). |
| `wrap up` | (during 4) | Finalize: word-count checks, master-CP roll-up, commit, push (§7.4). |
| `rotate the CP` | any | Manually trigger archive rotation on the focused CP. |

---

## §7 The Weekly Partners' Review Workflow

### 7.1 Live pass — `run weekly review`

The driver (typically Tony or Drew) opens Claude Code in the relevant tenant repo and types `run weekly review`. Mode 4 loads. Claude:

1. Reads `master-cp.md`, `weekly-cp.md`, every active project CP, and (in `cp-canonic`) `canonic/sprint-cp.md`.
2. Walks every active project, grouped alphabetically by code.
3. For each project:
   a. Reads `projects/<code>.md`.
   b. Announces in chat: *"**GGL-5168 Playbooks (Activation)**. Status: Open. Last touched 2026-05-04 (Tony). Quick Resume: [Quick Resume from CP, ≤2 sentences]."*
   c. Asks: *"Updates? Decisions? Status thoughts?"*
   d. Driver types light tags: `update`, `decision: <text>`, `blocker: <text>`, `next: <text>`, `skip` (no change), or free text. Verbal discussion in parallel — Claude doesn't capture it now.
4. After all active projects + sprint: Claude makes minimal CP edits in real time based on typed tags. No deep prose yet.

The live pass should fit in 30-40 minutes of the 60-minute meeting. The remaining 20-30 minutes is open discussion that the transcript captures.

### 7.2 Verbal anchors

The room uses two natural phrases that help the deepening pass:
- **Project transitions:** "Next, **GGL-5168 Playbooks**…" — gives Claude an unambiguous segment boundary.
- **Decision elevation:** "**Decision:** [statement]" — promotes to the Decisions list.

Not robotic ceremony — just consistent shorthand. Claude infers blockers and action items from context.

### 7.3 Deepening pass — `deepen from transcript`

After the meeting, the driver pastes the Zoom transcript into the same session and types `deepen from transcript`. Claude:

1. Acknowledges the transcript is in-session, **explicitly does not commit it to the repo** (per Hard Constraint).
2. Builds a transcript alias map: matches "fifty-one sixty-eight" → `ggl-5168`, etc. Carries name spellings for all collaborators + key client contacts.
3. For each touched CP plus any project mentioned in transcript:
   a. Locates the project's segment via verbal anchors + alias map.
   b. Extracts decisions, blockers, context, color, sub-decisions.
   c. Produces a **structured diff** in chat showing proposed additions/changes per section.
4. Driver reviews each diff. Approves, edits, or rejects per section.
5. Approved changes are written into the CP files. Transcript discarded at session end.
6. **Regenerates the master-CP one-line summary** for each touched project, from that project's Quick Resume. **Hard cap: ≤120 characters, single sentence, no markdown.** Renderer enforces; over-length truncates with `…` and logs.

**Conflict handling:** if the deepening pass extracts something that contradicts what the live pass captured, Claude surfaces the conflict and asks — never silently overwrites.

### 7.4 Wrap-up — `wrap up`

After deepening, the driver types `wrap up`. Claude:

1. Runs `wc -w` on every CP touched. Triggers archive rotation per bootstrap v2 if any exceeds 3,500 words. Audits for duplication if any exceeds 2,500 words.
2. Updates `weekly-cp.md`'s Quick Resume (`Last partners' review`, `This week's themes`).
3. Promotes any cross-cutting decisions surfaced in deepening to `weekly-cp.md`'s Decisions section (with source attribution: `(2026-05-05, source: ggl-5168 deepening)`).
4. Commits all changes. Single commit message:
   ```
   [partners' review] 2026-W19 — N projects updated

   Live pass + transcript deepening.
   Updated: ggl-5168, ggl-5176, ibx-5153, …
   ```
5. Pushes to `main`.

### 7.5 Mid-week updates

Anyone can trigger `update <code>` (or just open the file) at any time during the week. Mid-week edits do not trigger automation, do not require a deepening pass, and do not require touching `master-cp.md` — sync keeps that surface fresh independently.

The only mid-week touches on `master-cp.md` are MC-2 sync events, which are automated.

---

## §8 Multi-Person Access Model

### 8.1 Tool baseline per person

| Person | Primary tool | File access | Tenants accessed |
|---|---|---|---|
| Tony | Claude Code (terminal) | Direct filesystem | All three |
| Drew | Claude Code (terminal) | Direct filesystem | All three |
| Marcello | Claude Desktop app | GitHub MCP | `cp-1p` + `cp-canonic` |
| Brandon | Claude Desktop app | GitHub MCP | `cp-1p` + `cp-canonic` |

All four can be productive in the system. Tool asymmetry is small (Marcello/Brandon don't run `cp sync` locally — they rely on the Action), but functionally they read and write CP files on parity.

### 8.2 Per-tenant CLAUDE.md

Each tenant's generated `CLAUDE.md` encodes the team's protocol for *that tenant*. The four-mode rules + gatekeeper rule + status vocabulary are identical across tenants (they come from the same template), but the project list and tenant name differ.

If a collaborator wants AI behavior tuned to their personal style, that goes in their global `~/.claude/CLAUDE.md`, not the tenant repo.

### 8.3 Branching & coordination

Per tenant:
- **Push directly to `main`** for routine CP edits (partners'-review commits, mid-week per-project updates).
- **Branch + PR** for: structural changes (new project, schema migration, automation tweak).
- **`cp-sync` branch is bot-managed.** Humans never push to it.
- **Partners'-review rule:** only one driver edits during the meeting. Others contribute verbally and don't push concurrently.

### 8.4 Conflict resolution

CP files are small; conflicts rare. Standard `git pull --rebase` workflow. If two humans hit each other in a CP's hand-written section, resolve in <2 minutes.

If a human conflicts with the bot's `cp-sync` branch (rare — engine-managed regions are isolated), the human pulls latest `main`, redoes their hand edit, pushes. The bot retries on next sync.

---

## §9 Canonic Integration

### 9.1 Two systems, one summary layer

ClickUp stays canonical for sprint task tracking. `cp-canonic/canonic/sprint-cp.md` is a *summary* layer that captures what the founders need to remember about each sprint that doesn't fit in a ClickUp ticket.

### 9.2 Update cadence

- **Partners' review** is the primary touchpoint.
- **End of sprint:** driver runs `rotate the CP` on `canonic/sprint-cp.md`. The rotation moves the closed sprint's content to `canonic/archive/sprint-W##-cp.md` and resets the live file with the new sprint's frame.

### 9.3 No automation between ClickUp and the CP

ClickUp tasks do not auto-write to `sprint-cp.md`. Sprint summaries are produced by humans + Claude during the partners' review. ClickUp → CP automation is v3+ (revisit when manual cadence shows pain).

---

## §10 Migration Path from v01

### 10.1 The current `context-protocol` repo becomes `cp-engine`

Today's `FirstPersonSF/context-protocol` (this repo, where this spec lives) is the right home for the framework. Rename in place — preserves git history, no re-fork.

Steps:
1. Rename `FirstPersonSF/context-protocol` → `FirstPersonSF/cp-engine` on GitHub
2. Add `pyproject.toml` and `src/cp_engine/` skeleton
3. Move `docs/specs/cp-engine-spec-v01.md`, `cp-engine-spec-v01-amendments.md`, `cp-engine-spec-v02-architecture-change.md` → `docs/specs/history/`
4. v02 (this file) lands at `docs/specs/cp-engine-spec-v02.md`
5. Tag v0.1.0 once the engine has minimal sync + render

### 10.2 Tenant repos created fresh

`cp-1p`, `cp-firstpersonsf`, `cp-canonic` are new repos. No git history to preserve — they didn't exist.

Order (per architecture-change doc):
1. **`cp-firstpersonsf` first.** Highest-stakes tenant — contains `mc-2`. Seeds `projects/mc-2.md` from the existing MC-2 state via the `mc-2` sync backend.
2. **`cp-1p` second.** Seeds projects from the current MC-2 active jobs. Sync backend = `mc-2`.
3. **`cp-canonic` third.** Starts empty; populated organically over the next few sessions. Sync backend = `github-issues`.

### 10.3 PR #17 is independent

PR #17 (the MC-2 status vocab refactor) is a hard prerequisite for `cp-firstpersonsf` and `cp-1p` (because both use the `mc-2` sync backend, which depends on the new vocabulary), but it's already complete and pending merge — independent of the rest of this migration.

### 10.4 No backfill of historical CPs

v02 starts fresh. No attempt to retro-fit ggl-repos or other historical artifacts. Per Q2 in v01: ggl-repos continues independently.

---

## §11 Phasing

### v0.1 — framework minimum (target: 2 weeks from spec close)

- `cp-engine` skeleton: `status.py`, `config.py`, `modes.py`, `render.py` (with master-cp + weekly-cp + project-cp + CLAUDE.md templates), basic `sync.py` for the `mc-2` backend
- `cp` CLI with `init`, `render`, `sync`, `status` subcommands
- Reusable GitHub Action at `cp-engine/sync@v0`
- v0.1.0 tagged; published to GitHub Packages or PyPI (decide at release)

### v0.2 — first tenant (target: +1 week after v0.1)

- `cp-firstpersonsf` standing up; first `cp sync` runs end-to-end against MC-2
- `projects/mc-2.md` populated, partners' review run once against this tenant
- One-line summary regen verified end-to-end

### v0.3 — second + third tenants (target: +2 weeks after v0.2)

- `cp-1p` standing up; full FPSF active-jobs roster
- `cp-canonic` standing up; `github-issues` sync backend implemented
- All four loading modes verified across all three tenants

### v1.0 — production (target: +1 month after v0.3)

- GitHub App migration complete (PATs retired)
- Drift-detection CI for the three status vocabularies
- All four collaborators onboarded
- One full quarterly cycle of partners' reviews completed without engine bugs

### v1+ Stretch

- Per-bullet diff granularity in deepening pass
- Slack notification on sync events (so non-driver founders see status changes in real time)
- Auto-detection of "stale CP" (>14 days untouched on an active project) surfaced in master CP
- ClickUp → `canonic/sprint-cp.md` automation (revisit when manual cadence shows pain)

---

## §12 Open Questions

*(Per spec-writing-guide §Question-Resolution Workflow, all post-flight questions resolve before the spec closes. This section starts empty and accumulates only if v02 implementation surfaces ambiguity.)*

None as of 2026-05-07.

---

## Appendix A: Glossary

| Term | Meaning |
|---|---|
| **CP** | Context Protocol — the bootstrap pattern this spec extends. |
| **Framework** | `cp-engine` — the versioned package. |
| **Tenant** | A `cp-*` repo holding one corpus of CP files (e.g. `cp-1p`). |
| **Master CP** | `master-cp.md` — the per-tenant index of all projects. |
| **Weekly CP** | `weekly-cp.md` — the per-tenant meeting-state file (Quick Resume, themes, decisions, research). Loaded only in mode 4. |
| **Project CP** | One file per project: `projects/<code>.md`. |
| **Sprint CP** | `canonic/sprint-cp.md` — the rolling current-sprint summary in `cp-canonic` only. |
| **Engine-managed region** | A section of a CP file the engine writes and re-writes; bracketed by `<!-- cp-engine:start … -->` / `<!-- cp-engine:end … -->` markers. |
| **Live pass** | Partners' review's first phase: driver types light tags during discussion. |
| **Deepening pass** | Partners' review's second phase: transcript-driven richer extraction with diff review. |
| **Sync** | One-way propagation of source-of-truth state into engine-managed regions. |
| **Active subset** | The set of `mc_status` values that count as "active" — `Deal ∪ Open` AND `is_internal=false`. |
| **Driver** | The collaborator running the AI session during the partners' review. |
| **Verbal anchor** | One of two natural phrases ("Next, [code-name]" / "Decision:") that aid transcript parsing. |
| **Mode 1-4** | The four loading modes that govern what files Claude reads in a session. |
| **Gatekeeper rule** | The CLAUDE.md instruction forbidding auto-globbing outside connected tenant repos. |

---

## Appendix B: Decisions Log

Bucket-1 decisions (made silently per spec-writing-guide defaults) and notable v01 → v02 changes, for traceability.

### Carried forward from v01 unchanged

1. **Branching strategy: push-to-main with serial-driver partners'-review rule.** Branch + PR only for structural changes.
2. **Project name display: denormalized into `master-cp.md` and per-CP H1.** Avoids round-tripping to source-of-truth on every read.
3. **Per-CP "Active research" section is recommended, not required.**
4. **Filenames lowercase, hyphenated.** Matches MC-2's project code formatting.
5. **Anchor block `Project:` field uses full code+name for project CPs.**

### Changed in v02

6. **MC-2 sync events broadened to active subset (Deal ∪ Open).** v01 said "Active only"; Amendment 1 redefined active subset; v02 reflects.
7. **Status vocab `Deal | Open | Holding | Closed | Archived`.** v01 had `Potential | Open | Closed | Archived | Internal`; Amendment 1 redefined.
8. **`is_internal` is an orthogonal boolean, not a status value.**
9. **Reading discipline is four explicit modes with a gatekeeper rule.** v01 had a single default reading model.
10. **`master-cp.md` split — meeting-state moves to `weekly-cp.md`.**
11. **One-line summary in master-CP rows, regenerated only during deepening pass, ≤120 chars.**
12. **Multi-tenant + framework-package architecture.** v01 was single-repo.
13. **Sync identity is a bot on a `cp-sync` branch with auto-merge.** v01 was direct-to-main from the GitHub App.
14. **Per-machine paths via gitignored `.cp-engine.local.toml`.** v01 had no analog (single repo, one machine).
15. **`mc-2` placement in `cp-firstpersonsf` as a regular project.** v01 didn't address (single-repo).
