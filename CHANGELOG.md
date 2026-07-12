# Changelog

All notable changes to `cp-engine` are recorded here. The package follows [semver](https://semver.org/).

Tenants pin to a minor version (`engine = "~= 0.1"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration notes.

## Unreleased

**Account mirror: reap on demote/retire.**

- `_mirror_account_elements` now reconciles `1p/<account>/_stakeholders/`
  both ways: elements that left the account scope (demoted back to their
  provenance project, or retired) have their mirror files REMOVED, not
  left to linger. Only engine-written `*.md` files are reconciled; other
  files humans park in the dir survive. A demoted element reappears as a
  normal `spine/_authored/` file on its provenance project's next sync.

## v0.61.0 — 2026-07-12

**Asset ingest: initiatives reach folder-binding + ingest parity with
engagements (mc-2 #192, "open the door").**

- `resolve_project_folders` now falls back to the `initiatives` table for
  slug codes (`mission-control`, `storyos`) — and for digit-carrying slugs
  that miss `projects.number` — returning `ProjectFolders` with
  `is_initiative=True`; `resolve_project_folders_by_id` likewise falls back
  to `initiatives.id` on a `projects.id` miss (the initiative workspace's
  button passes that id). Folder coordinates hydrate from initiative-owned
  `project_integrations` bindings (`hydrate_initiative_row` now carries
  `google_drive_folder_id` + `mc_dropbox_folder_id`, same ref shapes as
  projects).
- The previously-inert `is_initiative` / `initiative_id` owner seam is live:
  the run loop's skip-cache and cross-path dedup pre-checks key on the owner
  pair (`initiative_id` for initiatives), and the document-ingest pipeline's
  two engagement-shaped seams (`deduplication.check_asset`,
  `storage.create_asset`) are rebound per-run so initiative rows INSERT with
  `initiative_id` (migration 081's exactly-one-owner CHECK) instead of
  FK-crashing on `project_id`.
- Confirm-gate parity (#59): an initiative with no folder bindings gates
  exactly like an unconfigured client project (CLI refusal / `--all` SKIPPED
  note / webhook structured refusal) — never a silent empty run. Initiatives
  have no per-source enable columns, so both sources read as enabled and a
  missing binding reports "enabled but folder not set".
- `list_files`’ client-only kind guard now exempts initiatives (which live
  under self-* companies by construction); non-client ENGAGEMENTS still
  skip. `cp ingest-assets <slug>`, the `--all` fan-out (which already
  enumerated initiatives), and the webhook route all work end-to-end for
  initiatives with no interface changes.

## v0.60.0 — 2026-07-11

**Asset ingest: confirm-gate for projects with NULL Drive/Dropbox folders (#59).**

- `ingest_project_assets` now short-circuits when a resolved client project
  has no ENABLED source with a folder id (both folder columns NULL/empty, or
  every enabled source's folder unset) — recording WHY on the new
  `IngestRunResult.unconfigured_reason` (also mirrored into `source_notes`)
  instead of returning a normal-looking empty run. A disabled source's NULL
  folder is not a gap; non-client kinds keep their existing `list_files` skip.
- CLI `cp ingest-assets <code>`: refuses (exit 1) with an actionable message
  for an unconfigured project; new `--allow-empty` flag downgrades the
  refusal to a SKIPPED note (exit 0) for scripted use. Single-project only —
  combining with `--all` is a usage error.
- CLI `cp ingest-assets --all`: the sweep never hard-fails on a config gap —
  unconfigured projects are skipped with a visible per-project `SKIPPED`
  note and counted in the new `unconfigured=` total.
- Webhook `/api/assets/ingest` (the mc-2 button): an unconfigured project's
  run row records a structured refusal (`status='failed'` + "set the
  project's folders in MC-2" error), which the mc-2 button's status line
  already renders — no dashboard change needed.

## v0.59.0 — 2026-07-11

**Asset ingest: same-title re-ingests supersede instead of duplicating (#57).**

- Ingest: a 'created' asset whose exact title (case-insensitive) already
  exists active in the project with different content now chains to the
  prior copy via `prev_asset_id`, flips the old copy to
  `status='superseded'`, and deletes its chunks (embeddings cascade) — so
  `pull_source` can never interleave old and new chunks under one citation.
  Same-title-same-hash stays the existing dedup no-op; 'versioned' rows keep
  the pipeline's own same-path chain. New `superseded` count in the run
  summary.
- Read paths: `list_project_sources` and the `_sources.md` manifest (both via
  `list_sources`) drop assets that have a successor — another asset's
  `prev_asset_id` pointing at them — covering backlog rows whose status flip
  never landed. `pull_source` already excludes superseded rows via the
  scoped-chunks RPC's `status='active'` filter.
- New `cp assets-dedupe` (dry-run by default; `--apply` to execute): the
  one-time tenant-wide backlog sweep. Groups active assets by owner +
  case-insensitive title, keeps the newest, chains it, and retires the older
  copies — but any group whose older copy is referenced by a spine element's
  `sources` is BLOCKED and reported, never touched. Asset rows are never
  deleted (chain history); only their chunks are.

## v0.58.0 — 2026-07-11

**Sprint files: deliverable-cards engine region (cards consumer 3 of 3).**

- Engagement sprint files' `### Deliverables` gains an engine-managed
  `deliverable-cards` region — one line per deliverable (name · due ·
  done ✓ · outputs accrued), derived at sync time from the estimate +
  linked bars + spine serves. Hand-written notes below the region stay
  human territory; initiatives keep their slimmer scaffold. Best-effort:
  no MC-2 creds → the region renders its empty state. Files pre-dating
  the region pick it up with next week'''s scaffold.

## v0.57.0 — 2026-07-11

**demote_stakeholder (the scope ladder's down verb).**

- New MCP tool: returns an account-scoped element to its provenance project
  (`scope='project'`, `company_id` cleared) — resolvable from any of the
  company's projects, targeting provenance. Parity with mc-2 #189's
  dashboard Remove-from-account. Tool census: 16.

## v0.56.1 — 2026-07-11

**Fix: deactivation sweep spared account-level dirs.**

- The stale-project sweep treated `1p/<account>/_stakeholders/` (the new
  account mirror dir) as an unknown project working dir and moved it to
  `inactive/`. Underscore-prefixed dirs under an account are engine
  surfaces, never project dirs — the sweep now skips them.

## v0.56.0 — 2026-07-11

**/cp-prep deliverable strip (deliverable cards, consumer 2).**

- Each engagement's planning block gains a `**Deliverables:**` strip — one
  line per deliverable of the default estimate (`name · due ~date · done ✓ ·
  N outputs accrued`), derived from phase_deliverables + linked schedule
  bars (mig 105) + spine `serves` bindings. Best-effort; initiatives and
  no-estimate projects render without it.

**Account-dir stakeholder mirror.**

- `cp sync` now mirrors account-scoped elements (promoted stakeholders) to
  `1p/<account>/_stakeholders/<slug>.md` — fetched by company, so sibling
  projects' promotions appear and every project's sync converges on the same
  files. The provenance project's `spine/_authored/` no longer carries them
  (stale pre-promotion mirror files are removed); archived account elements
  are skipped. Best-effort, never aborts sync.

**Frameworks slice 2: authoring adapter + decision diff.**

- `framework_compose` returns `body` — the package's `{sections}` content
  adapted to spine-element markdown (explicit consumer-boundary adapter;
  framework identity never renders into the body).
- `framework_decompose` accepts `baseline` (a prior result) and returns
  `diff`: changed/new/dropped fields + confidence moves, with
  hardened/softened summaries — unchanged-value-but-hardened = a decision
  RATIFIED, the P&P Report's citation shape.

## v0.55.0 — 2026-07-11

**SOW as projection + Brief contract (canonical-objects slices).**

- `agreement_projection.render_engagement_block`: the Agreement element's
  read-side projection — phases, deliverables (dated via schedule bars,
  done-marked, ANY-done-wins), activities, kickoff — composed live from the
  estimator. `pull_spine_element` appends it for Agreement-layer elements of
  engagements (`derived_block: true`); fail-soft when no estimate.
- `attach_nudge`: an Agreement pull with no attached source names any
  SOW-looking doc sitting in the source store.
- Tenant CLAUDE.md documents the two standing-element contracts (six-section
  Brief; human-terms-only SOW). mc-2 #185 updated the scaffolds new spines
  are born with.
- Deferred: the mirror-side derived block (`cp sync` markers) — read-side
  projection ships first.

## v0.54.0 — 2026-07-11

**Account-scoped stakeholders (canonical-objects slice 1).**

- mc-2 mig 104 (applied live 2026-07-11): `spine_substance.company_id` +
  `scope ('project'|'account')`; `project_id` retained as provenance on
  account rows.
- Read union: `list_spine_elements`/`pull_spine_element` (and both write-side
  resolvers) surface the company's account-scoped elements in every project
  of that company; rows carry `scope`. Initiatives (no company) skip the
  account arm.
- New `promote_stakeholder(code, key)` MCP tool: element-level move to
  account scope (all versions together), with an already-promoted note, a
  sibling-collision guard ("version the account element instead"), an
  initiative rejection, and a non-Stakeholders-layer warning.
- Element-level writes (`set_spine_element` layer/framing/serves,
  `retire_spine_element`, `add_spine_version`) now target the element's OWN
  provenance `project_id`, so account elements are editable from any of the
  company's projects.
- Mirror note: account rows continue to mirror under their provenance
  project's `spine/` until the account-dir mirror
  (`1p/<account>/_stakeholders/`) ships as a follow-up.
- Design: cp tenant `docs/plans/2026-07-11-stakeholders-account-scope-design.md`.

**Inbound frameworks, slice 1 (MCP tools).**

- New `inbound-frameworks[anthropic]` dependency @ `a7214a7`; all
  1p-component-library pins bumped to the same SHA (verified no-op for the
  existing packages — empty diff over their subdirectories).
- Three MCP tools: `framework_readiness` (curated menu + snapshot identity,
  no LLM), `framework_decompose` (extraction over an explicitly scoped
  corpus — repo-relative files, spine elements, or source docs; path-escape
  guarded; misses recorded, never dropped), `framework_compose` (generation
  from human-confirmed field values). Anti-graveyard and the framework-IP
  invariant carried per the package's IMPLEMENTATION.md; engines persist
  nothing; `usage` surfaced per call.
- Model knobs: `CP_FRAMEWORKS_DECOMPOSE_MODEL` / `CP_FRAMEWORKS_COMPOSE_MODEL`
  (default `claude-sonnet-5`, the pilot-validated tier).
- Design doc: cp tenant `docs/plans/2026-07-11-inbound-frameworks-integration-design.md`.

## v0.53.0 — 2026-07-10

**Spine MCP curation verbs (#47): retitle, rebind, retire.**

- `set_spine_element` gains `framing` (retitle — the est_item_id/machine path
  never changes, so existing keys keep working) and `serves` (rebind to
  work-item ids; `[]` unbinds; `binding` derives as live/unbound, the same
  rule the authored-element builders use). Like `layer`, both apply to EVERY
  version of the element so its history moves together.
- New `retire_spine_element(code, key)` — archives every version and
  supersedes the live row, in that order (a failure in between leaves the
  element hidden, not half-retired). Nothing is deleted; a dashboard
  un-archive brings it back.
- **Fix:** archived elements are now invisible to every spine read path
  (`list_spine_elements`, `pull_spine_element`, and both write-side
  resolvers). Previously `archived=true` alone left the element in listings
  and resolvable — the live-but-archived leak found during the sap-5174
  hygiene pass. NULL `archived` (pre-column rows) still reads as unarchived.

## v0.52.0 — 2026-07-09

**Spine distill data-loss fix (#44) + async Frame & promote.**

- `promote_card` no longer serially overwrites distinct artifacts bound to
  one work item: when a promote's sources diverge from the bound card's live
  version's sources, it now CREATES a new authored element with
  `serves=[work-item]` (shared `build_create_rows` — indistinguishable from
  MCP `create_spine_element` output, mirrored under `spine/_authored/`)
  instead of superseding the card. Same-source re-distills still version.
  Forensics: cp tenant `docs/spine-distill-overwrite-issue-2026-07-09.md`.
- Webhook `/api/spine/promote` 409s an already-promoted card (the observed
  double-submit duplicate write) and is now 202-then-background: the click
  returns immediately with a `spine_promote_runs` row
  (`kind='frame_promote'`) the mc-2 dashboard polls; clone → directed
  distillation → push → mirror → card flip run in a background task.
- Webhook tenant clones for promote are now sparse
  (`--filter=blob:none` + cone checkout of the scope dirs) — no more
  full-tenant checkout per click.

## v0.51.2 — 2026-07-08

### The CLI-downgrade loop is dead

- **`sync-cli-version.sh` can no longer move the CLI backwards.** The
  plugin's SessionStart hook trusted the marketplace clone's
  `plugin.json` as the target version, but Claude Code doesn't reliably
  refresh that clone — so after every release, any session started
  outside a tenant "healed" a newer installed CLI down to the stale
  clone's version. Two fixes: (a) the hook now refreshes the marketplace
  clone itself before reading it (guarded: only a checkout under
  `~/.claude/plugins/` is ever reset); (b) a hard no-downgrade guard —
  if the installed CLI is newer than the plugin says, the hook no-ops,
  even offline. One manual clone reset per machine picks this up; after
  that it's self-sustaining.

## v0.51.1 — 2026-07-07

### Dates loop: partners channel lives in MC-2, not .cp-engine.toml

- The partners-rollup channel id now resolves from MC-2's ``app_config``
  table (key ``dates_loop_partners_channel``, value = channel id string
  or ``{"channel": "C0…"}``) — channel configuration has ONE home, mc-2,
  same as the per-project channel map. The ``[dates_loop].partners_channel``
  toml knob is removed (``window_days`` stays). Absent key → the rollup
  post is skipped; per-project posts are unaffected.

## v0.51.0 — 2026-07-07

### Commitments consolidation: MC-2 replaces ClickUp for dated work (#38, #39)

MC-2's new `public.commitments` table (mc-2 mig 097) is now the single
store for who-owes-what-by-when. ClickUp is out of the ingest and
planning paths entirely (the app itself survives for other uses).

- **Meeting-ingest writes repointed** — the webhook's Stage A writes
  Fathom action items as `date_status='proposed'` commitments instead of
  ClickUp task proposals; the `set-milestone` / `set-client-ask-task`
  verbs write commitments with a real `due_date` column and no
  `clickup_list_id` gate. Hash recipes unchanged, so re-ingests stay
  no-ops across the cutover. The `clickup-task-closed` read path keeps
  working against historical rows during the decommission tail.
- **Weekly Slack dates loop** (`cp dates-loop`, `POST /dates-loop`) —
  per-project channel posts (slipped / due this week / due next N days /
  needs a date, with MC-2 schedule milestones riding along) plus a
  tenant-wide partners rollup with pile-up detection. The post is the
  date-agreement mechanism: `posted_count` bumps on each successful
  send, `proposed → agreed` promotes at the second unchanged post, and
  past-due open rows get stamped `slipped`. Post-only v1. New
  `[dates_loop]` config block (`partners_channel`, `window_days`).
- **prep-planning off ClickUp** — MC-2's estimator schedule is the sole
  milestone source; them→us commitments are the client-asks; us→them and
  internal commitments join the Open Commitments table with
  `[proposed]`/`[slipped]` markers on unratified dates; sprint-file asks
  dedupe against commitments via the shared record-ask `cp:hash`. The
  "(ClickUp list not set)" warnings are gone. The `clickup_*` kwargs on
  the public render entry points remain as documented no-ops.
- **`/cp-prep` six-section contract** (#39, shipped in the same window) —
  `_planning.md` is pinned to Focus list / Decisions & blockers /
  Cross-cutting patterns / Per-owner commitments / Forward calendar /
  Roster table, with the invariant that every active project appears
  exactly once (Focus entry or roster row).

## v0.50.2 — 2026-07-06

### Spine write tools resolve elements the same way the read tools do

- **`add_spine_version` fixed**: it looked up the element's prior versions
  with `.eq("project_code", <code passed by caller>)`, but the stored
  `project_code` is the canonical slug (`ibx-5153-ai-campaign`), so any
  caller passing a shorter form (`ibx-5153`) got `no authored element`
  even though `pull_spine_element` resolved the same key fine. It now
  scopes by `project_id` (the resolved UUID, same as every read path) and
  accepts a `framing` (title) substring as well as an exact `est_item_id`,
  via the new shared `project_sources.resolve_element_versions`. New rows
  carry the element's own stored slug forward.
- **`create_spine_element` collision guard fixed** (the write-side twin):
  it scoped the existing-element check by `project_code` too, so a slug
  collision could be MISSED when the caller's code differed from the row's
  — it now scopes by `project_id`.
- Regression tests pin both directions, including the framing-substring
  key form and full-version-history numbering; the write-tool test fake
  now applies its `.eq()` filters (it previously ignored them, which is
  why the bug slipped through).

## v0.50.1 — 2026-07-04

### Account / sprint-planning ingest: transcript cap, model, timeout

- Transcript ceiling for account + sprint-planning plan generation raised
  60k → 400k chars. The old cap sat exactly at a one-hour meeting's
  length, silently cutting the END of longer 1P sprint-planning sessions
  — the projects discussed last got nothing routed. Truncation (now
  effectively unreachable) logs a warning instead of being invisible.
- Both multi-project plan generators default to `claude-opus-4-8`
  (was `claude-opus-4-7`) — the 1P scope is the hardest routing job the
  pipeline has (~20 projects in one call). Other model pins untouched
  (incl. project_sources' explicit do-NOT-bump pin).
- `_call_claude` gains a `timeout` param (default 120s unchanged);
  account/sprint-planning callers pass 300s — their prompts are ~6× larger
  now and their responses routinely 8k+ tokens.

## v0.50.0 — 2026-07-04

### Sprint-planning accuracy: MC-2 schedule milestones, freshness verdicts, decision ages

- **Forward Calendar reads MC-2's estimator schedule as the PRIMARY
  milestone source** (`_fetch_mc2_schedule_milestones`): day-granular
  milestones + feedback windows from `estimator.schedule_items`
  (start_date + start_week*7 + day_offset — the Gantt's date math),
  skipping `done` items. ClickUp `milestone` tags remain the secondary
  source. The ClickUp-centric empty-state sentinels were rewritten and
  only fire when neither source has anything.
- **`engagement_number()` slug fix in clickup_routing** — engagement
  codes have been full slugs ("ggl-5136-go-safety-website") since the
  v0.35 canonical-id change, but the resolver only checked the TAIL
  segment for digits, so prep-planning silently failed to resolve the
  ClickUp list for every slug-coded engagement ("ClickUp list not set"
  even when it was). The number is now taken from the second dash-segment
  (short "ggl-5168" still works; embedded years like "…-update-2026"
  are not mis-parsed). Live effect: 17 more engagements resolve.
- **Exec-summary freshness verdicts in the bundle** — each block's
  `**Exec Summary:**` heading carries `(updated <date> · <n>d ago)`
  parsed from the wrap-up stamp; >14 days renders a ⚠ STALE warning so
  the in-session synthesis knows which project states to distrust.
  Unstamped summaries are flagged as freshness-unknown.
- **Cross-cutting decisions are aged** — each entry renders
  `(<n>d old · <date>)`, with an "aging: resolve or re-affirm" nudge at
  14+ days and an explicit note on undated entries. The tenant CLAUDE.md
  wrap-up protocol gains a weekly-cp.md decisions sweep (append
  `[resolved: …]` markers for done/expired entries — the planner already
  drops resolved ones).
- `/cp-prep` skill updated for all of the above (milestone sources,
  STALE handling in the Focus list, new empty-state guidance).

## v0.49.0 — 2026-07-03

### MC-2 integration ids resolve from project_integrations bindings (read-flip)

- New `mc2_bindings` module: batch-fetch a project's/initiative's
  `project_integrations` rows and hydrate the legacy dict keys
  (`slack_channel_id`/`slack_channel_ids`, `clickup_list_id`,
  `google_drive_folder_id`, `mc_dropbox_folder_id`) from the normalized
  `external_ref` bindings. MC-2's flat integration columns are being
  retired (mc-2 PR #146 is the mc-2-side read-flip); cp-engine no longer
  selects them anywhere:
  - **ClickUp routing** (`clickup_routing.resolve_clickup_project`) — list
    ids come from the owner's `''` clickup binding. Covers ingest
    set-milestone / set-client-ask-task, prep-planning ClickUp allocation,
    and the webhook's clickup-propose via their existing wrappers.
  - **Slack channel map** (`cp slack-channels`, weekly digest) — channel
    ids come from slack bindings: the `''` singleton is the primary,
    labeled rows are related channels. `slack_channel_name` was never
    populated in MC-2 and is now always `None` on `ChannelMapRow`.
  - **Asset ingest** — Drive folder id and Dropbox folder path hydrate
    from the `google_drive` / `dropbox` bindings.
- Ship THIS version everywhere (webhook + local CLIs) before MC-2's
  Phase-C migration drops the flat columns — older engines still SELECT
  those columns and will 500 against a post-drop schema.

## v0.48.1 — 2026-07-03

### spine_substance column guard — writer identity (mc-2 #130)

- Every MC-2 Supabase client built by `mc2_db.get_client` now sends
  `X-Spine-Writer: cp-engine`. MC-2 migration 092 installs a BEFORE
  UPDATE trigger on `spine_substance` that rejects changes to the
  engine-owned columns (`body`/`status`/`origin`) unless the request
  carries an authorized writer identity (this header via PostgREST's
  `request.headers` GUC, or the `app.spine_writer` GUC for direct SQL
  sessions). Curation-column writes are unaffected. Ship THIS version
  everywhere cp-engine writes to MC-2 (webhook, local CLIs) before the
  trigger migration is applied.

## v0.48.0 — 2026-07-03

### Sprint-planning pipeline fixes (#16 + assessment findings)

- Bare `cp prep-planning` no longer renders the deprecated 426-line
  inventory — it exits with a pointer to `--bundle` / `--summary`; the
  dump moved behind an explicit `--legacy-render` with a provenance
  stamp in its header.
- Cross-cutting decisions parser rewritten: hash-marked auto-ingest
  entries and date-only entries never matched the old end-of-line
  regex, so planning docs carried a stale, arbitrary subset. The
  28-day window now applies to everything; `--summary` reports
  stale/undated counts.
- Planning-week `sprint_allocations` now flow into the bundle and
  summary (`tenant_hours_planned`, "Planned (this sprint)" strip) —
  forward capacity, not just last week's actuals (#16).
- `capacity_binding` reshaped to `{basis, owners}` — planned-hours
  based (≥40h or ≥5 allocated projects, hours shown) when allocations
  exist; owner-of-record fallback labeled as such.

## v0.47.0 — 2026-07-03

### Architecture Phase 4 (cp-engine portion): god-module splits + bypass retirement

**webhook/main.py split** (#32, PR #35). 4,062 → 217 LOC:
`signatures.py` (three HMAC schemes + replay window), `git_ops.py`
(clone→commit→push, Correlation-Id trailer), `pipeline.py` (shared
auto-ingest pipeline), and six domain routers. All 13 route paths,
response shapes, and error strings identical; `main` re-exports the
historical names.

**cli.py split** (#33, PR #36). 3,162 → 188 LOC; implementations in
`cp_engine/cli_cmds/` (11 modules). The command surface is
byte-identical — `cp --help` unchanged; every flat command name
preserved.

**Raw-client bypass retirement** (#34, PR #36). Every
`MC2Backend().connect()` callsite now acquires its client via
`mc2_db.get_client`; `connect()` deleted. `spine_client()` remains the
one protocol-sanctioned raw access (sync.py's spine mirror).

## v0.46.0 — 2026-07-03

### Architecture Phase 3: DAL + observability (issues #26–#28)

**Golden-markdown tests** (#26, PR #29). Byte-exact golden fixtures for
`render.py` and `sprints.py` outputs (`UPDATE_GOLDENS=1` to regenerate),
plus first coverage for `plan_from_account_meeting.py`. 45 new tests.

**Supabase DAL — `cp_engine.mc2_db`** (#27, PR #30). One client
constructor (`get_client`) replaces 15 divergent construction sites;
3-tier credential resolution (env → 1Password `op://` → mc-2
`backend/.env`) moved from `sync_mc2` with a resolved-creds memo (one
`op read` per process, not per query); `required=False` fail-soft logs
what it swallows. `Tables` registry names every MC-2 table (17 public +
5 estimator); no raw `.table("...")` literal remains in `src/` or
`webhook/`, enforced permanently by a test. Column-set constants
consolidated; typed row mappers where adopted. Fixed along the way: the
asset-ingest pipeline branch called a deleted helper (`NameError` on
real, non-injected runs).

**Webhook observability + self-heal consolidation** (#28, PR #31).
Sentry on the webhook — env-gated on `SENTRY_DSN`, no-op otherwise;
14 formerly-silent swallow-and-continue sites now alert. A correlation
id per delivery (honors `X-Correlation-ID`) flows through every log
line (`[cid:...]`), pushed commit messages (`Correlation-Id:` trailer),
the `auto_ingest_runs` row (new `correlation_id` column, applied via
the mc-2 migration ledger; insert is column-tolerant either way), and
Sentry tags — one grep reconstructs one delivery end-to-end. The
plugin's SessionStart self-heal hook now defers inside any tenant
(ancestor `.cp-engine.toml`) so the tenant pin is the single version
truth; the two hooks can no longer disagree.

## v0.45.1 — 2026-07-03

### promote_card stamps `layer` from the card's kind

Frame-promoted meeting distillations landed with `layer` NULL at the source:
`promote_card` built the substance file without a layer, so the mirrored
`spine_substance` row couldn't be filed by layer in the MC-2 UI (Drew's
v0.45.0 residual). Now:

- Fresh promotes stamp `layer = canon_layer(kind)` (activity → `Activity`,
  deliverable → `Deliverables`) into the file's frontmatter.
- Re-promotes into a pre-stamping file (the `add_version` path) backfill the
  missing layer the same way instead of preserving NULL forever.
- The ~10 existing NULL rows across 6 projects were repaired directly in
  MC-2; `layer` is MC-2-owned in reconcile, so the repair is durable.

## v0.45.0 — 2026-07-03

### Spine MCP: layer control + retrospective/research kinds

From Drew's ibx-5153 spine-report review — two write-path gaps that forced
direct SQL on `spine_substance`:

- `set_spine_element` gains a `layer` argument: re-file an element under any
  spine layer (retrospective, research, synthesis, decisions, client
  feedback, timeline, …). The value is canonicalized and applied to every
  version of the element so its history stays in one layer.
- The layer canon (`authored_element._LAYER_CANON`, mirrored byte-identical
  in mc-2) gains `retrospective`, `research`, `deliverable(s)`, `timeline`,
  and `clientfeedback` aliases — layers the spine UI already renders but the
  write vocabulary was missing. `create_spine_element` can now author a
  retrospective in one call.

### Inbox: re-ingest retires stale wrong-job cards

`build_inbox_card_from_transcript` now calls `retire_stale_cards`: when a
meeting ingests under a project, actionable (proposed|framed) inbox cards for
the same source_ref in OTHER projects auto-dismiss — the fix for "meeting
tagged to the wrong job leaves a stale Frame & promote card". Promoted cards
are never touched. Pairs with mc-2's new manual Dismiss button (PR #120).

### document-ingest repin (@7cd7b18)

The visual fallback now triggers on NEGLIGIBLE extracted text (default <200
chars, `INGEST_VISUAL_MIN_TEXT_CHARS`), not exactly-zero — designed decks
often leak a single colophon line. Page-cap default raised 60→100.

## v0.44.0 — 2026-07-03

### Asset ingest: image-only PDFs now RAG (visual-capture fallback)

Designed decks, Miro exports, and scans exported without a text layer used
to fail asset ingest with "No chunks generated (empty document?)" — hit
live on two IBX-5153 Keynote-exported decks. document-ingest's PDF parser
(repinned to 1p-component-library@8dc32a8) now falls back to faithful
per-page visual transcription via `visual-document-capture` (the module
already powering `cp workshop-synth` Stage 1) whenever pypdf extracts no
text; the transcriptions flow through the normal page-aware chunker, and
chunks carry `metadata.visual_capture = true`.

Requirements + knobs (all env, read at parse time):

- `ANTHROPIC_API_KEY` must be set in the ingest environment — without it
  (or with `INGEST_VISUAL_CAPTURE=0`) behavior is exactly as before.
- `INGEST_VISUAL_MODEL` — default `claude-sonnet-5` (validated faithful on
  the failing IBX-5153 deck).
- `INGEST_VISUAL_MAX_PAGES` — cost guard, default 60 (one vision call per
  page).

## v0.43.3 — 2026-07-02

### Fixed — `pull_project_source` query pulls actually see the Voyage key; plain pulls find older docs

Two follow-ups to v0.43.2's attempt at the same symptom:

- **Query-ranked pulls still failed with `VOYAGE_API_KEY environment variable
  not set`** even after v0.43.2 loaded the key into the environment. The
  embedder doesn't read the environment — it reads document-ingest's settings
  singleton, and unconfigured that falls back to `DefaultIngestSettings`, which
  has **no `voyage_api_key` field at all**. `pull_source` now wires
  `AssetIngestSettings` in via `_configure_pipeline_once()` (exactly as the
  ingest pipeline does) before constructing the embedder.
- **Plain (no-query) pulls returned "no source named …" for docs that exist.**
  The recency-ordered RPC read spans the whole project+account chunk pool, so a
  doc older than the newest `limit` (50) chunks never surfaced. On a miss the
  pull now widens once to 2000 chunks before giving up; returned chunks stay
  capped at the caller's `limit`.

Both verified live against `sap-5174`'s "Concur and SAP - All in with AI.docx"
(a Jun-17 ingest sitting below the top-50 recency window).

## v0.43.2 — 2026-07-02

### Fixed — `pull_project_source` resolves Voyage creds for query-ranked pulls

A query-ranked source pull embeds the query via Voyage, but the MCP tool
`pull_project_source` never loaded `VOYAGE_API_KEY` from the mc-2 `.env` the way
the spine-promote path does — so a local MCP session's ranked pull failed with
`VOYAGE_API_KEY environment variable not set` (surfaced while reading a
`meeting_synthesis` source back). It now calls `_load_ingest_creds` before
`pull_source` when a `query` is present (no-op without a query, since no
embedding happens then). Plain full-doc pulls were unaffected.

## v0.43.1 — 2026-07-02

### Fixed — deep-synthesis service poll handles the running/failed job states

Live smoke-testing the `meeting_synthesis` chain end-to-end (the first run with
the synthesizer's X-API-Key bypass actually live) surfaced a client-contract
mismatch: `call_synth_service` treated HTTP `400` as "job not ready" while
polling `/api/analyze/{job}/result`, but the service returns **`409`** ("Job
status: running") until completion — so every poll raised instead of waiting.
It now treats `409` as not-ready and keeps polling, AND distinguishes a
terminally **failed** job (409 with `"failed"` in the detail) — raising promptly
instead of spinning until the timeout. Verified end-to-end against the live
service: a transcript-only synthesis now completes and returns decisions,
segments, and open questions.

(Companion fix in the meeting-synthesizer service: `/api/analyze` now accepts a
`supplied_transcript` so the no-video "Deep synthesis" fallback works — that's a
service deploy, not a cp-engine change.)

## v0.43.0 — 2026-07-02

### Added — Deep multimodal meeting synthesis (the `meeting_synthesis` fidelity)

A third RAG fidelity above `meeting_summary` and `meeting_transcript`: a deep
synthesis produced by the meeting-synthesizer service, which reads the meeting's
video (slides/whiteboards off the frames) and — new — the **documents presented
in the meeting** (decks, Miro/whiteboard PDF exports), reconciling deck content
against what the room actually said.

- **`cp_engine/meeting_synthesis.py`** — `synthesize_meeting()` mirrors
  `promote_meeting_transcript` (engagement gate → guard → produce → ingest →
  stamp `meta.kind=meeting_synthesis` → stamp `fathom_meetings.synthesis_generated_at`
  → never-raises). Plus `discover_recording()` (best media candidate from the
  project's mc-2 ingest folder, human-confirmed — never silent), `call_synth_service()`
  (POST meeting-synth `/api/analyze` with X-API-Key + poll), and
  `synthesis_to_markdown()`.
- **Webhook `POST /api/meetings/synthesize`** — signed proxy target mirroring
  promote-transcript; optional `{media_url, documents}` body; falls back to a
  transcript-only synthesis when no video is supplied.

### Changed — workshop-synth Stage 1 uses the shared visual-document-capture module

`capture_worksheet` now delegates its native-PDF faithful-capture mechanics to
the reusable `visual-document-capture` component (extracted to
1p-component-library), passing workshop-synth's own transcript block as ambient
context so the cross-stage prompt-cache prefix stays byte-identical. Behavior-
preserving; the two synthesis stages (hypotheses, narrative) stay local.

### Deploy note

The webhook needs two new env vars at deploy: `SYNTH_SERVICE_URL` (default
`meeting-synth.1p.is`) and `SYNTH_SERVICE_API_KEY` (the synthesizer's
service X-API-Key). mc-2 migration 085 (`fathom_meetings.synthesis_generated_at`)
is already applied to prod.

## v0.42.2 — 2026-07-01

### Fixed — Slack digest close/resolve buttons resolve against the right sprint week

The attention-digest Slack buttons (close-ask / resolve-risk / snooze-*)
embedded only `verb|code|hash` in their payload — no sprint week. At click
time the webhook defaulted the sprint week to `date.today()`'s and searched
that week's sprint file for the hash. When a digest was rendered in one sprint
week and clicked in another (any rollover between render and click — e.g. a
Sunday digest clicked Monday, or a mid-week sprint cutover), the item lived in
the digest's week's file, not today's, so the hash wasn't found and the click
returned **"No matching item (already resolved or moved sprint)"** for a live
item. The button now carries the digest's `week_iso` as a 4th payload part
(threaded through the snooze-modal `private_metadata` round-trip too), and the
handler passes it to `execute_plan(week_iso=)` — which already supported it.
Buttons still in Slack with the old 3-part payload fall back to the current
week (unchanged behavior). The digest reports the week its items were actually
read from, including the previous-week fallback, so the embedded week always
matches the searched file.

## v0.42.1 — 2026-07-01

### Fixed — Exec Summary migration preserves all Quick Resume content

The v0.42.0 Quick Resume → Exec Summary migration read only the *single value
after each* of the four canonical labels (`**Last session:**`,
`**Current work:**`, `**Next up:**`, `**Blockers:**`) via a one-line regex. Real
tenant regions carry more than that, and the extra content was silently dropped
on migration: a fifth hand-added `**Last meeting:** <text>` field disappeared
entirely, and multi-line bullets under `**Next up:**` (dated commitments, owner
assignments — e.g. "share Round 2 with Infoblox", "email Dan Pearl") were
truncated to just the label-line value. Across the live tenant, 11 of 36
projects lost real active content this way.

The migration now parses the full region and preserves every line. Extra bullets
under any canonical field carry as additional bullets under that field (Next-up
continuation bullets stay under **Next up**). Non-canonical fields such as
`**Last meeting:**` fold into **Where it stands** as labeled bullets
(`- Last meeting: <text>`) rather than being dropped. Placeholder canonical
values still drop to header-only, so a region that had only placeholders and no
extra content still reads as unauthored. The migration remains idempotent (runs
once, then a no-op) and account-CP-guarded; only the currently-dropped content
is now retained.

## v0.42.0 — 2026-06-30

### Changed — sprint planning reads a structured, model-authored Exec Summary (replaces the stale Quick Resume scrape)

Sprint planning was "disappointingly vague" because the roll-up
(`cp prep-planning`) represented each project by scraping the *first
`**Current work:**` line* of `cp.md`'s Quick Resume — a field that was often
stale, empty/template, or thin. This release replaces that lossy *pull* with a
fresh *push*: each project's state is captured as a structured **Exec Summary**
authored by the model at wrap up (when the truth is freshest), and the roll-up
synthesizes across those.

**The Exec Summary region.** Project `cp.md` now carries an engine-managed
`## Exec Summary` region (markers `<!-- cp-engine:start exec-summary -->` /
`end`) with six fields — Objective / Status / Where it stands / Next up /
Blockers / Updates — plus `**Last session:**` and a dated `· updated <date>`
header. It replaces the old `## Quick Resume` state box in both the project and
initiative templates. The tenant-wide `weekly-cp.md` Quick Resume is a separate
surface and is unchanged.

**Migration is automatic on `cp sync`.** Every existing project/initiative
`cp.md` converts its old `quick-resume` region (marker-wrapped or the older
marker-less `## Quick Resume`) to the new `exec-summary` region in place, seeded
from the old Current work / Next up / Blockers values plus a dated
"migrated from Quick Resume" Update. Idempotent (runs once, then a no-op) and
content-preserving. Account `cp.md` files — whose Quick Resume is freeform
relationship prose, not a state box — are left untouched.

**The model authors; the engine plumbs.** The engine scaffolds, migrates, and
reads the region; the model writes the prose at `wrap up` (read the prior
summary → merge this session's changes → rewrite the six fields → append one
dated Update → roll off Updates older than ~4 weeks). **Auto-ingest no longer
writes project `cp.md` state at all** — per-meeting truth lands in the sprint
file only. Retired ingest verbs (`current_work` / `next_up` / `blockers`) are
recognized-and-ignored (not errored) so in-flight webhook plans during the
deploy window degrade gracefully.

### Added — `cp prep-planning --bundle`

A new output mode that emits the structured per-project material — each
project's full Exec Summary plus the deterministic metrics the engine already
computes (capacity binding, urgent flags, forward calendar, cross-cutting
decisions) — as raw material for the model to synthesize `_planning.md`
in-session (Focus list / decisions & blockers / cross-cutting patterns /
per-owner commitments) rather than a pre-rendered inventory. `/cp-prep` now
drives this bundle → in-session-synthesis flow. `--summary` and the default
doc render are unchanged.

### Fixed — exec-summary "authored" detection unified (prevents placeholder leakage)

The region readers need to tell an authored region from a fresh scaffold or a
just-migrated one. The shared primitives — region slice, "is authored" check
(a `_<…>_` placeholder field or bullet does not count, nor does the
auto-migration bullet), and the migration-bullet regex — now live once in
`render.py`, sourced from a single `EXEC_SUMMARY_MIGRATION_SUFFIX` constant, and
are used by `prep_planning` and `agenda`. (`summary.py` extracts a specific
field for the master-cp one-liner and keeps an equivalent per-field placeholder
check.) This eliminated two latent bugs a
duplicated implementation had hidden: a fresh-scaffold region reading as
"authored" (which would have spliced `- _<2-4 dense bullets…>_` placeholder text
into the planning doc), and `summary.py`'s Status extraction leaking the
`**Where it stands:**` label when the Status field was empty.

## v0.41.2 — 2026-06-30

### Fixed — backfill counts 'already embedded' as skipped, not failed

`cp meetings-backfill` classified a nested embed `ok:False` as `failed`, but
`embed_meeting_summary` returns `{ok:False, reason:"summary already embedded"}`
(and `"meeting has no summary"`) as BENIGN idempotent skips. So a re-run — or
`--all` after a scoped run — reported every previously-embedded row as a failure
and exited non-zero, crying wolf at cron/CI (live `--all` reported 25 such
"failures"). Benign reasons now count as `skipped`; only genuine embed outages
(Voyage/OpenAI down, "stamp matched no row") are `failed` and trip the exit code.

## v0.41.1 — 2026-06-30

### Fixed — resolve a project by its raw full_job_name (meetings tag form)

Fathom stores the raw `full_job_name` display string (e.g.
`"IBX 5167 DDI Platform Video"`) in `fathom_meetings.project_tags`.
`_resolve_project_id` matched only the slug code, the slugified on-disk id, or
the `<prefix>-<number>` form — so every tagged meeting failed to resolve and
`cp meetings-backfill` linked nothing (caught by live verification: ibx-5167's
24 meetings resolved 0). Adds an exact `projects.full_job_name` match branch.
Additive — all existing resolution forms still work.

## v0.41.0 — 2026-06-30

### Added — meetings as first-class, work-item-scoped sources (Phase 1: cp-engine backend)

Tagged Fathom meetings become a queryable, work-item-scoped inventory whose
summaries are always embedded into RAG and whose full transcripts can be promoted
on demand. Built on the existing mc-2 `fathom_meetings` table (no new table); the
substrate gap was *resolution + linkage*, not storage. Engagements only in v1
(initiative promotion is cleanly deferred). **Pairs with an mc-2 PR (migration 084
+ meeting-list UI) — co-merge, then release.** Migration 084 (adds `project_id`,
`initiative_id`, `work_item_id`, `work_item_confidence`, `summary_embedded_at`,
`transcript_promoted_at` to `fathom_meetings`) applies at deploy.

- **Resolution + linkage** (`cp_engine.meetings`). `resolve_meeting_project`
  maps a meeting's display-string `project_tags` to a `projects.id`;
  `link_meeting` orchestrates resolve → work-item assign → write link columns →
  embed summary, triggering the retag cascade when the project changes.
- **Two RAG fidelities, kept distinct by a `meta.kind` discriminator.**
  `embed_meeting_summary` embeds the summary (`kind=meeting_summary`, always,
  cheap); `promote_meeting_transcript` embeds the full transcript flattened from
  the `transcript` jsonb (`kind=meeting_transcript`, on demand, engagement-gated).
  Both reuse the v0.40.2-fixed ingest path; idempotent via a stable temp path
  keyed on `recording_id`.
- **Retag re-scope cascade** (`rescope_meeting`). Re-tagging a meeting moves its
  `fathom_meetings` row and its `rag_assets` rows to the new project by the
  scope-independent `(source_provider='fathom', source_file_id=recording_id)`
  key — no re-embed (chunks follow by `asset_id`), no ghosts, idempotent.
  `company_id` is only rewritten when provided (never nulled).
- **Work-item auto-assign** (`assign_work_item`). A confidence gate
  (threshold 0.75, inclusive) behind an injected classifier; below threshold the
  meeting lands unassigned ("needs assignment"). The LLM classifier is a later
  task — meetings currently land unassigned by default.
- **Flow change** (webhook `_perform_auto_ingest`). After the existing sprint-file
  ingest, each meeting is also linked + its summary embedded — once per meeting,
  additive and non-fatal (a failure never aborts the primary ingest), with
  `company_id` resolved from the meeting's own project.
- **Backfill** (`cp meetings-backfill [<code>] | --all`). Links + embeds existing
  tagged meetings (no Fathom API — reads existing rows); paginated (no silent
  >1000-row cap), per-row failure isolation, surfaces unresolved/failed rows,
  exits non-zero on failure.
- **Read tool** (`list_project_meetings` MCP tool + `cp_engine.project_sources`
  helper). Lists a project's meetings with `summary_embedded`/`transcript_promoted`
  flags; never returns the heavy transcript blob.

Note: re-tagging a meeting to its *current* project does not re-fire the webhook
(a fathom-meeting-sync trigger gap; workaround is unassign-then-reassign), so the
retag cascade fires only when the webhook actually re-runs — a deploy-time
dependency, not fixable in the engine.

## v0.40.2 — 2026-06-30

### Fixed — spine transcript promotion could not build the ingest pipeline

Two coupled defects made every transcript promotion fail with the misleading
downstream reason `stamp matched no row` (ingest never wrote a row, so the stamp
correctly matched nothing):

- **Promote path skipped pipeline configuration.** `_default_pipeline_factory`
  called `_build_pipeline` without first calling `_configure_pipeline_once`, so
  document-ingest's OpenAI client factory was never installed. The normal ingest
  path (`ingest_project_assets`) always configures first; the promote path now
  does too.
- **Ingest creds were never loaded into the environment.** The pipeline reads
  `OPENAI_API_KEY` / `VOYAGE_API_KEY` via `os.getenv`, but `_load_supabase_creds`
  loads only the SUPABASE_* keys. New `_load_ingest_creds(config)` exports the
  ingest keys (env first, then `<mc-2 clone>/backend/.env`; never clobbers an
  existing env value; no-op when no clone is configured). The MCP `set_spine_element`
  and `promote_spine_transcript` tools now call it before promoting. (The Railway
  webhook already has both keys in its container env, the same way it has the
  SUPABASE_* keys.)

Also fixes a latent test-isolation bug: `test_module_import_is_light` evicted
`cp_engine.sync_mc2` from `sys.modules` without restoring it, polluting later
tests that monkeypatch that module.

## v0.40.1 — 2026-06-29

### Fixed — transcript promotion on account-nested engagements

- **Spine transcript promotion now resolves the real working dir.** `promote_transcript`
  joined the bare project code onto the tenant root (`<tenant>/<code>/<rel_path>`), which
  misses every engagement — engagements live account-nested at `<tenant>/1p/<company>/<code>/`.
  Every `set_spine_element(..., important=True)` and `promote_spine_transcript` call on a real
  engagement therefore failed with `transcript file not found`, leaving the `important` flag set
  but nothing embedded into RAG. Promotion now locates the project's working dir via
  `_resolve_project_cp_path` (walking `1p/<company>/`, `firstpersonsf/`, `canonic/`) and roots
  `rel_path` there, falling back to the flat layout when no `cp.md` resolves. Regression test
  added.

## v0.40.0 — 2026-06-26

### Added — spine importance/note/done, transcript promotion, folder scan, ingest caching, dashboard

A batch of spine + ingest enhancements (cp-enhancements items 1–6), all backward-compatible.

- **Spine importance + note (item 1).** A first-class `important` flag and a standing
  `note` ("why this matters") on spine elements (`spine_substance.important`/`.note`,
  mc-2 migration 082). Importance sorts an element first in `list_spine_elements`,
  badges it, and gates transcript promotion; both carry forward across versions from
  the live row. New MCP tool `set_spine_element(code, key, important?, note?)`.

- **Derived "done" (item 2).** A read-through `done` on spine elements computed from
  the Gantt bar's completion (`estimator.schedule_items.done`) — no new state. ANY bar
  done ⇒ done (reuses `execution_status` Rule 1); three states (true / false / null for
  unbound). Surfaced read-only in `list_spine_elements` / `pull_spine_element`,
  fail-soft, no N+1.

- **Promote transcript to RAG (item 3).** Marking an element important embeds its source
  transcript into `rag_assets` (retrievable via `pull_project_source`). New idempotent
  MCP tool `promote_spine_transcript(code, key)` doubles as the retry door; importance is
  always set even if the embed fails. Check-before-write idempotency on
  `(project_id, source_file_id=est_item_id)`; engagements only (initiatives deferred).

- **Folder targeted scan (item 4).** `ingest_project_assets(..., only_folder=)` +
  `cp ingest-assets --folder` narrow the per-project allowlist to one folder
  (provably narrow-only — never reaches an unconfigured folder). The ingest webhook
  accepts a `folder` param.

- **Ingest caching (item 5).** Repeated `cp ingest-assets` runs skip unchanged files
  pre-download via a provider content-hash change-token stamped in
  `rag_assets.meta.change_token`, plus an in-process TTL folder-listing cache.
  `--no-cache` bypasses both; the `unchanged=N` count is surfaced in the run summary.

- **Spine dashboard webhook (item 6).** `POST /api/spine/promote-transcript` — async,
  HMAC-verified, run-tracked (`spine_promote_runs`, mc-2 migration 083) — lets the
  `/spine` dashboard trigger transcript promotion (clones the tenant via the shared
  `_cloned_tenant`, no leak). The dashboard ★/note/done surfaces + the promote proxy
  and card UI ship in mc-2.

## v0.39.2 — 2026-06-24

### Added — Supabase creds from 1Password (`op://` references)

A second user can now run the engine without cloning the mc-2 backend or pasting
a service key into a dotfile. The cred resolver (`sync_mc2._load_supabase_creds`)
gains a middle rung between the environment and the `mc-2/backend/.env` fallback:

- A `[supabase]` block in the gitignored `.cp-engine.local.toml` holds **1Password
  secret references**, not secrets — `url_ref = "op://<vault>/<item>/url"` and
  `service_key_ref = "op://<vault>/<item>/service_key"`. They resolve live via the
  1Password CLI (`op read`) at launch, so the key never lands on disk and the
  shared, committed `.mcp.json` (`cp mcp`) is untouched — no per-machine `op run`
  wrapper, no file churn between machines.
- Resolution order is **env → 1Password refs → mc-2 dotenv**. Environment
  variables still win (CI/Actions unchanged); a machine with no `[supabase]` block
  behaves exactly as before.
- **Configured refs are authoritative.** If `op` is missing, not signed in, or a
  reference fails to resolve, the resolver raises `BackendUnavailable` loudly
  (naming the cause) rather than silently falling through to the dotenv path — an
  explicit 1Password intent that can't be honored is an error, not a fallback.
  A half-configured block (one ref missing) is likewise a loud misconfig.
- Works for both `cp mcp` (the MCP server) and `cp sync` — one code path.

## v0.39.1 — 2026-06-24

### Fixed — `cp-sources` MCP resolves the canonical on-disk slug

The MCP resolver (`_resolve_project_id`) had no branch for the canonical on-disk
id since v0.35.0 — `slug_full_job_name(full_job_name)` (e.g. `ibx-5153-ai-campaign`),
the id `cp.md` Facts, the working-dir name, and `CLAUDE.md` all use. The number
sits in the *middle* of that slug, so it matched neither the exact-`code` branch
nor the legacy `<prefix>-<number>` bridge (which assumes the number is the trailing
segment). It resolved to `None` and the spine/source tools returned a bare `[]`,
indistinguishable from a genuinely empty spine — a silent false negative that
misreported IBX-5153's 35-element live spine as empty.

- **New resolution branch.** After the exact-`code` match and before the legacy
  `<prefix>-<number>` bridge, scan company-prefixed candidate rows and match
  `slug_full_job_name(full_job_name) == project_code`. Verified live: both
  `ibx-5153-ai-campaign` and `IBX-ai-campaign` now resolve to the same project id
  and surface all 35 live spine elements.
- **No more silent empties.** `list_project_sources` / `list_spine_elements`
  return a structured `{"note": "code '<x>' resolved to no project"}` on an
  unresolvable code instead of a bare `[]`, so an empty result can never again
  masquerade as an empty spine.

## v0.39.0 — 2026-06-24

### Added — initiatives are first-class in the spine / sources / ClickUp stack

Initiatives (internal workstreams: `mission-control`, `storyos`, …) live in MC-2's
`initiatives` table, parallel to `projects` but with no commercial apparatus. The
spine/sources/ClickUp stack assumed every owner was a `projects` row, so an
initiative's spine never worked end-to-end (a 2026-06-23 audit found the full gap
set). This release makes initiatives first-class across the cp-engine surface,
paired with mc-2 backend/frontend changes and live migrations 079–081.

- **Initiative-code resolution.** The `cp-sources` MCP resolver (`_resolve` /
  `_resolve_project_id`) falls back to the `initiatives` table and degrades to a
  `None` company id (initiatives have no Drive/Dropbox folders), so all spine MCP
  tools resolve an initiative slug. `cp sync` already mirrors initiative spines by
  uuid, so authoring into an initiative's spine now works end-to-end.

- **Canonical `layer` normalization.** `authored_element.canon_layer()` maps the
  MCP tool's lowercase `type` vocab to the spine UI's canonical TitleCase `layer`
  strings (e.g. `email`→`Email`, `source`→`Source material`), so a kind authored
  via MCP groups under the same layer the dashboard filters on. Mirror of mc-2's
  `authored_element` (kept in sync by the golden-vector parity test).

- **Owner-aware ClickUp round-trip.** `clickup_task_proposals` now carries either
  `project_id` OR `initiative_id` (mc-2 migration 081, `num_nonnulls = 1` CHECK).
  The propose writers (`webhook/clickup_propose._build_proposal_row`,
  `ingest._write_milestone`, `ingest._write_client_ask_task`) and the close-lookup
  (`webhook/main._lookup_proposal_by_clickup_task_id`) all select the correct owner
  column, so an initiative-sourced action item no longer FK-crashes on insert and
  resolves on close.

- **Source-ingest gate lifted for initiatives.** `active_ingestable_codes` includes
  active initiatives, and the `rag_assets` owner column is written via
  `_owner_filter` / `ProjectFolders.is_initiative`. NOTE: the resolve→folders→write
  half is intentionally inert until a future task adds initiative folder columns +
  an initiatives-table resolve path; the seam is documented.

- **All 7 `cp-sources` MCP tools documented in the `CLAUDE.md` template** (spine
  read/write + source list/pull/fetch), not just `list/pull_project_source`.

## v0.38.1 — 2026-06-22

### Parity — `build_create_rows` carries `sources`

`authored_element.py` is a behavioral mirror of mc-2's `backend/src/lib/authored_element.py`. mc-2 PR #99 (spine document routing) added an optional `sources` param so a routed `rag_asset` records a structured source on the created element. Synced the same param here (`build_create_rows(..., sources=None)` → `"sources": list(sources or [])`) so the golden-vector parity guard stays valid in both repos. Default `[]` preserves all existing behavior — no functional change in the cp flow.

## v0.38.0 — 2026-06-22

### Added — spine-read MCP tools (`list_spine_elements` + `pull_spine_element`)

The `cp-sources` MCP server could WRITE spine elements (`create_spine_element`,
`add_spine_version`) and READ ingested sources (`list/pull/fetch_project_source`),
but had **no way to READ spine elements back** — so reading a project's distilled
memory (e.g. an ingested client email, an authored deliverable held in
`spine_substance`) forced a drop to raw Supabase SQL. Two read tools close the gap,
mirroring the existing thin-wrapper pattern (resolve code → `project_id` → delegate
to a pure fn → never raise, structured error note on failure):

- **`list_spine_elements(project_code)`** — the live-element index: per element
  `est_item_id, framing, layer, binding, status, serves_count, body_len`. Never the
  body itself (only its length), per the no-large-columns-in-lists rule.
- **`pull_spine_element(project_code, key)`** — one live element's full body +
  context (layer, binding, serves, sources, version_label). `key` resolves by exact
  `est_item_id` first, then case-insensitive title (`framing`) substring; refuses to
  merge distinct elements (ambiguous → note), mirroring `pull_source`'s discipline.

Pure functions `list_spine` / `pull_spine` land in `project_sources.py`
(transport-agnostic, explicit columns, read LIVE versions only). Tool count 5 → 7.

## v0.37.0 — 2026-06-20

### Fixed — spine mirror no longer doubles/strands on a code change

The v0.35.0 canonical-id rename exposed a parallel bug in the spine mirror: rows
key on `project_code` (which is embedded in the row id), so a `full_job_name`/code
change **doubled** estimate/context rows (re-mirrored under the new code, old ones
orphaned) and **stranded** authored rows (MC-2-owned, never reaped, never
re-written) under the dead code. Result: `cp where <new-canonical-code>` returned
"No spine substance" — the spine was reachable only by the dead old code.

Fixed by keying the whole spine layer on the stable `project_id` uuid:
- **Reaps key on `project_id`** — `sync_spine_substance` (estimate) and
  `sync_spine_context` now see all of a project's rows regardless of code, so
  old-code duplicates are reaped on rename.
- **Authored rows re-home** — `_rehome_authored_codes` updates a drifted authored
  row's `project_code` + `id` prefix (a rename, not a delete; bodies preserved).
- **Authored reverse-mirror keys on `project_id`** — finds the re-homed rows.
- **Snapshots re-home too** — `spine_snapshots` gains `project_id` (mig 078) and
  `_rehome_snapshot_codes` rewrites its `id`/`deliverable_id`/`project_code`.

Running the fixed sync self-heals an already-split project (no separate script).

### Companion
mc-2 migration 078 (`spine_snapshots.project_id`).

## v0.36.0 — 2026-06-20

### Fixed — `full_job_name` edits no longer strand project content (uuid-anchored dir location)

v0.35.0 keyed working-dir + sprint-file names on the `full_job_name` slug. Editing
`full_job_name` in MC-2 changed the id, and sync's dir-location (matched by code
string) couldn't bridge old→new — so the old dir with any hand-added content was
swept to `inactive/<old-slug>/` and a fresh empty dir scaffolded. That gotcha is
now fixed.

- **`MC-id:` frontmatter stamp** — each project `cp.md` carries its MC-2 row uuid
  (the stable identity, immutable across name/code edits). Emitted by the
  templates for new scaffolds; **auto-stamped into existing CPs on sync**
  (self-healing — edits frontmatter only, body byte-preserved).
- **`_find_project_dir` is uuid-first** — locates a project's dir by its `MC-id`
  stamp regardless of the (possibly drifted) dir name; falls back to the legacy
  code-name match for unstamped/uuid-less items.
- **Drift becomes a rename** — a found-but-drifted dir is `git mv`d to the new
  code-slug, and its sprint files are renamed across all weeks (shared helper with
  the migration script).
- **uuid-aware staleness** — `_deactivate_stale_cps` keeps a dir live when its
  stamped uuid is in the live set, so a drifted dir is never swept to `inactive/`.

Net: editing `full_job_name` now cleanly renames the dir + sprint files, content
and git history preserved. Closes the gotcha noted in v0.35.0.

## v0.35.1 — 2026-06-20

### Fixed — `cp sync` re-splices the `project-facts` region (cp-engine#15, for real)

The project `cp.md` Facts table (Code / Status / Stage / Budget / Owner / Client /
Last touched) was written once at scaffold and never refreshed — so after v0.35.0
changed the canonical id, the Facts `Code` still showed the old `ibx-5192`. `cp sync`
now re-renders and re-splices `project-facts` from the live `ProjectState` on every
sync (the region is purely engine-managed). Guarded to skip files lacking the
region marker, so legacy/hand-crafted CPs are never full-rewritten. Keeps Status /
Stage / Budget etc. fresh too, not just Code.

## v0.35.0 — 2026-06-20

### Changed — canonical project id now derives from MC-2 `full_job_name`

A project's canonical id (`ProjectState.code`) was a derived `<company>-<number>`
(`ibx-5192`). It is now the slugified MC-2 `full_job_name`
(`ibx-5192-platform-sales-readiness-summit`) — company + number + description in
one id, sourced from MC-2's human-authored field rather than a derivation.

- **`slug_full_job_name`** (state.py) — `"IBX 5192 Platform Sales Readiness Summit"`
  → `ibx-5192-platform-sales-readiness-summit`. Verified unique + filename-safe
  across all active projects.
- **`_engagement_canonical_id`** (sync_mc2.py) now slugifies `full_job_name`,
  falling back to `<company>-<number>` only when it's empty. Also fixed the
  sprint-allocations join SELECT, which lacked `full_job_name` (would have silently
  mis-joined capacity data).
- **`slack.py` channel-map** now uses the same `_engagement_canonical_id` (it had
  independently built the old `<company>-<number>` form) — keeps the weekly Slack
  digest routing to the right projects.
- **`dir_slug`** returns the slugified code only (the code is now the full
  descriptive slug, so appending the name would double it).
- Resolves cp-engine#15 (Facts `Code` showed a number-derived id, not the
  canonical one).

**`projects.code` and `full_job_name` in MC-2 are read-only** — never written. The
asset layer is untouched (resolves by `projects.number`, still in the slug).

### Migration

Companion one-time tenant migration renames sprint files
(`ibx-5192.md` → `ibx-5192-platform-sales-readiness-summit.md`) across all weeks
for active projects; working dirs already match the new slug. **Note:** after this
ships, editing a project's `full_job_name` in MC-2 changes its id, which sync's
dir-locate (keyed on the code) can't bridge — the old dir with any hand-added
content is swept to `inactive/<old-slug>/` (non-destructive; needs a manual
`git mv`). Same class as the existing company-rename resync gotcha. Future fix:
key dir-location on the stable MC-2 row uuid.

## v0.34.0 — 2026-06-20

### Added — Source live-link layer (re-fetch any ingested source on demand)

Asset ingest used to embed a source doc's text and then discard any way back to the original binary (`rag_assets.file_path` was a dead `/tmp` scratch path; `url` was null). This release makes every ingested source re-fetchable on demand and gives it a durable web link.

- **New `rag_assets` columns** (mc-2 migration 077): `source_provider` (`drive`|`dropbox`), `source_file_id`, `source_path`. Stamped at ingest time in the existing post-`ingest_file` UPDATE (`_stamp_scope`), read back to reconstruct a `FileRef`.
- **`_source_url`** now returns a durable link instead of `None` — Drive `/file/d/<id>/view`, Dropbox a **team-only** shared link. **Fail-closed**: if the deployed Dropbox connector can't prove team-only support (no `team_only` param in its signature), the link is dropped rather than risk publishing a public link to client material.
- **`fetch_source` + `fetch_project_source` MCP tool** — download an ingested source's original binary to a local path Claude can `Read` (e.g. a `.pptx` to inspect hidden slides). Wiring-only tool over the pure function; never throws.
- **Backfill** (`scripts/backfill_source_coords.py`) recovers coords from legacy dead temp paths (the Dropbox/Drive id is encoded in the `dropbox-id_<id>`/`drive-<id>` temp dir). Drive recovers losslessly; Dropbox recovers the id (re-fetchable via the fallback below). Dry-run by default, column-existence preflight, no silent caps.
- **Dropbox fetch-by-id fallback** in `download_file` — when a Dropbox `FileRef` has an id but no `path_display` (backfilled rows), fetch via `files_download(path="id:<id>")`. Lets historic Dropbox rows re-fetch without re-ingest.

Verified end-to-end live on IBX-5192: backfilled rows → `fetch_source` pulled the real 7.4 MB v6 `.pptx` from Dropbox by id and exposed its hidden/visible slide flags.

### Companion changes
mc-2 migration 077 (columns); social-builder-app `DropboxConnector.get_shareable_link` gained an optional `team_only` param (members-only links, re-verified on both create and reuse paths).

## v0.33.0 — 2026-06-19

### Added — `cp spine-recover` (project_code drift recovery)

`cp spine-recover CODE` re-homes a project's legacy spine elements into the current capture-loop authored format under the canonical `<company>-<number>` code. It exists because a slug-drift — the now-retired `cp spine-migrate` wrote rows under the working-dir slug while `cp sync`/`cp where` use the canonical code — could strand a project's real distilled memory in a dead transitional shape under a second `project_code`.

The command reads the legacy capitalized-layer disk files (the authoritative copy), **re-distills source-backed elements from their original `rag_assets`** (a fresh, full-fidelity refresh — matched by the `source:` filename), **carries synthesis elements verbatim** (re-distilling cross-cutting work degrades it), and authors them as `origin='authored'` context rows. **Dry-run by default**; `--apply` writes; a failed/empty re-distill falls back to the verbatim body (never empties an element); estimate-bound elements are flagged `needs_rebind` in the report. Used to recover IBX-5153 (32 elements) end-to-end.

### Removed — `cp spine-migrate`

Retired — it could no longer run (read the `spine_elements` table, dropped in mc-2 migration 072) and was the source of the drift above. Replaced by `cp spine-recover`. The `placement_rule` helper (used only by it) was removed too.

### Companion MC-2 changes

The `/api/spine/projects` list now flags each summary with `split: true` when one `project_id` has `spine_substance` rows under more than one `project_code` — the early warning the IBX drift lacked. Canonical-code docstrings corrected (`<company>-<number>`, not the dir-slug).

## v0.32.1 — 2026-06-19

### Fixed — dropped the dead `spine_elements` mirror call (PGRST205 log noise)

`cp sync` called `sync_spine_elements` on every project, but the `spine_elements` table was dropped (mc-2 migration 072) once the `spine_elements` → `spine_substance` merge completed. The call hit a missing table, threw a `PGRST205` error caught by the best-effort wrapper, and logged a `spine-element mirror skipped` warning per project on every sync. Removed the call + import; the substance/context mirror is its replacement. (No behavior change — the mirror was already a no-op-via-exception.)

## v0.32.0 — 2026-06-19

### Spine capture loop — author elements into the spine from anywhere

You (or an LLM, or the MC-2 UI) can now author a spine element **directly into MC-2** instead of writing a local markdown file. The element is live immediately, versioned, and reachable by any LLM that reads the spine; the repo `spine/` markdown becomes a generated mirror. This ships steps 1–3 of the capture-loop design (the LLM-writes-to-spine loop).

#### Added — the write engine + MCP write tools

- `authored_element.build_create_rows` / `build_version_rows` — pure row-builders for authored spine elements. Authored elements live in `spine_substance` with `origin='authored'`, `placement='context'`, the element type in `layer`, and a synthetic `_authored/<slug>` `est_item_id` so an unbound element (an Email, a free Synthesis) still carries full version history. `serves` optionally binds it to estimate work-items.
- `create_spine_element(project_code, label, type, body, serves)` and `add_spine_version(project_code, element_id, body, version_note)` — MCP tools (alongside the existing `list_project_sources` / `pull_project_source`) so Claude writes elements from any session. `create` guards against silently clobbering an existing element; `add_spine_version` supersedes the prior live version via a targeted status update (never rebuilding the prior row) and carries a `version_note` ("what changed").

#### Added — origin-aware bidirectional `cp sync` (the reverse-mirror)

`cp sync` is now bidirectional by origin. `distilled` rows mirror disk→DB and reap-if-missing as before. `authored` rows mirror **DB→disk**: sync renders each authored element to `spine/_authored/<slug>.md` (round-trip-parseable, idempotent), **never reaps** an authored row for a missing file (MC-2 owns it), and **read-skips** `spine/_authored/` so generated mirrors never flow back disk→DB. A shared `is_skipped_spine_dir` predicate keeps the substance loader and the inbox iterator from drifting.

#### Companion MC-2 changes (migrations 074 + 075)

`spine_substance.origin` (`distilled` | `authored`) and `spine_substance.version_note`. A `POST /api/spine/{code}/element` endpoint authors/versions elements from the UI (mirrors the pure builder locally, per the `execution_status.py` convention, to avoid coupling the backend to a fresh cp-engine release).

## v0.31.1 — 2026-06-19

### Fixed — cp-sources MCP resolves the working-dir id to the canonical project

`list_project_sources("ibx-5192")` returned `[]` even when the project's assets were correctly ingested. cp-engine derives a project's working-dir slug + `cp.md` Facts id as `<company>-<number>` (`ibx-5192`), but `projects.code` is a company-prefixed SLUG for nearly every project (`IBX-platform-sales-readiness-summit`, `GGL-activation`). The MCP `_resolve` did an exact `projects.code` lookup, so the working-dir form never matched.

`_resolve_project_id` now bridges both forms: exact `projects.code` first, then a `<companyprefix>-<number>` fallback matching `companies.code` (case-insensitive — `companies.code` is stored uppercase) + `projects.number`. So `ibx-5192`, `IBX-5192`, and the slug code all resolve to the same project. The `_sources.md` manifest path already keyed on `project_id` correctly; its stale "0 docs" simply needed a re-sync.

## v0.31.0 — 2026-06-19

### Spine UI edits survive `cp sync`

The MC-2 `/spine` card dashboard lets a human reclassify an element's **layer**, bind its **serves** (which work-item it feeds), and **archive** it. Those edits write the `spine_substance` row *and* mark the field `confirmed` in `field_states`. Before this release, the next `cp sync` clobbered `layer`/`serves` from the markdown frontmatter and reset `archived` to its disk default — silently reverting the human's correction.

#### Changed — `layer` / `serves` / `archived` are now confirmed-wins tracked fields

`_SUBSTANCE_TRACKED_FIELDS` extends from `(framing, body, status)` to also include `layer`, `serves`, and `archived`. Sync now runs them through the same one-way reconcile door as the distilled content: a **confirmed** MC-2 value wins and a divergence raises a `review_flag` instead of being overwritten; an **unconfirmed** field still tracks disk as before (normal sync is unchanged). The orphan-reap path also protects a row confirmed only on one of these fields — it is flagged source-missing, never deleted.

`serves` compares **order- and type-insensitively** (both sides normalized to a sorted string list) so a reordered or list-vs-tuple representation never raises a false drift flag.

#### Added — `archived` on the substance model

`WorkItemSubstance` gains `archived: bool` (default `False`), parsed from frontmatter and emitted on round-trip **only when `True`** (default-false files round-trip byte-for-byte). Requires the companion MC-2 migration `073` (`spine_substance.archived`).

## v0.30.0 — 2026-06-18

### Estimate-as-spine: a grounded "where are we / what's next"

The goal: when you work in CP, get a grounded answer about where a project stands and what's next — every claim traceable to its source, not a muddy synthesis. This release makes the live MC-2 **estimate** (the SOW: phases → activities/deliverables, drawn from the `estimator` schema) the project's trustworthy execution spine, and re-founds the spine's distilled substance on it.

#### Added — `cp where CODE`

A grounded where-are-we/what's-next answer in the terminal. Reads the project's default estimate, its schedule, and its bound substance; for each work-item shows its derived execution status + a one-line basis + a `[source]` marker, grouped by phase, with schedule-native events (milestones/holidays) listed separately. Every factual clause is source-tagged — the target conversation made runnable.

#### Added — derived execution status (`execution_status.py`)

`derive_status` computes a work-item's status — **done · active · next · flag · done?** — from signals the spine already has, so it can't rot: the project's maintained schedule bars (kept current as work happens), whether the item has live substance, and recent substance activity. The only non-derived signal is an explicit one-click `done` toggle on the Gantt bar. A past-ended bar with nothing captured surfaces as **flag** ("ended but nothing captured") — the honest answer where a naive system would guess "done".

#### Added — the schedule reader (`estimate.py`)

The estimate is now time-aware: reads `public.projects.start_date` and the `estimator.schedule_items` bars, maps relative weeks → calendar dates (`week_to_date`), and joins bars to their work-items (`schedule_for_item` / `native_schedule_events`). Schedule bars carry a nullable link to the scope work-item they place in time; milestones/holidays/markers stay schedule-native.

#### Changed — the spine is one store now (the merge)

The legacy layer-organized `spine_elements` store is **merged into `spine_substance` and retired.** `spine_substance` gains two axes: `placement` (`item` = nests under an estimate work-item · `context` = the cross-cutting rail) and `layer` (the artifact-kind axis powering a by-layer view). `cp spine` now reads `spine_substance`. `cp spine-migrate CODE [--dry-run] [--mc-project-id UUID]` converts legacy elements into substance with proposed placement (cross-cutting → context; deliverables → best-guess binding for human confirm), hydrating each body from its on-disk `rel_path`.

#### Companion MC-2 changes (migrations 068–072)

Schema + UI shipped alongside in MC-2: schedule↔work-item link + `done` flag, `placement`/`layer`/`serves` on substance, the `/spine` outline endpoint computing status + scheduled dates (a parity-verified mirror of `derive_status`), the status-badge + by-layer + native-events UI, and the `spine_elements` drop. Tenants should run migrations 068–072.

## v0.29.0 — 2026-06-17

### Added — `cp workshop-synth`: workshop synthesis pipeline

The strategist's post-workshop move — combine the filled-in Miro worksheets with the meeting transcript into "what we now understand" — made into a 3-stage pipeline, each stage a reviewable artifact:

1. **Capture** — each worksheet PDF is read by Claude *natively as a document* (vision), preserving columns, section headers, every sticky's text, color/selection (green = chosen), and author tags — a faithful transcription, not an interpretation. A multi-page Miro export is split per page (each page = one board → its own artifact).
2. **Hypotheses** — per board, the patterns/learnings it reveals.
3. **Narrative** — across all hypotheses, the cross-workshop synthesis (through-lines, tensions, decided vs open).

The full workshop transcript is ambient context at every stage (read directly — not the vector store), and is prompt-cached so a multi-board run pays for it once rather than per call. Per-worksheet best-effort: one bad board doesn't abort the run. Outputs land in `<project>/workshop-synthesis/<date>/`.

`cp workshop-synth CODE [--worksheets PATH...] [--transcript PATH] [--out PATH]` — defaults to the project's `workshop-worksheets/` dir and the newest persisted transcript (see v0.28.3). Adds a direct `pypdf` dependency. The completed-worksheet captures double as reusable gold-standard exemplars.

## v0.28.3 — 2026-06-17

### Added — full meeting transcripts persisted per project

The webhook already fetched the full transcript (and staged it to a temp path to ingest from), but only the distilled Retrospective was persisted — the verbatim text lived only in Fathom. Now every meeting ingest also writes the full transcript to `<project>/meeting-transcripts/<date> <title>.txt`, committed with the auto-ingest. Best-effort (a persist failure never aborts ingest). A meeting that writes no sprint-file bullets but persists a transcript now produces an attributed `transcript only` commit rather than a no-op. Filenames slice the date off the ISO timestamp and walk to a ` (N)` suffix on a same-day/same-title collision (distinct meetings never clobber; re-ingest of the same meeting is idempotent). This makes full transcripts durable local project sources — and is the input for the forthcoming workshop-synthesis pipeline.

## v0.28.2 — 2026-06-17

### Fixed — asset ingest created duplicate rows for the same file across sources

The asset-ingest pipeline dedups only on `(project_id, file_path)`, so the *same file content* arriving at a different path created a duplicate `rag_assets` row. Two real cases hit this: a doc present in both the project's Google Drive AND Dropbox folder (ingested once per source), and a file copied within a single Dropbox folder. Live audit found 12 such pairs on IBX-5153 and 1 on sap-5174.

`ingest_project_assets` now runs a content-hash pre-check before parse/embed/insert: it computes the file's SHA-256 (matching the pipeline's own `file_hash`) and, if an active asset with that hash already exists in the project **at a different path**, skips the file and counts it as `deduped`. Same-path matches are left to the pipeline (its skip-vs-new_version province). The pre-check fails safe — any query error degrades to the prior behavior (ingest anyway), logged to stderr.

- New `IngestRunResult.deduped` counter, distinct from `skipped` (pipeline's same-path unchanged-file skip) and `skipped_shortcuts`. Surfaced in the `cp ingest-assets` summary lines, the `--all` aggregate, and the webhook's `asset_ingest_runs` patch.
- Requires mc-2 migration **067** (`asset_ingest_runs.deduped` column + a partial index on `rag_assets (project_id, file_hash) WHERE status='active'` so the per-file pre-check is a direct lookup). Applied live.

This prevents *new* cross-path duplicates; pre-existing duplicate rows are cleaned up separately.

## v0.28.1 — 2026-06-16

### Fixed — `cp mcp` couldn't find the tenant root from a project subdir

The `cp-sources` MCP server (`cp mcp`, launched by Claude Code via the tenant-root `.mcp.json`) inherits the cwd of whatever directory the session opened in — frequently a project subdir like `1p/infoblox/ibx-5153-ai-campaign`, not the tenant root. `_resolve()` loaded config from `Path.cwd()`, and `config.load()` requires `.cp-engine.toml` to sit exactly in that dir (no upward walk), so every source-listing/pull call from a project subdir failed with `No .cp-engine.toml at <subdir>. Not a tenant repo?`.

The server now resolves the tenant root by walking UP from cwd (reusing the existing tenant-root walk-up, promoted from `capture_session._find_existing_tenant_root` to the public `find_tenant_root`), falling back to cwd when no config is found so the downstream loader still raises its own clear error. The MCP source tools now work from any working dir within the tenant.

## v0.28.0 — 2026-06-16

### Added — ambient project sources (`_sources.md` manifest + `cp-sources` MCP server)

A project's ingested source documents (Drive/Dropbox briefs, decks, research pulled into the `rag_assets` store) were reachable only by hand-written queries. This release makes them ambient — visible to a session and pullable into reasoning during strategy/deliverable work.

- **`_sources.md` manifest** — `cp sync` regenerates an auto-managed manifest in each project working dir listing every ingested doc with a one-line summary. A glance tells a session what source material exists for the project. Both project-scoped and shared account-scoped docs (company-wide, promoted via `cp promote-asset`) are listed.
- **`cp-sources` MCP server** (`cp mcp`, stdio FastMCP) — two tools:
  - `list_project_sources(project_code)` — the live list of a project's ingested docs.
  - `pull_project_source(project_code, doc_title, query?)` — a doc's full text + citation by title; pass `query` to rank chunks by relevance instead of full-doc order.
- **Engine-managed `.mcp.json`** — sync now installs a tenant-root `.mcp.json` registering the `cp-sources` server (idempotent merge that preserves any tenant-added servers, same discipline as the `.claude/settings.json` SessionStart-hook merge). The CLAUDE.md template gains a "Project sources" section so sessions know the manifest + tools exist.
- Adds the `mcp` dependency.

## v0.27.6 — 2026-06-16

### Fixed — asset ingest skips non-ingestable shortcut files

Shortcut/pointer files (`.url`, `.lnk`, `.webloc`) carry no ingestable content — a `.url` is a link, not a document. They were downloaded, handed to the parser, rejected ("No parser available"), and counted as ingest *failures* (noise). They're now skipped BEFORE download and counted in a new `skipped_shortcuts` field, surfaced via a `source: "skip"` source_note ("skipped N non-ingestable shortcut file(s)") rather than polluting the failure count. No DB/migration change — the note rides the existing `source_notes` channel. Requires a webhook redeploy.

## v0.27.5 — 2026-06-16

### Added — per-project asset-ingest folder allowlist

Asset ingest swept every file in a project's Drive + Dropbox tree (SAP-5174's first run found 68 files, mostly internal docs). Projects can now restrict ingest to curated subfolders via a `projects.asset_ingest_folders text[]` allowlist (added by an MC-2 migration; cp-engine reads it NULL-safely).

- **Match rule:** a file is ingested if any FOLDER segment in its path contains an allowed name (case-insensitive substring), so `['Client Assets']` matches `01 Client Assets`, `Client Assets v2`, etc., but never a file merely *named* "Client Assets". Empty/whitespace allowed names are stripped on read (an empty string can't silently match everything).
- **Back-compat:** an unset/empty allowlist ingests the whole tree (today's behavior).
- **Zero-match flags loudly:** if a non-empty allowlist matches nothing, the run completes with 0 created and a `source_note` ("matched 0 of N — check folder names") rather than silently ingesting everything.
- **Drive support:** the Drive tree-walk now records each file's folder breadcrumb (it previously discarded it — only Dropbox carried a path). The filter applies to both sources.

Resolved server-side in `resolve_project_folders_by_id`, so the webhook contract is unchanged. The filter outcome rides the existing `source_notes` channel (the Ingest-assets button already renders it). Requires a webhook redeploy + the MC-2 migration.

## v0.27.4 — 2026-06-16

### Changed — component-library deps moved to FirstPersonSF + Drive auth from env

The three pinned component-library deps (`document-ingest`, `cloud-storage`, `resilience`) now point at **`FirstPersonSF/1p-component-library`** (the repo was transferred out of Canonic-OS and renamed) and are unified on a single release commit, so the `resilience` cloud-storage depends on matches the `resilience` cp-engine installs directly (the prior mismatched commits — `5efa7e1` vs `714acd9` — were a latent install hazard).

The bump also carries a Drive-connector fix: `GoogleDriveConnector` now reads the service-account JSON from a `GOOGLE_SERVICE_ACCOUNT_JSON` env var (via `from_service_account_info`), so Drive auth works on the webhook's Railway deploy where there's no file to point `GOOGLE_SERVICE_ACCOUNT_FILE` at. This unblocks the asset-ingest button's Drive source. The library also gained proper editable-package test setup + CI for the cp-engine-consumed surface (no behavior change to cp-engine). Requires a webhook redeploy.

## v0.27.3 — 2026-06-16

### Fixed — asset ingest resolves the project by id, not a parsed code-number

Asset ingest resolved the MC-2 project by extracting a number from the code (`re.search(r"(\d+)")` → `projects WHERE number=…`). The post-restructure slug codes don't carry the number — `SAP-vision-update-2026` parses to **2026** (a year) but its number is **5174**, and `IBX-ai-campaign` (5153) has no digits at all — so resolution failed with "no MC-2 project resolved". Adds `resolve_project_folders_by_id` (looks up `projects.id` directly) and threads an optional `mc_project_id` through `ingest_project_assets` and the webhook `POST /api/assets/ingest`. The MC-2 button passes the authoritative project id, so no number-parsing is involved. The by-code/by-number path is kept as back-compat (bare-numeric codes + the CLI). Requires a webhook redeploy.

## v0.27.2 — 2026-06-16

### Fixed — asset ingest from the webhook (cwd-config crash)

The v0.27.1 webhook `/api/assets/ingest` crashed in prod with `No .cp-engine.toml at /app. Not a tenant repo?`. `ingest_project_assets` resolves Supabase creds lazily via `cp_config.load(Path.cwd())`, but the webhook's container cwd (`/app`) holds no tenant checkout. The webhook already has `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` in its environment, so `_run_asset_ingest` now passes them straight through as `supabase_url`/`supabase_key` — the cwd-config path is never reached. Adds a fast-fail guard (missing env → run recorded `failed` with a clear message instead of the opaque config error). Requires a webhook redeploy.

## v0.27.1 — 2026-06-16

### Added — Asset ingest from MC-2 (webhook side)

Asset ingest (Drive/Dropbox docs → embedded `rag_assets`) was CLI-only and aborted the whole run if the first source failed to authenticate — useless from a button where the user can't fix creds. This release adds the webhook half of an MC-2 **Ingest assets** button.

- **`list_files` resilience** (`asset_ingest.py`): each storage source (Drive, Dropbox) is now isolated — a per-source failure (e.g. missing service-account creds) records a `source_note` and continues to the other source instead of aborting the run. The note carries the exception *type* and the full traceback goes to stderr, so a real bug is distinguishable from a dead-creds skip. `list_files` returns `(refs, source_notes)`; `IngestRunResult` carries `source_notes`.
- **`POST /api/assets/ingest`** (cp-engine-webhook): HMAC-signed, fire-and-forget. Inserts an `asset_ingest_runs` row (`status=running`), returns 202 immediately, runs `ingest_project_assets` in a background task (`asyncio.to_thread`), and updates the row to `done`/`failed` with counts + `source_notes` on completion. No tenant clone, no git push — asset ingest writes only `rag_assets` + the runs table.

Pairs with MC-2 migration 065 (`asset_ingest_runs`) + the MC-2 proxy/status endpoints + the project-page button. The webhook runs where the Drive/Dropbox/embedding creds already live, so the button needs no local credential setup. Requires a webhook redeploy.

## v0.27.0 — 2026-06-16

### Added — Spine estimate-binding (the spine is re-founded on the live estimate)

The spine no longer carries free-floating pointer-stubs (the v0.26 per-field grid that failed on first real use). It is re-founded on the project's **live default estimate** (the `estimator` schema's phases → activities/deliverables that drives the client portal): each estimate work-item *is* a thread, and its distilled, versioned **substance** binds to it. Substance is markdown source-of-truth on disk, mirrored to MC-2; git is the version history.

- **Estimate reader** (`estimate.py`): `fetch_estimate(client, mc_project_id)` reads the live default estimate over PostgREST (`.schema("estimator")`) into frozen `Estimate`/`EstimatePhase`/`EstimateItem` dataclasses.
- **Versioned substance model** (`substance.py`, `spine_context.py`): per-item substance files (`spine/<phase>/<item>.md`) with frontmatter binding `est_item_id`, and `## v<N> — date · status` version sections (framing brief + sources + distilled body; exactly one `live`, the rest `superseded`). Project-level context elements (`brief|agreement|stakeholder|decision|source`) with optional links to estimate items.
- **MC-2 mirror** (`spine_substance_sync.py`): `cp sync` reconciles substance + context to the `spine_substance`/`spine_context` tables per project (reconcile-don't-clobber, reusing the spine_elements field-state pattern). Bindings reconcile against the estimate: a missing item → `orphaned` (flagged, never deleted), not lost.
- **Ingestion inbox** (`spine_inbox.py`, `cp spine-frame`): the Fathom webhook writes a *proposed* card (raw-faithful distillation + a best-guess estimate item) to `spine_inbox` instead of writing substance directly. A human supplies a framing brief and promotes the card → a directed re-distillation under that framing becomes a new live version. The webhook path is kill-switchable (`SPINE_INBOX_ENABLED`).
- **One-click promote endpoint** (`webhook/main.py` `POST /api/spine/promote`): the server-side equivalent of `cp spine-frame`, signed like the other webhook endpoints. It clones the tenant, runs the directed distill, writes the markdown, mirrors, commits + pushes, and flips the card to `promoted` **only after a successful push** (the push is the commit point — a push failure leaves the card un-promoted for safe retry, no data loss). MC-2's verification UI calls this through a thin signed proxy so a browser click lands a version directly in the tenant repo.

Requires MC-2 migrations 063 (`spine_substance` + `spine_context`) and 064 (`spine_inbox`). The MC-2 spine UI ships the estimate-outline view (substance inline, version history, per-version confirm) + the framing inbox, replacing the per-field grid.

## v0.26.0 — 2026-06-15

### Changed — Shell → Spine rename (no behavior change)

The structural backbone has been called the *spine* throughout the design ("the authoritative spine"); the code, CLI, and data still said *shell*. This release renames everything to *spine* so the metaphor is consistent and the forthcoming Mission Control verification UI is born as "Spine" rather than shipping as "Shell" and renaming later. Purely mechanical — no behavior changes; the full suite stays green at 1018 passing.

- **Modules:** `shell.py → spine.py`, `shell_sync.py → spine_sync.py`, `shell_sweep.py → spine_sweep.py`, `shell_snapshot.py → spine_snapshot.py`, `shell_sources.py → spine_sources.py`, `shell_stats.py → spine_stats.py`.
- **CLI:** `cp shell → cp spine`, `cp shell-stats → cp spine-stats` (no back-compat alias — single operator). `cp sweep`/`cp snapshot`/`cp snapshots` keep their names.
- **Symbols:** `ShellElement → SpineElement`, `load_shell → load_spine`, `find_shell_dir → find_spine_dir`, `sync_shell_elements → sync_spine_elements`, and the rest.
- **Data:** live tables renamed `shell_elements → spine_elements`, `shell_snapshots → spine_snapshots` (mc-2 migration 062 — data-preserving `RENAME`; rows, RLS, FKs, indexes preserved).
- **Tenant tree:** project working dirs `shell/ → spine/`.

## v0.25.0 — 2026-06-15

### Changed — Project Shell spine inversion, Phase 4 (sweep enrichment)

The sweep was meant to be the reconsideration mechanism — re-reading the whole project and surfacing what's drifted — and wasn't yet. Two additions, scope deliberately tight.

**Meeting summaries feed the synthesis.** `cp sweep` now pulls the project's recent meeting entries (the whole Fathom summaries preserved in the Retrospective layer, Phase 2) into the LLM prompt, so the readout reflects what was actually *discussed*, not only the elements' current state. Up to the last 4 meetings, newest first. (Because MC-2 spine rows don't carry element bodies, the sweep hydrates the Retrospective body from disk before reading it — without this the feature is silently inert on the canonical read path.) Entries are split on true `### <date>` headers so an embedded `###` inside a Fathom summary doesn't fragment a meeting.

**Drift → review flags.** The sweep may now *propose drift* — elements whose recorded status or thinking looks superseded by the recent discussion — as a trailing fenced `drift:` yaml block. `cp sweep` parses it (best-effort; the Synthesis file keeps clean prose) and records each as a `review_flag` (`source: sweep`) on the element's MC-2 row, for the human to confirm in the forthcoming Mission Control UI. The sweep proposes; the human confirms.

`review_flags` are now **source-aware**: reconcile (Phase 1) and sweep flags coexist — at most one open flag per field *per producer* — so a routine `cp sync` no longer wipes a sweep-proposed drift flag (and vice versa). No migration: rides the existing `review_flags` jsonb. Embeddings-driven corpus reconsideration and active client-doc re-surfacing remain out of scope (design Non-goals).

## v0.24.0 — 2026-06-15

### Added — Project Shell spine inversion, Phase 3 (client-document linkage)

A meeting gives a summary we preserve whole (Phase 2). A client document deserves the same: the distillation is useful, but the original must stay reachable and re-considerable. Client originals already live in `rag_assets` (chunked, embedded — IBX-5153 has 101); the distilled `Brief`/`ClientFeedback` shell elements are our synthesis of them. Nothing connected the two. This phase links them.

**`cp shell` "Source documents" facet.** After the ranked sweep, `cp shell` lists the project's active `rag_assets` and shows which distilled elements cite each one. The project's assets are resolved via the bridge `shell_elements.project_id === rag_assets.project_id` (a uuid the shell already carries — *not* via `projects.code`, which is a different slug). Best-effort and MC-2-only — never breaks the sweep. The list caps at 25 bullets (linked assets first) with the true total in the header.

**Live-computed links, no frontmatter mutation.** A distilled element's `source` frontmatter holds human-readable file refs (e.g. `synthesis-docs/client_input_brief_distilled.md`). `match_sources_to_assets` matches those to assets by basename (case-insensitive, deterministic, empty-key-guarded) at render time — so an element shows its linked original without baking typed links into human-authored files. The `source` parser was widened to also *preserve* typed-link dicts (`{type: rag_asset, id, title}`) when present, round-tripping them through MC-2's `source` jsonb; `source` is not a reconcile-tracked field, so it mirrors freely.

Linkage and reachability only — embeddings-driven reconsideration across the corpus stays out of scope (a design Non-goal). No migration: rides the existing `shell_elements.source` jsonb. Verified live on IBX-5153 — 13 distilled elements link their embedded originals (the foundation doc → `infoblox_ai_foundation.md`, Carol's framework deck → the client-owned pptx, the kickoff transcript → the agency briefing).

## v0.23.0 — 2026-06-15

### Added — Project Shell spine inversion, Phase 2 (the Retrospective layer)

We process every input down to tiny nuggets and the big picture evaporates. The Retrospective is the anti-compression move: a new living shell layer that preserves the narrative of *how a project got to where it is*, built entirely from data we already store but throw away.

**New `Retrospective` layer.** A twelfth shell layer holding one living element per project at `shell/Retrospective/meeting-history.md`. It's framing (never demotes — append-only history), Lens-scored (`LAYER_IMPORTANCE` 0.60), and a first-class shell element: spine row in MC-2, snapshot-able, sweep-visible — not a special-cased sidecar.

**Whole Fathom summaries, preserved.** On every tagged-meeting auto-ingest, the webhook appends a dated entry that embeds the Fathom summary **whole** — the good narrative we already store in `fathom_meetings.summary` but never used. Structured pointers (decisions, action items, recording link) are *added* around it, never a replacement. Entries are newest-first under the element's H1, idempotent on `meeting_id`. Covers all three ingest shapes — single-project, account, and sprint-planning meetings each leave a Retrospective entry in every project they touch.

The append is best-effort: a retrospective failure never breaks or fails the surrounding ingest, and it rides the existing per-project `git add -A` commit (no extra commits). Per the design, entries are markdown-only — no `retrospective_entries` structured table until a UI needs it.

## v0.22.0 — 2026-06-15

### Changed — Project Shell spine inversion, Phase 1 (MC-2 becomes the verified source of truth)

The shell shipped (slices 1–3) with markdown as source of truth and MC-2 as a downstream mirror. The original intent was the opposite: **MC-2 is the fast, human-verified spine**; markdown is the rich detail it points into. This phase restores that direction. Requires mc-2 migration `061_shell_field_verification` (adds `field_states`, `confirmed_by`, `confirmed_at`, `review_flags` to `shell_elements`).

**Reconcile, don't clobber.** `cp sync`'s shell mirror no longer overwrites the five human-confirmable fields (`status`, `stage`, `target_date`, `serves`, `depends_on`). For each, once a human has marked it `confirmed` in MC-2 (via `field_states`), a conflicting re-derivation from disk is recorded as a `review_flag` and the confirmed value is **kept** — never overwritten. Proposed/absent fields update freely, as before. This one-way door is what makes MC-2 authoritative: the machine may *suggest* a change, but only a human may *change* a confirmed value.

**Confirmed rows survive disk loss.** The orphan-reap that deletes rows whose markdown vanished now skips any row carrying a confirmed field — deleting it would silently destroy confirmed state. Such rows are kept and flagged (`source: present→missing`) for human resolution. Unconfirmed orphans are still reaped.

**Bounded review queue.** At most one open `review_flag` per field — a persistent conflict refreshes its flag rather than appending a new one on every sync (sync runs on every auto-ingest push and SessionStart).

**Reads treat MC-2 as truth.** `cp shell` reads MC-2 unconditionally; a successful read — even an empty one — is the authoritative answer and no longer falls through to disk. Disk is consulted only when MC-2 doesn't serve the read, and that path is labelled loudly: *"MC-2 unreachable — showing last-known markdown-derived state, unverified."* Elements read from MC-2 now surface their `field_states`/`review_flags`/`confirmed_by`/`confirmed_at` for the forthcoming Mission Control verification UI.

Element *creation* and body authoring remain markdown — this inverts authority over **structured state**, not over **content**. Design: `cp/docs/plans/2026-06-15-shell-authoritative-spine-design.md`.

## v0.21.1 — 2026-06-14

### Changed — `derive_status` warmest-wins for multi-serve elements

An element that `serves` multiple deliverables now takes the **warmest** state across them (active > reference > dormant) instead of the coldest. Previously a single blocked or shipped consumer dragged the whole element down — so the Phase B sweep synthesis, which serves the active foundation doc *and* the blocked 6/17 workshop, rendered `(dormant)` despite actively feeding live work. Now it stays `active`: an element is as live as its liveliest consumer (stays active if it feeds any live unblocked deliverable; settles to `reference` if it only feeds shipped/citable work; cools to `dormant` only when every consumer is blocked). Single-serve elements and all invariants (frontmatter floor, framing-never-demotes) are unchanged. Verified live on IBX-5153: exactly two multi-serve elements flipped `dormant`→`active` (the sweep readout 0.48→0.68, and the in-progress "five concepts" deliverable that a blocked downstream consumer had wrongly cooled), nothing moved cooler.

## v0.21.0 — 2026-06-13

### Added — Project Shell, slice 3 (snapshots + sweep + analytics)

The shell's deferred trio. Snapshots give a deliverable a version axis; the sweep turns the whole-project Lens into a written readout; analytics query the spine across every project. Requires the `shell_snapshots` table (mc-2 migration `060_shell_snapshots`).

**Phase A — named snapshots.** `cp snapshot <code>/<deliverable-id> --label "..." --reason "..."` freezes a deliverable's current body+spine into a `shell/<Layer>/<el>.snapshots/<date>-<label>.md` sibling file — a verbatim copy with an added `snapshot:` frontmatter block (label, reason, a `commit` pointer to "what else was true then", and a `working_copy_dirty` flag). `cp snapshots <code>/<deliverable-id>` lists a deliverable's snapshots newest-first. The frozen file is the source of truth; a `shell_snapshots` MC-2 row indexes it, mirrored by `cp sync` (per-project upsert + orphan-reap, best-effort). Recovers *what a document said* at a turning point — `target_history` only preserved *when the date moved*.

**Phase B — whole-project sweep.** `cp sweep <code>` widens the Lens to every element, sends the ranked set + body excerpts to the LLM, and writes the synthesis as a `Synthesis`-layer element (`shell/Synthesis/<date>-sweep.md`, idempotent same-day) — a written across-the-project readout that explicitly names the cold-but-important threads (re-heat as advisory prose, no persisted state). The sweep element `serves` the active deliverables, so it scores hot on the next pass. Also available opt-in as `cp prep-planning --sweep`, riding the existing weekly rhythm (best-effort per project; default path unchanged — no LLM call without the flag). Needs `ANTHROPIC_API_KEY` exported (no dotenv fallback); a clear error if it's missing. The sweep's ranking is shared with `cp shell`'s display via one `rank_elements` helper, so the two can never drift.

**Phase C — cross-project analytics.** `cp shell-stats` reads the `shell_elements` spine across all projects: a deliverable **type inventory** ("how many Positioning Narratives have we made"), a **stage distribution** (in-flight vs shipped), and **due-soon** (deliverables with a `target_date` in the next N days — `--within-days`, default 14). `--type <t>` narrows the inventory + due-soon. Unlike `cp shell`, this command **requires MC-2** — it *is* the cross-project index, so offline is a clear error with no disk fallback, by design. Note: **due-soon excludes already-shipped (`stage: final`) deliverables** (a shipped thing isn't "due"), while the inventory and distribution count all deliverables — the section headers say so.

**Deferred beyond slice 3:** auto-snapshots (manual only for now), persisted re-heat flags, per-stage timing analytics, the Strategy engine / StoryOS bridge.

## v0.20.0 — 2026-06-13

### Added — Project Shell, slice 2 (MC-2 spine + sync + auto-demotion)

The shell's structured project state graduates from markdown-only (slice 1) to a queryable MC-2 spine, with the relevance Lens now reading from that spine and computing demotion from the deliverable graph rather than hand-set frontmatter. Requires the `shell_elements` table (mc-2 migration `059_shell_elements`).

- **Frontmatter → MC-2 mirror, folded into `cp sync`.** Every sync cycle mirrors each project's `shell/<Layer>/*.md` frontmatter into the `shell_elements` table — one row per element, keyed to `projects.id`, upserted by `element_id`, with orphan rows (elements deleted/renamed on disk) reaped per-project. The file owns the body, the frontmatter owns the spine, MC-2 is the queryable index. The mirror is **best-effort per project**: a mirror failure logs a `WARNING` (with traceback) and never aborts the surrounding sprint/CP sync.
- **`cp shell <code>` reads MC-2 (canonical), falls back to disk.** The Lens now reads the spine from MC-2 so cross-project queries and auto-demotion drive the same command. When MC-2 is unreachable (no creds / offline) it degrades **visibly** to the slice-1 markdown read — a missing-creds `BackendUnavailable` is a quiet stderr note, while a connected-but-query-failed error is a loud `WARNING` flagging a likely schema/query bug (never a silent stale read).
- **Auto-demotion — derived status from the deliverable graph.** An element's effective relevance status is now *computed*: an element serving a `final` deliverable settles to `reference`; one serving a blocked deliverable (a `depends_on` target not yet final) cools to `dormant`; framing-layer elements never demote; an explicit non-active frontmatter status is the floor (derivation only demotes, never promotes). The sweep's score **and** its `(reference)`/`(dormant)` markers both read this derived status, so display and ranking always agree.

Verified end-to-end on IBX-5153: `cp sync` mirrored 29 elements across 10 layers, `cp shell ibx-5153` produced the identical ranked sweep MC-2-backed (foundation doc + 6/17 workshop on top), and the offline fallback rendered the full sweep from disk.

**Deferred to slice 3:** named snapshots, the periodic sweep/synthesis readout, auto-snapshots, cross-project type analytics.

## v0.19.1 — 2026-06-13

### Changed — Lens gains a status-weight term (Project Shell)

Surfaced by hand-backfilling IBX-5153 into the shell: delivered `final` artifacts (a workshop pre-read, a leave-behind) were tying the still-open work they served at the top of the sweep. The Lens score now multiplies a fourth term — a status weight (`active=1.0`, `reference/dormant=0.7`, `final=0.5`) — so settled elements demote below live ones without dropping out of the sweep. The design's "deliverable ships → its elements demote together," made real.

## v0.19.0 — 2026-06-13

### Added — Project Shell, slice 1 (`cp shell <code>`)

The first slice of the **Project Shell** — structured per-project state that gives incoming material a known home so a project's history stops mushing together. Markdown-only and read-only this slice; the MC-2 spine, snapshots, and demotion automation land later.

- **Element schema + `shell/` convention.** A project's state is a set of small markdown elements under `<project>/shell/<Layer>/<name>.md`, where the YAML frontmatter is the structured spine (`id`, `layer`, `type`, `stage`, `target_date`, `depends_on`, `serves`, `status`, `last_touched`, …) and the body is the content. Eleven fixed layers: Brief, Agreement, Research, SourceMaterial, ClientFeedback, Synthesis, Drafts, Deliverables, Decisions, Timeline, Stakeholders.
- **The Lens (computed relevance).** `relevance = recency × serves-active × layer-importance` — never stored, always computed. Elements serving an active deliverable (or in an always-ambient framing layer) score hot; everything else decays but never reaches zero ("a dimmer, not an off-switch").
- **`cp shell <code>`** — read-only command that prints the full relevance-ranked sweep of a project's shell, hottest first, with `[stage]` / `due <date>` (with `(overdue)` marking) / `(reference|dormant)` annotations. Resolves the project dir offline (no MC-2 connection), reusing sync's scope/dir-resolution helpers.
- New dependency: `python-frontmatter`.

### Fixed — closed-recent + exceptions recency windows anchored on the injectable sync clock

`render_master_cp`'s `is_closed_recent` and the exceptions-README window used wall-clock `datetime.now()` instead of the `sync_clock` the rest of sync already threads, so the first master-cp render pass and the agenda second pass disagreed on "now." Both now anchor on `sync_clock`, fixing two time-dependent test failures and the latent inconsistency behind them.

## v0.18.2 — 2026-06-10

### Fixed — remove the plugin's CLI-install hook (it fought the tenant hook and kept reverting `cp` to a stale version)

Root cause of a recurring, hand-fixed-3–4-times bug: the local `cp` CLI kept reverting to an old version (observed: 0.10.0 from frozen rev `1cfe5e4`) every session, even right after a manual `uv tool install --force` to the current version.

**Two SessionStart hooks were fighting over the `cp` binary, installing from different sources:**

- The **plugin** shipped `plugin/hooks/sync-cli-version.sh`, a SessionStart hook that force-reinstalls `cp` from **`git+…@v<plugin-version>`** — the plugin's *own* released tag.
- The **tenant** ships `.claude/hooks/check-cp-engine-version.py` (added in v0.17.0), which reinstalls `cp` from the **local cp-engine clone** — the right source for a developer who runs unreleased engine code.

Because the plugin marketplace has `autoUpdate` off, **multiple stale plugin versions (0.8.2 / 0.10.0 / 0.18.0) stayed cached and registered simultaneously**, each carrying its own copy of the hook. Whenever a stale plugin's hook won the SessionStart race, it dragged `cp` back to that plugin's git-tagged version, undoing the tenant hook's local-clone install. Manual heals only lasted until the next session — hence the repeated recurrence.

**Fix:** delete the plugin's CLI-install hook entirely (`plugin/hooks/sync-cli-version.sh` + `plugin/hooks/hooks.json`). The plugin no longer touches the `cp` CLI in any way. The **tenant hook is now the single source of truth** for the CLI version, installing from the local clone. One healer, one source — the war is over.

**Machine cleanup also required (one-time, not shipped):** the already-cached stale plugin versions still carry the hook until purged. On an affected machine, remove the `hooks/` dir from each cached plugin version under `~/.claude/plugins/cache/cp-engine/cp-engine/<ver>/` and from `~/.claude/plugins/marketplaces/cp-engine/plugin/hooks/`. New plugin installs from v0.18.2+ won't ship it.

### Migration notes

**No tenant action required** beyond the patch flowing automatically (`~= 0.18`). The tenant SessionStart hook (v0.17.0+) continues to keep `cp` in lockstep with the tenant pin via the local clone. If your `cp` has been flapping, purge the stale plugin caches as above once; it won't come back.

## v0.18.1 — 2026-06-09

### Fixed — three sync/status bugs surfaced while debugging a "stale master-cp" report

A user reported `master-cp.md` looking out of date after a meeting ingest. The root cause was operational (a missing `cp sync` — `cp ingest` writes sprint files only), but the debugging surfaced three real engine bugs that amplified the "something's broken" impression. All three are fixed here, test-first.

**`master-cp.md` provenance header not bumped on sync (#10).** The anchor `Provenance: Version <NN> | <date>` line lives *outside* every engine-managed region, so splice-mode sync never refreshed it — the file showed an old version + date (e.g. `0.8.16.4 | 2026-05-18`) even immediately after a clean regeneration. The header contradicted the in-body `Last sync` timestamp and read as "out of date." `_write_if_changed` now refreshes the provenance line from the freshly-rendered body on every splice write (new `_refresh_provenance_header`).

**`cp status` was a stub (#9).** It printed `not implemented yet (lands in v0.4)` and exited 1, despite being advertised in `--help` as "show what would change on next sync" — the natural first command to reach for when diagnosing a stale tenant. It's now a real no-write dry-run: `sync_tenant` gains a `dry_run=` flag, `_write_if_changed` delegates change-detection to a new `_would_change` (compute-don't-write), and the write-heavy passes (`install_into_tenant`, account/sprint scaffolding, deactivation) are skipped under dry-run. `cp status` reports the high-signal files that would change (master-cp, CLAUDE, .gitignore, new project CPs) and touches nothing.

**`last-week-workload` regenerated empty at the start of a sprint week (#11).** Sync read allocations via `_last_week_monday`, which returns the *upcoming sprint-planning* window's Monday — at the start of a sprint that's the **current** week, which has no logged hours yet. Result: every project lost its `_Last week: <hours>_` annotation and the whole workload region emptied, reading as data loss. Sync now reads the **prior completed** week via `_prior_completed_week_monday`, matching the rule `cp prep-planning` already uses, so the two surfaces agree and last week's real hours show.

### Migration notes

**No tenant action required** beyond the patch flowing automatically (`~= 0.18`). No data migration. The next `cp sync` will bring stale provenance headers current and restore the workload annotations.

## v0.18.0 — 2026-06-06

### Fixed — `cp prep-planning` now resolves the ClickUp token from MC-2's `.env`

`cp prep-planning` read the ClickUp Personal API Token only from `CLICKUP_API_TOKEN` in the process environment. But MC-2's `backend/.env` — the canonical local-dev home for the engine's credentials — stores the same token under `CLICKUP_API_KEY`, and unlike the SUPABASE creds (which already fall back to that `.env` via `sync_mc2._load_supabase_creds`), the ClickUp side never did. The result: running `cp prep-planning` from a fresh shell silently failed *every* project's milestone fetch — `milestone_counts` all zero, an empty Forward Calendar across the whole doc — which defeats the entire point of the v0.15 forward-looking planning doc. This was the same class of env-name mismatch that bit the SUPABASE wiring in v0.15.

New `_resolve_clickup_token(config)` mirrors the SUPABASE resolver exactly: it checks the process environment first (accepting **both** `CLICKUP_API_TOKEN` and `CLICKUP_API_KEY`), then falls back to `<mc-2 clone>/backend/.env` (again both names, clone path from `.cp-engine.local.toml`'s `[local-repos]`), printing a one-line stderr note on `.env` fallback so the implicit dependency stays visible. The token is resolved once at the CLI level (where `config` is available) and passed through `render_planning_doc` / `render_planning_summary`'s existing `clickup_token` parameter. `_clickup_token()` also now accepts both env-var names for any direct callers.

### Migration notes

**Tenants must bump** `.cp-engine.toml`'s `[engine].version` from `~= 0.17` to `~= 0.18`.

**No data migration required.** If you previously had to export `CLICKUP_API_TOKEN` by hand to get a populated Forward Calendar, you no longer need to — `cp prep-planning` now finds the token in `mc-2/backend/.env` automatically.

**Plugin users:** update the cp-engine Claude Code plugin (`/plugin`) to pick up the current `/cp-prep` command, which has used `cp prep-planning` (not the deprecated `cp prep-agenda`) since v0.15. The marketplace entry has `autoUpdate` off, so the plugin cache does not refresh on its own.

## v0.17.0 — 2026-06-05

### Added — engine-managed `.claude/` SessionStart hook that self-heals a stale `cp` CLI

The `cp` CLI is a `uv tool`-installed binary, separate from the cp-engine Claude Code plugin. It is installed from a frozen git rev, the marketplace entry has `autoUpdate: false`, and nothing re-resolves it — so the CLI silently lags the tenant's `[engine].version` pin. The failure is invisible until someone runs `cp sync`, which then refuses with "installed cp-engine X does not satisfy the tenant's engine pin". Observed in the cp tenant: the CLI sat at 0.10.0 while the engine shipped through 0.16, because the pin's guard only fires at sync time and server-side auto-ingest (a separate, current install) masks the staleness day-to-day.

This release ships a SessionStart hook into every tenant on sync. `install_into_tenant` (new module `cp_engine.claude_settings`) copies a packaged hook script to `.claude/hooks/check-cp-engine-version.py` and idempotently merges a single SessionStart entry into `.claude/settings.json`. On each session the hook compares `cp --version` against the tenant pin and, on mismatch, reinstalls `cp` from the local clone (resolved from `.cp-engine.local.toml`'s `[local-repos] cp-engine`).

The hook is deliberately defensive: it implements the `~=`/`>=`/`==`/bare PEP-440 constraint check inline (no `packaging` dependency, so it runs under bare system python3), is non-blocking and fully silent on the healthy path, and exits 0 on every failure path — a version-check hook must never stop a session from starting. The settings merge recognizes its own entry by a stable sentinel substring, preserves any tenant-authored hooks and settings, and refuses to clobber a malformed `settings.json`.

`settings.json` is JSON and can't carry the `cp-engine:start/end` splice markers the markdown files use, hence the sentinel-based merge rather than region splicing. The hook script directory carries an `__init__.py` so hatch reliably ships it on directory/editable installs (the wheel already included it; a directory install dropped it without the package marker).

### Migration notes

**Tenants must bump** `.cp-engine.toml`'s `[engine].version` from `~= 0.16` to `~= 0.17`.

**No data migration required.** On next `cp sync` the engine creates `.claude/settings.json` and `.claude/hooks/check-cp-engine-version.py` (or merges into an existing `settings.json`). Both are committed to the tenant. Tenants with their own `.claude/settings.json` keep all existing keys and hooks — the engine only ensures its one SessionStart entry. The generated `.gitignore` now also ignores `.claude/hooks/__pycache__/`.

## v0.16.0 — 2026-06-03

### Added — sprint files now show real recent-commits activity

`_collect_sprint_per_project_data` had been a v0.8.0 stub returning `{}`, so every sprint file in every tenant rendered "_No commits in the last 7 days._" regardless of real activity. This release implements the recent-commits aggregation: for each project, the engine walks the local clone (resolved from `TenantConfig.local_repos`, the per-machine map in `.cp-engine.local.toml`), runs `git log --since=<sprint_start>`, and renders the commits as a bulleted list in the sprint file's `Where it stands` → `Recent activity` section. Caps each list at 20 entries; truncates subject lines to 80 chars with ellipsis. Skips projects without a mapped clone, with a missing path, or where `git log` errors — none of those should break sync.

Caught during implementation: `sprint_week_dates(now)[0]` returns the *planning-rolled* Monday (Wed-Sun rolls forward to next week's Monday per the cp planning-week rule). Using it as the `--since` anchor filtered out everything on any Wed-Sun sync. The fix anchors at calendar Monday via the existing `sprints._monday_of(now)` helper. The "(last 7 days)" rendered header now actually matches the data window, which it never did before.

Backwards-compatible: only the `recent_commits` dict key is populated. The other per-project metric keys (`sessions_this_week`, `open_issues`, `last_session_*`) keep their renderer-default fallbacks. Tenants without any `[local-repos]` entries see no behavior change.

### Migration notes

**Tenants must bump** `.cp-engine.toml`'s `[engine].version` from `~= 0.15` to `~= 0.16`.

**No data migration required.** No schema changes. Sprint files refresh on next `cp sync` (idempotent — no-op when no new commits).

**Recommended:** ensure every project the tenant tracks under FPSF/Canonic Internal Tooling has its local clone mapped in `.cp-engine.local.toml`'s `[local-repos]` block. Without the mapping, that project's sprint file will continue showing the "no commits" placeholder. The cp tenant's mapping was extended this release: `cp`, `fathom-meeting-sync`, and `1p-component-library` are now mapped alongside the existing `mc-2`, `cp-engine`, `storyos`, `unf-forge`, `market-leadership-scorecard`.

## v0.15.4 — 2026-06-02

### Fixed — Forward Calendar empty when ClickUp `due_date` is null but task name carries the date

`_normalize_clickup_task` only read ClickUp's structured `due_date` field. fathom-meeting-sync's Stage C push (the dashboard's "Create N tasks" button) sets the proposal's text-form deliverable as the ClickUp task NAME — including the `(due YYYY-MM-DD, owner: ...)` suffix — but does NOT set ClickUp's structured `due_date`. So milestones pushed via the dashboard arrived in ClickUp with `due_date: null` even though the date was right there in the name.

Symptom (surfaced 2026-06-02 in live verification): IBX milestone (`tag=milestone`, `Type=2` custom field, correct in every other way) appeared in the doc only as an Open Commitments row, never in Forward Calendar — because `_render_forward_calendar` skips milestones with no date anchor.

Added `_NAME_DUE_DATE_RE` (matches `(due YYYY-MM-DD`) and `_parse_due_date_from_name`. `_normalize_clickup_task` now prefers ClickUp's structured field when present and falls through to the name regex when null. Defensive against any push source that omits the structured field — not just fathom-meeting-sync's current shape.

The upstream fix (set ClickUp's structured `due_date` in `pushApprovedProposal`) is tracked separately so structured data becomes the norm going forward. This regex stays as defensive cover.

## v0.15.3 — 2026-06-02

### Fixed — `cp prep-planning` couldn't resolve clickup_list_id from a fresh shell

`_make_supabase_client` in `prep_planning.py` only read `os.environ` for `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`. It did NOT use the `mc-2/backend/.env` fallback that `sync_mc2._load_supabase_creds` uses (the canonical local-dev convention per `[local-repos.<user>]` in `.cp-engine.local.toml`).

Symptom: any user running `cp prep-planning` from a fresh shell without exported SUPABASE creds saw "ClickUp list not set in MC-2 — milestones not tracked" for EVERY project — even projects with valid `clickup_list_id` rows in MC-2. The v0.15 Forward Calendar feature silently produced no data.

Surfaced during the live verification: pushed an IBX milestone through the Fathom dashboard's Stage C → ClickUp; `cp prep-planning` couldn't see it. Manual call to `_resolve_proposal_project` worked; the same code path through the CLI didn't. Root cause: supabase_client was None because the env-loader path differed from sync's.

Fix: `_make_supabase_client` now delegates to `sync_mc2._load_supabase_creds`. Both `cp sync` and `cp prep-planning` use the same precedence rules (os.environ first, then mc-2/backend/.env via tenant local-repos config). Returns None on `BackendUnavailable` (preserves graceful-degrade for tenants without an mc-2 clone).

The cluster-4 reviewer flagged this as a nitpick during the v0.15.2 system review ("Inconsistent SUPABASE creds resolution"). Turned out to be a silent feature-killer, not a nitpick.

## v0.15.2 — 2026-06-02

Day 2 of the post-v0.15.0 system-review cleanup. Ships the remaining 16 findings (12 Important + 4 defensive) across webhook, ingest, sprints, sync, prep_planning, and tooling. 745 tests passing (was 684 at v0.15.1).

### Security

#### Fixed — Fathom auto-ingest had no replay protection

`/api/auto-ingest` used body-only HMAC with no timestamp window. A captured signed request could be replayed indefinitely. Combined with the v0.13 `/rerun` endpoint (which ignored its body), one captured signature would have let an attacker rerun arbitrary failed runs.

Added `X-Webhook-Timestamp` header support folded into the HMAC base, matching the existing Slack-signature pattern. Skew check rejects timestamps more than 300 seconds off when the new `WEBHOOK_REQUIRE_TIMESTAMP=true` env var is set. The rerun endpoint additionally requires a JSON body `{"run_id": "<uuid>"}` that matches the URL path's `run_id`, binding the HMAC to the target.

**Backwards-compatibility gate:** when `WEBHOOK_REQUIRE_TIMESTAMP` is unset or `false` (the default), legacy body-only HMAC is still accepted with a transient WARNING log. This lets cp-engine ship first, then fathom-meeting-sync deploys its own update sending the new header, then the gate flips on. The cp-engine warning text instructs the operator to flip the env var once fathom-meeting-sync rolls.

When the gate is enforced, missing `run_id` in the rerun body returns 400 immediately (the equality check no longer silently falls through on absent keys).

### Correctness — webhook

#### Fixed — concurrent auto-ingest pushes clobbered each other

When two webhook requests arrived close together, each cloned independently and pushed to `main` with no rebase loop. The second push hit a non-fast-forward error → 500 → Fathom retry → race. Phantom "failed" runs accumulated even though the underlying ingests had succeeded.

Added `_push_with_retry()` that detects non-fast-forward via stderr markers (`non-fast-forward`, `fetch first` — deliberately NOT the broader `rejected` substring, which also catches auth failures and pre-receive hook rejections), runs `git pull --rebase` between attempts (up to 3), and aborts cleanly with `git rebase --abort` on rebase conflict. Wired into all 3 push call sites (`_commit_and_push`, `_commit_meeting_artifacts`, `_commit_clickup_close`).

#### Fixed — Fathom duplicate-delivery idempotency

When Fathom retried a webhook delivery (its 30s timeout window vs. the LLM-process loop), we'd re-clone, re-LLM-plan, and re-push. `execute_plan`'s content-hash dedupe caught the bullet writes, but `auto_ingest_runs` accumulated duplicate `success` rows.

Added `_find_successful_duplicate_run()` that scans the last 20 `auto_ingest_runs` rows for the same `(meeting_id, sorted(project_codes))` fingerprint. Match → 200 `{"status": "duplicate_delivery_skipped", "existing_run_id": "..."}` BEFORE clone/LLM. The rerun endpoint intentionally bypasses this check (explicit user intent).

#### Fixed — Slack-action background tasks were invisible to postmortem

When Railway restarted (deploy, OOM, scale-down) during a Slack-action background task, the user saw the click acknowledged but the cp tenant never updated, with no log trail to reconstruct what was queued. Added structured `slack_action_spawn` and `slack_action_complete` log lines (key=value greppable) emitted in `_handle_block_action`, `_handle_view_submission`, and `_run_action_in_background`. A `TODO(v0.16)` comment notes the planned `slack_action_intents` persistence layer for actual recovery.

#### Fixed — ClickUp close webhook missed batched events

`/clickup-task-closed` read only `history_items[0]`. ClickUp's webhook batches multiple changes per event (assignee + status); if the first item was the assignee change, we silently 204'd even though the payload carried a real status→closed transition. Now iterates the full array, finds the first `field=='status'` item with `after.type=='closed'`, and processes it. Diagnostic log captures `(field, after.type)` of every item when no close transition is found.

### Correctness — data layer

#### Fixed — sprint-file open asks double-rendered against promoted ClickUp client-asks

During the bridging period as `set-client-ask-task` rolls out, the same ask exists both in the sprint file's `### Open asks` section AND in ClickUp as a `client_ask`-typed task. The Open Commitments table rendered both copies.

Threaded a `clickup_task_ids: dict[str, str]` map (cp_hash → ClickUp task_id) from the CLI through `build_planning_result` → `build_project_block`. The CLI pre-fetches the map by scanning current sprint files for hashes, then calling the existing `_fetch_clickup_task_ids_for_hashes` (already used by the daily digest for "Open in ClickUp" buttons). Sprint asks whose hash matches a known ClickUp task are filtered out. Silent-degrade on pre-fetch failure (the dedupe is polish, not load-bearing).

#### Fixed — LLM-extracted text with newlines broke same-line hash regex

`_write_ask`, `_write_decision`, `_write_risk`, `_write_inbound`, `_write_stakeholder`, and `_write_slack_digest` wrote text directly from the LLM extractor. Multi-line text (e.g. `"Line one\nLine two"`) produced bullets where the `<!-- cp:hash=... -->` trailer ended up on a different line than the bullet text, breaking the subsequent `close-ask`/`snooze-ask`/`resolve-risk` regex matches.

Added `_sanitize_inline_text()` (`" ".join((text or "").split())`) at the start of every affected handler, mirroring the pattern already used in `_write_quick_resume_verb`.

#### Fixed — `today=` parameter ignored by date-defaulting handlers

`_write_ask`, `_write_decision`, `_write_risk`, `_write_inbound` accepted `**_` and fell through to `_today_iso()` (which calls real `datetime.now()`). The `today` parameter Drew threaded through `execute_plan` for backfill/test scenarios silently did nothing. Added `_resolve_today_iso(today)` and updated handler signatures to accept `*, today: date | None = None`.

#### Fixed — Outbound scaffold placeholder leaked into `client_outbound`

`_parse_client_section`'s Outbound branch had a fall-through `else` that wrapped any non-bracket bullet — including the scaffold placeholder `- _<message — \`[status · date]\` prefix>_` — into an `Outbound(status="draft", date="", text=...)`. Today no consumer iterates `sf.client_outbound`, but the field is in `SprintFile` and `sprint_file_to_dict`, so any future consumer would silently see ghost rows. Added the `_is_template_placeholder` guard, matching the `_parse_horizon` pattern.

#### Fixed — `splice_managed_region` exception in master CP aborted the entire sync

Inside `_write_if_changed`, `splice_managed_region` for the master CP first pass could raise `MarkerDuplicated` / `MarkerInverted` on a single duplicated marker (hand-edit, leftover merge conflict). The whole `sync_tenant` run died before sprint files, account CPs, or weekly strips were written. Account/strip splices already wrapped in `except Exception`; master CP didn't.

Wrapped both splice loops in `try/except Exception`. On failure: log warning + fall back to full rewrite (mirrors the schema-evolution branch directly above), continue the sync.

#### Fixed — `_parse_heading_dates` mis-dated cross-year sprints

The H1 carried one year (`Dec 28 – Jan 3, 2027`), so a W53 sprint parsed as start=Dec 28 **2027**, end=Jan 3 2027. The bad start would propagate through `SprintFile.week_start` into `count_sprint_meetings`, `_carry_forward_rollup` ask aging, and aggregate sort keys. Would fire late December 2026.

Added `_WEEK_ISO_RE` and updated `_parse_heading_dates` to derive both dates from `week_iso` via `date.fromisocalendar` when available. The H1 regex stays as a fallback for malformed input.

### Operations & tooling

#### Fixed — empty/whitespace `.cp-link` silently captured into cwd

`capture_session.py` resolved an empty `.cp-link` to `Path("")` → `Path(".")`, which then `.exists()` to True and `.resolve()` to the user's current working directory. That cwd got treated as a cp working dir; we'd write `sessions/...md`, edit `cp.md`, and `git add` against whatever random repo was there. Silent footgun.

Added a strip-then-truthy guard before the `Path(target_str)` construction. Empty/whitespace falls through to the unlinked branch.

#### Fixed — `pin_resolver` could silently select a prerelease tag

`packaging.SpecifierSet.contains()` defaults to including prereleases when the candidate is a prerelease and the spec lacks an explicit prerelease marker. A hypothetical `v0.16.0a1` tag pushed before `v0.16.0` lands would have been chosen as the highest match for `~= 0.15`, silently shipping a prerelease engine to every tenant on next sync.

`list_remote_tags` now filters `v.is_prerelease` at source. Defense-in-depth check in `resolve()` belt-and-suspenders the same exclusion.

#### Fixed — `/cp-ingest` skill had hardcoded sample data

`plugin/commands/cp-ingest.md` referenced `sprints/2026-W19/cp.md` (sample data leaked into the real script). On real tenants without that directory, `parse-sprint` failed and the script silently fell back to `date +%Y-W%V` (no `-u`), so a late-night Pacific user landed in the wrong ISO week. Replaced with `WEEK=$(date -u +%Y-W%V)` directly; surrounding prose updated to reference `sprints/<W##>/<code>.md`.

#### Fixed — `scripts/release.py` CHANGELOG check was one-sided

The preflight check verified that a `## v<new-version>` section existed in CHANGELOG.md, but never that it was at the top. If someone drafted `## v0.16.0` ahead of `v0.15.2`, `release.py 0.15.2` would still pass preflight and ship, leaving the v0.16 stub orphaned above. Now asserts the first `^## v` match equals the new version.

#### Fixed — `scripts/release.py` tag check only covered local

A tag that existed on origin but had been deleted locally would pass preflight, then `git push origin v0.15.x` would reject AFTER the commit landed on `main`. Added `git ls-remote --tags origin <tag>` check; refuses release if remote already has it.

## v0.15.1 — 2026-06-02

A system-wide code review after v0.15.0 shipped surfaced 20 issues across cp-engine. This patch ships the 4 most urgent fixes; the rest are tracked for v0.15.2 + v0.16.

### Fixed — `set-milestone` and `set-client-ask-task` were dead code in production

The v0.15.0 headline ingest verbs (`set-milestone`, `set-client-ask-task`) silently logged INFO and skipped on every real Fathom webhook. Root cause: `webhook/main.py` had 7 `execute_plan()` callsites that never passed `supabase=`, so the new handlers hit the graceful-degrade branch (`if supabase is None: continue`) and did nothing. The LLM prompt actively instructed Claude to emit these verbs; the verb dispatch worked in tests with mocked supabase; the wiring just wasn't there in the webhook caller.

Threaded `supabase=_create_supabase_client()` plus `meeting_id=` through all 7 callsites (per-meeting × single-project, per-meeting × summary, sprint-planning × single-project, sprint-planning × summary, clickup-task-closed, slack-action, account-meeting via `_ingest_one_project`). For the two callsites where no `meeting_id` is in scope (clickup-task-closed, slack-action), `meeting_id=None` is acceptable — those endpoints emit only `close-ask` / `snooze-ask` / `resolve-risk` plans, never proposal verbs.

Added regression test `test_ingest_one_project_passes_supabase_and_meeting_id_to_execute_plan` plus an end-to-end test file `tests/test_webhook_milestone_wiring.py` that catches future re-regressions.

### Fixed — `set-milestone` and `set-client-ask-task` had no dedupe

The two new ingest verbs did raw `clickup_task_proposals.insert(row)` with no `cp_ask_hash` check. Combined with v0.13's rerun endpoint (which Drew shipped specifically to make re-ingest safe), every rerun created N duplicate `pending` proposal rows.

Added `cp_ask_hash` computation via `_content_hash(code, "set-milestone" | "set-client-ask-task", deliverable_or_what)` and a `_proposal_already_present()` helper that queries the existing pending/approved row for that hash. Match → silent no-op (logged INFO). Rejected rows do NOT dedupe (a previously-rejected proposal can re-enter on rerun by design). Lookup failure falls back to allowing the insert (preserves graceful-degrade).

### Fixed — daily digest block count could exceed Slack's 50-block limit

`attention_digest._render_digest_blocks` produces ~4 blocks per past-due ask + ~4 per escalated risk. The W22 retro showed 24 past-due asks across the tenant; the next time a similar week happens, Slack would reject the digest with `invalid_blocks` and the whole digest would fail silently for all recipients.

Added `MAX_ITEMS_PER_SECTION=10` (per-section soft cap, sorted by urgency descending — `days_past` for asks, `raised` for risks) AND `MAX_TOTAL_BLOCKS=45` (global hard budget). Each section recomputes its remaining budget so later sections shrink as earlier ones consume budget. When a section is truncated, an end-of-section context block surfaces the count: `"+ N more past-due asks not shown (see weekly-cp.md or run /cp-prep for full list)"`.

### Fixed — Open Commitments table cells could break on `|` or newlines in content

`prep_planning._render_commitments_table` built table rows with unescaped cell content. ClickUp task names like `"Spec | implementation | review"` or sprint-file asks containing newlines would corrupt the table, shifting every subsequent cell.

Added `_md_table_cell(text)` helper that escapes `|` → `\|`, collapses `\n`/`\r` → space, handles `None`, and strips trailing whitespace. Applied at the single row-emit site, covering all 4 cell positions across all 3 row sources (ClickUp milestones, ClickUp client-asks, sprint-file open asks).

### Fixed — `templates/CLAUDE.md.j2` still described the old `cp prep-agenda` flow

The Jinja template that scaffolds every tenant's CLAUDE.md still had the multi-step `cp prep-agenda` procedure (read sprint files manually, etc.) in its "Sprint planning prep" section. Next `cp sync` would have written this stale workflow into every tenant's live CLAUDE.md, silently undoing v0.15.0's `/cp-prep` skill rewire from the tenant's perspective.

Replaced the multi-step procedure with terse `/cp-prep` guidance pointing at `cp prep-planning` and the rendered `sprints/<W##>/_planning.md`.

## v0.15.0 — 2026-06-02

### Added — `cp prep-planning` forward-looking sprint-planning doc

Replaces the backward-looking `cp prep-agenda` for sprint planning. Where prep-agenda surfaced *past* state (recent inbound, aged asks, last-week decisions), prep-planning assembles a *forward-looking* doc anchored on dated ClickUp milestones: per-account project blocks each with a Where line, a Forward Calendar (milestones with due dates), and an Open Commitments table that two-ways tracks "us → them" (milestones we own) and "them → us" (client-asks we're waiting on).

The motivation: Drew's 2026-06-01 sprint-planning run with the old prep-agenda surfaced its limits — partners walked 30 projects' past-tense state in flat order, with no way to see "what ships when" or which projects need decisions this week vs. next week. The new doc:

- Groups by account (walk Google → Infoblox → SAP → Teleflex → … → FPSF initiatives → Canonic)
- Leads each project block with **urgent flags** (slip risk, decision due, past-due ask, escalated risk) so partners' attention vectors to what matters
- Surfaces tenant-wide cross-cutting context at the top — capacity-binding owners (≥5 projects) and decisions partners owe each other from `weekly-cp.md`
- Renders an Open Commitments table mixing dated ClickUp milestones with sprint-file open asks for the back-populate-in-progress period

Source of truth for milestones is ClickUp. Each active project (engagement OR initiative) carries a `clickup_list_id` in MC-2; this module fetches `tags[]=milestone` and `tags[]=client-ask` tasks from each list via the ClickUp REST API with pagination support (`_CLICKUP_MAX_PAGES=100`, safety cap at 10,000 milestones per list).

### Added — `set-milestone` + `set-client-ask-task` ingest verbs

Two new auto-ingest verbs (`set-milestone`, `set-client-ask-task`) classify Fathom action items into the existing `clickup_task_proposals` review-gate inbox with three new `task_type` values (`action_item` / `client_ask` / `milestone`). Milestones carry `confidence` (high/medium/low), `depends_on`, and `linked_to` fields. Client-asks track external commitments back to us as first-class tasks rather than free-form bullets.

The dashboard's approve-and-push path (in `fathom-meeting-sync`) was extended to write these classifications + dependencies through to ClickUp as custom fields and native task dependencies. Required env vars: `CLICKUP_TYPE_FIELD_ID`, `CLICKUP_CONFIDENCE_FIELD_ID`, `CLICKUP_LINKED_TO_FIELD_ID`. The webhook fails fast at module load if any are missing.

### Added — urgent-flag detection with 4 rules

`_detect_urgent` in `prep_planning.py` surfaces per-project attention items via four rules:

1. **slip_risk** — milestone with date in next 14 days, low confidence OR stale `depends_on` (where stale = sprint ask's `by` date is in past + dep substring-matches ask description)
2. **decision_due** — decisions in this/next sprint horizon, with template placeholders filtered out (the `_<...>_` italic-angle-bracket scaffold shape never trips a flag)
3. **past_due_ask** — sprint asks with `by` date in past (warn if age < 14 days, alert if ≥ 14)
4. **escalated_risk** — sprint risks marked `severity: escalated`

### Added — cross-cutting section with capacity binding + decisions

`_render_cross_cutting` surfaces tenant-wide context before the per-project walk:

- **Capacity-binding owners** — anyone with ≥5 active projects of record (sorted by count descending; implicit ownership from sprint-file commitments deferred to v2 with TODO)
- **Cross-cutting decisions partners owe each other** — parsed from `weekly-cp.md`'s `## Decisions (cross-cutting, last 4 weeks)` section, filtered to the last 28 days and excluding entries marked `[decided: ...]` or `[resolved: ...]`

### Changed — `_section_body` regex now matches suffixed headings

`sprints._section_body` previously matched only exact `## Heading` form, silently returning empty bodies for real scaffold headings like `## Horizon — 4–8 weeks out`. Caused `agenda._extract_decisions_due_for_project` and `_parse_horizon` to silently drop their entire payloads on every real sprint file — a regression that had been hiding in production since the scaffold templates added suffixes.

Loosened the regex to accept `— suffix`, `– suffix`, `- suffix`, `: suffix` separators (suffix kept on one line so DOTALL can't bleed across sections). Also filters scaffold `- _<...>_` placeholder bullets out of `_parse_horizon` so they don't leak into the master-cp.md carry-forward roll-up.

### Changed — shared helpers promoted to public names

Helpers previously imported across modules under leading-underscore names (`_filter_active`, `_short_iso_date`, `_to_datetime` from `agenda.py`; `_bullets`, `_parse_bracketed_bullet`, `_section_body`, `_subsection` from `sprints.py`) are now public — no leading underscore. Each had exactly one cross-module caller; a `_shared.py` module would have been over-engineering.

### Changed — `cp prep-agenda` deprecated

Still works (still produces its backward-looking doc; existing automations don't break) but emits a stderr deprecation warning naming `cp prep-planning` as the replacement. Will be removed in a future release.

### Migration notes

- **Tenants must bump** `.cp-engine.toml`'s `[engine].version` from `~= 0.14` to `~= 0.15`.
- **MC-2 migrations 051 + 052** must be applied (already applied to the live `1p_knowledge_dev` project in this release cycle; new tenants will need them).
- **`/cp-prep` skill** now points at `cp prep-planning` and writes to `sprints/<W##>/_planning.md` (was `_agenda.md`). Old `_agenda.md` files from prior runs can coexist.
- **ClickUp setup required:** projects need `clickup_list_id` populated in MC-2; FPSF initiatives need their lists too. Custom-field IDs (Type, Confidence, Linked To) must be set on Railway as `CLICKUP_*_FIELD_ID` env vars. See `docs/clickup-field-ids.md`.

### Fixed — `_validate_plan` rejects malformed `set-milestone` / `set-client-ask-task` plans early

Both new verbs validate at plan-time, not handler-time, so the LLM gets a clear "missing field" or "unknown verb" error before the row reaches Supabase.

### Fixed — pagination in `_fetch_clickup_milestones`

Previously the ClickUp `/list/{id}/task` fetch was a single GET with the default 100-task page size, silently truncating any project that ever accumulates 100+ milestone-tagged tasks. Now loops with explicit page counter, breaks when a page returns < 100 tasks, raises `RuntimeError` with task-ceiling context if the safety cap of 100 pages (10,000 tasks per list) is exceeded.

### Fixed — malformed dates in weekly-cp.md decisions

`_load_cross_cutting_decisions` previously fell back to `today` when a decision's date parsed failed — silently keeping typo'd 2019 entries in scope forever. Now logs a warning via the module logger AND appends a tenant-wide entry to `result.errors` AND skips the malformed entry entirely. Better for everyone: the bad data surfaces, the good data still flows.

### Fixed — ClickUp auth failures now surface in `--summary`

`_fetch_clickup_milestones` distinguishes 401/403 auth failures from generic 4xx/5xx, raising distinct `RuntimeError` messages. `build_planning_result` dedupes auth failures to a single tenant-wide `result.errors` entry (so 26 projects hitting the same bad token don't produce 26 redundant errors). Missing `CLICKUP_API_TOKEN` raises early with a load-bearing message.

### Fixed — `_(ClickUp list not set)_` placeholder distinguishes list-unset from list-empty

Previously the same misleading message rendered whether `clickup_list_id` was actually unset OR the list IS set but has zero milestone-tagged tasks. Now two distinct sentinels: `no_clickup_list` (fix via MC-2 dashboard) vs `no_milestones_tagged` (fix via back-population). Important during the v0.15 ramp-up period when most lists are set but empty.

### Fixed — project header dedup when code == name

When `project.code == project.name` (e.g. `cp`, `1p-component-library`), the header renders as `### cp — Drew and Tony` rather than the duplicated `### cp cp — Drew and Tony`.

## v0.14.2 — 2026-05-29

### Fixed — clicking one digest button no longer visually closes every item

`_post_response_url_update` used to walk every `actions` block in the original message and replace each with the same confirmation context. The daily digest packs N items into a single Block Kit message, each with its own `actions` block, so one click on item 3's "✅ Mark closed" button caused all N items to render as closed in-place — even though only item 3 had actually been committed to the cp tenant.

Symptom in the wild on 2026-05-29: 6 past-due asks in the digest; user clicked Mark closed on 1; the message showed all 6 with "✅ Closed · 5:47 PM UTC · `6320e6e4`" (the same fake-looking SHA on every line). The single real commit was correctly applied; the other 5 were UI lies.

Fix: thread the clicked button's `action_id` (Slack-guaranteed unique per element since the v0.14.0 namespacing of `<verb>_<code>_<hash>`) from `_handle_block_action` through `_run_action_in_background` into `_post_response_url_update`. The loop now only replaces the actions block whose element matches the clicked `action_id`. Other items keep their buttons intact, so the user can continue clicking down the list.

Also adds a `log.warning` when the action_id can't be found in the message blocks (e.g. message was edited mid-click) instead of silently misbehaving.

No behavior change to the cp tenant — clicks were always writing the correct file/commit; only the in-Slack visual was wrong.

## v0.14.1 — 2026-05-28

### Added — diagnostic logging on all `/slack-action` 401 branches

`_verify_slack_signature` now emits `log.warning(...)` on every 401 reject path, with enough context to diagnose which of the 5 verification checks failed (missing/malformed header, missing/non-int timestamp, replay-window skew, or HMAC mismatch). For HMAC mismatches, logs the first 9 chars of expected vs provided signatures (safe: 6 hex chars of a single HMAC output don't compromise the secret), plus body and secret lengths so wrong-secret-entirely cases are distinguishable from body-encoding mismatches.

Triggered by a production debugging session: silent 401s on real Slack callbacks gave only the access-log line ("401 Unauthorized"), with the HTTPException detail string not surfaced anywhere actionable. Now the application logs name the exact failure mode.

No behavior change beyond logging. Same env vars.

## v0.14.0 — 2026-05-28

### Added — Interactive daily attention digest with Slack action buttons

Daily digest now emits Block Kit messages with one item per ask + risk and inline action buttons:

- **Risk buttons:** ✅ Resolve · 💤 Snooze 7d · 📅 Snooze until…
- **Non-ClickUp ask buttons:** ✅ Mark closed · 💤 Snooze 7d · 📅 Snooze until…
- **ClickUp-linked ask:** single "Open in ClickUp" link button. v0.12's bidirectional close-loop is the authoritative closure path; we deliberately avoid double sources of truth by hiding Resolve/Snooze on those.

Button clicks hit a new `POST /slack-action` endpoint that returns 200 immediately (Slack's 3-second ack window), then runs the cp plan + commit + push in a background asyncio task, then updates the original Slack message in-place via `response_url` so the buttons get replaced with "✅ Resolved · 7:42 AM · `commit_sha`".

### Added — Three new plan verbs

- `resolve-risk` — flips `[escalated · ...]` or `[watching · ...]` to `[resolved · ...]` matching by `cp_hash`. Stamps `<!-- cp:resolved-at=YYYY-MM-DD -->` and optional `<!-- cp:closed-by=<source> -->` audit markers. Hash-not-found is a silent no-op (matches Task 1.1 dedupe pattern) — Slack-button re-clicks on stale messages don't pollute `errors`.
- `snooze-ask` / `snooze-risk` — append (or replace) a `<!-- cp:snoozed-until=YYYY-MM-DD -->` HTML-comment marker on the bullet matching `cp_hash`. CRITICAL placement: marker is inserted BEFORE the `cp:hash=` marker, NOT after — `_OPEN_ASK_RE` and `_RISK_RE` anchor `$` after `cp:hash`, so trailing markers would silently break the digest scanner. Regression-tested.

Digest scanners (`_find_past_due_asks`, `_find_escalated_risks`) now check each matched bullet for the snooze marker and `continue` if `until > today`. The marker is stripped from the captured text before the digest UI renders it.

### Added — `_write_close_ask` hash branch (Slack-button entry point)

`_write_close_ask` now accepts an optional `hash` field. When present, matches by `cp:hash` marker; otherwise falls through to existing text/match substring (backward compat — v0.12's ClickUp pipeline keeps working unchanged).

### Added — `POST /slack-action` endpoint with async background dispatch

Receives Slack `block_actions` and `view_submission` payloads. Verifies Slack's `v0:<timestamp>:<body>` HMAC scheme against new `SLACK_SIGNING_SECRET` (with 5-minute timestamp replay window). Returns `200` immediately, then runs work in `_spawn_background(...)` (asyncio.create_task with strong-reference retention to defeat asyncio's weak-ref GC). All sync calls (git subprocess, slack_sdk WebClient, requests.post) wrap in `asyncio.to_thread` so the event loop stays free.

For the "Snooze until…" button, the modal must open within the 3-second `trigger_id` expiry window, so `views.open` runs inline (still via `asyncio.to_thread`). The actual snooze happens on the subsequent `view_submission` callback, which `_spawn_background`s the same way as block_actions.

The `_post_response_url_update` helper splices the actions block out of the original message and replaces it with a context block carrying the confirmation text — buttons get visually replaced in-place after the work completes. Non-2xx Slack responses are logged but never raise (fire-and-forget UX).

### Migration notes

- Tenants bump engine pin from `~= 0.13` to `~= 0.14`.
- **New required env vars on cp-engine-webhook Railway service:**
  - `SLACK_SIGNING_SECRET` — from the Slack app's "Basic Information" page (used by `/slack-action` for HMAC verification).
  - `SLACK_BOT_TOKEN` — `xoxb-...` bot token (used by `views.open` to launch the date-picker modal).
- **Slack app configuration:** enable Interactivity & Shortcuts, set Request URL to `https://cp-engine-production.up.railway.app/slack-action`.
- `[attention_digest].recipients` in `.cp-engine.toml` MUST be Slack user IDs (not channel IDs). `views.open`'s `trigger_id` is per-user — interactive buttons require a DM context. (Already true since v0.12; reasserting because v0.14 makes it load-bearing.)
- The CLI surface `cp attention-digest --post-to-slack` is unchanged. The daily-digest cron in cp tenant continues to work as-is.

### Out of scope (deferred)

- Slash-command entry point (`/cp snooze ibx-5167 1w`) — Block-Kit buttons cover the digest case; slash commands cover the "from any context" case, which isn't on the table yet.
- "Unsnooze" button — if Drew snoozes by mistake, hand-edit the marker out of the sprint file. Add only if the use case proves real.
- Channel posts — digest stays in DM. Channel posting requires different auth scopes + UX considerations.
- Audit trail UI — `cp:resolved-at` + `cp:closed-by=slack` markers are in the bullet markdown; a queryable audit page lives in dashboard territory, not engine territory.

## v0.13.0 — 2026-05-27

### Fixed — auto-ingest no longer drops content when target sprint file is missing

`execute_plan` now auto-scaffolds the target sprint file from the most recent prior sprint file for the same project when the target file is absent. Previously this was the single largest source of auto-ingest failures (5 of 12 historic failures pre-v0.13), all triggered by the late-in-week roll-forward in `_planning_monday` — Wed-Sun meetings tried to land in next week's sprint dir, which sync hadn't created yet.

- New `scaffold_from_prior(tenant_root, project_code, target_week_iso)` helper in `sprints.py`. Walks `sprints/*/<code>.md`, picks the most recent prior week, derives a minimal `ProjectState` from the prior file's frontmatter + `← [Project CP]`/`[Initiative CP]` navigation link (no MC-2 dependency), and renders the new file via the existing `render_sprint_scaffold` + Jinja templates. Supports 1p client engagements, FPSF/Canonic initiatives, and standalone repos.
- `execute_plan` calls `scaffold_from_prior` when `sprint_path.exists()` is False. Falls back to logging the error (as before) only when no prior sprint file exists for the project — first-ever-ingest edge case.

### Added — `POST /api/auto-ingest/runs/:run_id/rerun` for failed runs

Failed `auto_ingest_runs` rows are now re-runnable. The endpoint loads the row, extracts `meeting_id` + `project_codes`, and re-fires the pipeline via a new shared `_perform_auto_ingest` helper (extracted from `/api/auto-ingest`). Writes a NEW row (keeps the original as a failure record for diagnostics). HMAC-signed against `WEBHOOK_HMAC_SECRET`; only `status='failed'` rows may be re-fired.

The matching `fathom-meeting-sync` release adds a "Rerun" button on failed-status rows in the Auto-ingest activity panel. The dashboard's webhook-server proxy holds the HMAC secret and forwards to cp-engine-webhook.

Recovers historic Class 2 (Anthropic 529) + Class 3 (Anthropic usage-limit) failures, plus any remaining Class 1 failures from before auto-scaffold shipped.

### Migration notes

- Tenants bump engine pin from `~= 0.12` to `~= 0.13`.
- No new env vars or secrets on cp-engine.
- For the Rerun button to work end-to-end, deploy the matching fathom-meeting-sync release (server proxy + dashboard) and ensure `CP_AUTO_INGEST_URL` + `CP_AUTO_INGEST_HMAC_SECRET` are set on fathom-webhook-server (they already are if auto-ingest itself is working).

### Out of scope (deferred)

- Retry-with-backoff for Anthropic 529s — the rerun button gives manual recovery. Automate later if 529s recur.
- `rerun_of_id` column on `auto_ingest_runs` — inferable from order + project_codes for now. Add when a rerun-chain UI emerges.
- Rerun for non-failed runs — risky with hand-edited sprint files.

## v0.12.1 — 2026-05-27

### Fixed — `/clickup-task-closed` matches ClickUp's real webhook payload shape

Task 1.7 (shipped in v0.12.0) was built against an assumed payload shape that included the task `description` — we regexed the `[cp:hash=...]` trailer out of it to find the matching cp ask. ClickUp's real `taskStatusUpdated` payload doesn't include `description`, so the v0.12.0 endpoint would have 204'd on every real webhook call.

Refactored to match the actual payload:

- **Routing:** look up `cp_ask_hash` + project code from `clickup_task_proposals.clickup_task_id` (which the dashboard already stored at task-creation time) instead of trying to extract them from a `description` field that isn't there. One Supabase SELECT, no ClickUp API round-trip.
- **Signature header:** `X-Signature` (previously had `X-Webhook-Signature` wrong). Algorithm unchanged — HMAC-SHA256 hex against `CLICKUP_WEBHOOK_SECRET`.
- **Event filtering:** `taskStatusUpdated` fires on any status change. Now filter on `history_items[0].after.type == "closed"` so non-close transitions return 204 without doing any lookup or ingest work.
- Dropped `_lookup_project_for_hash` and the `_CP_HASH_RE` regex; added `_lookup_proposal_by_clickup_task_id(task_id) -> tuple[cp_hash, code] | None`. Tests rewritten to drive the new payload shape (7 cases covering happy path, non-closed status, missing task_id, bad HMAC, missing secret, orphan task, no-op rerun).

## v0.12.0 — 2026-05-27

### Added — Lever 1: bidirectional ClickUp (close a task → flip the cp ask)

When a ClickUp task created from a cp ask is marked done, cp now flips the matching ask to `[closed]` automatically. The full round-trip:

1. Fathom meeting auto-ingests. Each Fathom `action_item` becomes a deterministic `record-ask` in the sprint file with a `<!-- cp:hash=abc12345 -->` marker AND a row in `clickup_task_proposals` carrying the same `cp_ask_hash` column.
2. Dashboard reviewer approves the proposal. Dashboard's `buildDescription` appends `[cp:hash=abc12345]` to the ClickUp task description.
3. Someone marks the ClickUp task done. ClickUp fires the new `POST /clickup-task-closed` webhook. The handler regexes out the hash, looks up the owning project via `clickup_task_proposals.cp_ask_hash`, runs a `close-ask` plan with `closed_by=clickup`, commits + pushes.

**Code changes:**

- New helper `_action_items_to_ask_items` in `plan_from_transcript.py` converts Fathom's `action_items` JSONB into deterministic `record-ask` plan items. The hash recipe matches `ingest._content_hash(code, "record-ask", text)` exactly so `_write_ask`'s dedupe recognises re-ingests as no-ops AND the ClickUp round-trip works.
- `generate_plan()` gains an optional `action_items` kwarg. When provided, the helper-built items are merged into `plan["projects"][code]["record-ask"]` AFTER `_validate_plan` runs. **Architectural shift:** Fathom's structured `action_items` are now the canonical source of asks; the LLM still surfaces additional contextual asks from the transcript, but the action-items stream is authoritative and roundtrippable.
- `webhook/main.py`'s `/api/auto-ingest` fetches the meeting row ONCE (previously called twice — for ingest and artifact generation separately) and threads `action_items` through `_ingest_one_project`. Account-meeting and sprint-planning endpoints intentionally NOT updated — Fathom's `action_items` JSONB has no per-project attribution.
- `_write_close_ask` gains an optional `closed_by` field. When present, appends a trailing `<!-- cp:closed-by=<source> -->` HTML-comment marker on the flipped bullet for audit.
- New `_build_proposal_row` in `webhook/clickup_propose.py` populates `cp_ask_hash` using the same `_content_hash` recipe as ingest. Refactored from the inline row-build loop.
- New endpoint `POST /clickup-task-closed` on cp-engine-webhook. HMAC-validated against new env var `CLICKUP_WEBHOOK_SECRET` (separate from Fathom's `WEBHOOK_HMAC_SECRET` so they rotate independently). Returns 200 on success or orphan, 204 on no hash, 401 on bad signature.

**Schema change:**

- Supabase migration adds `clickup_task_proposals.cp_ask_hash text` (nullable) with a partial index on non-null values. Migration name: `add_cp_ask_hash_to_clickup_task_proposals`. Applied to MC-2's Supabase project.

**Companion repo:**

- `fathom-meeting-sync/fathom/clickup-tasks.js` reads `cp_ask_hash` from the proposal row and appends `[cp:hash=<8-hex>]` as the last line of the ClickUp task description. Backward compatible — proposals without a hash produce a description without the trailer.

### Added — Lever 2: daily attention digest

A new `cp attention-digest` CLI command scans the current sprint dir for past-due asks + recently-escalated risks and prints a markdown digest. With `--post-to-slack`, sends as a DM to configured recipients. A new GitHub Action `daily-digest.yml` runs daily at 14:00 UTC (7am PT) and posts to Drew automatically.

**Past-due definition:**
- `· by <date>` set AND `<date>` < today (immediately overdue), OR
- No `by` date AND asked at least N days ago (default 7 — stale).

**Escalated-risk window:** risks with `severity=escalated` AND `raised` within the last N days (default 7).

**Code changes:**

- New module `src/cp_engine/attention_digest.py` with `_find_past_due_asks`, `_find_escalated_risks`, `compose_digest`, `attention_digest` (orchestrator), and `_post_digest_to_recipients` (Slack DM helper).
- New CLI subcommand `cp attention-digest [--post-to-slack] [--recipient NAME] [--today YYYY-MM-DD]`. Default prints to stdout for local preview.
- New `post_dm(client, *, user_id, text)` helper in `src/cp_engine/slack.py`. Slack auto-opens a DM when `chat.postMessage` is called with `channel=<user_id>`.
- New `[attention_digest]` config block on `.cp-engine.toml`. All fields optional with sensible defaults. Type-validated via `CommittedConfigInvalid`. Mirrors how `[team]` and `[risk_categories]` are parsed.
- Orchestrator falls back to the previous ISO week's sprint dir when the current week's dir doesn't exist yet (Monday-morning case where no meeting has scaffolded the new sprint dir).

**Config shape:**

```toml
[attention_digest]
recipients = ["U12ABCDE"]            # Slack user IDs; empty disables --post-to-slack
past_due_threshold_days = 7          # no-by asks become "stale" after this many days
escalated_window_days = 7            # risks escalated within this window count as "recent"
allocation_cap_hours = 50            # reserved for Lever 3 (allocation watch); not yet used
post_when_clear = true               # post "all clear" affirmation when nothing's flagged
```

**Companion repo:**

- cp tenant gains `.github/workflows/daily-digest.yml`. Cron: `0 14 * * *`. Reuses existing secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SLACK_BOT_TOKEN`) — no new secrets needed.

### Migration notes

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.11` to `~= 0.12`.
2. Add the `[attention_digest]` block with at least `recipients = ["U..."]` if you want Slack DMs.
3. **For Lever 1:** set `CLICKUP_WEBHOOK_SECRET` on the cp-engine-webhook Railway service, subscribe ClickUp's "task closed" event to `https://cp-engine-production.up.railway.app/clickup-task-closed`, deploy the matching `fathom-meeting-sync` commit (the `[cp:hash=...]` description trailer).
4. **For Lever 2:** enable `daily-digest.yml` in the tenant's GitHub Actions. First scheduled fire happens at 14:00 UTC the day after enable.

### Known limitations

- **Initiative-sourced ClickUp tasks:** `clickup_propose._resolve_project` returns an initiative's `id` when `initiatives.enable_clickup=true`, but `clickup_task_proposals.project_id` has an FK constraint to `projects.id`. An initiative-sourced proposal would fail to insert today. **Latent today** (verified: zero orphan rows in production), but worth flagging — a fix needs either an alternate FK + lookup path OR a routing change in `clickup_propose`. Engagement-sourced ClickUp tasks (the vast majority) are unaffected.
- **Allocation watch:** `[attention_digest].allocation_cap_hours` is wired through config but the classifier always returns an empty list. Allocation is reserved for Lever 3+. The digest renders 0 lines for allocation today.
- **DST:** the daily-digest cron is at 14:00 UTC = 7am PT during DST. After standard time kicks in (Nov), the effective time shifts to 6am PT. If 7am-PT-year-round matters, dual-cron with idempotent posting is the right fix.

### Tests

- Test suite: 538 passing (was 483 at v0.11.0). 55 new tests across 7 files:
  - 5 in `test_plan_from_transcript.py` (action_items helper + merge)
  - 3 in `test_ingest.py` (close-ask closed_by marker)
  - 3 in `test_clickup_propose.py` (cp_ask_hash on proposal rows)
  - 8 in `test_webhook_clickup_closed.py` (new endpoint, including null-history-data safety)
  - 22 in `test_attention_digest.py` (classifiers, composer, orchestrator, sprint-dir fallback, recipients posting)
  - 3 in `test_slack.py` (post_dm helper)
  - 4 in `test_cli_attention_digest.py` (CLI subcommand)
  - 7 in `test_config.py` (attention_digest block + type validation)

## v0.11.0 — 2026-05-26

### Changed — Quick Resume becomes engine-managed (Lever 5)

**Structural change — tenants must opt in by bumping their `engine` pin AND accepting that auto-ingest will overwrite hand-written `**Current work:**` content over time.** Project `cp.md`'s `## Quick Resume` block becomes a fully engine-managed region wrapped in `<!-- cp-engine:start quick-resume -->` markers. Auto-ingest writes `**Current work:**`, `**Next up:**`, and `**Blockers:**` lines via three new scalar verbs on every meeting that touches the project. LLM is the source of truth.

**Trigger:** pipeline projects (Deal-stage) had blank `One-line summary` cells in master-cp because their template-placeholder `**Current work:**` lines never got hand-filled. Auto-managing the field fixes this class of staleness durably — pipeline projects never show empty cells once their first meeting fires; active projects' summaries stay fresh week-by-week instead of going stale between hand-edit passes.

**Engine changes:**

- `sync._ensure_quick_resume_markers` wraps the existing `## Quick Resume` section in engine markers on the cutover sync. Idempotent — runs once per project after the upgrade, then no-ops forever. Pre-existing hand-written content gets wrapped as-is; next auto-ingest will overwrite individual lines.
- `project-cp.md.j2` template scaffolds new projects with markers from the start.
- Three new ingest verbs (`current_work`, `next_up`, `blockers`) are **scalar** (single string per project, not list of dicts). `null` value means LLM declined to refresh — leave prior line alone.
- `ingest.execute_plan` branches on verb shape: scalar QR verbs write to project `cp.md` via `_write_quick_resume_verb`; existing list-typed verbs continue writing to sprint files. Both types appear in the same plan; both files appear in `files_written` on the same `[auto-ingest]` commit.
- `_resolve_project_cp_path` locates the project `cp.md` by code under any scope (handles 1p account-nested + FPSF/Canonic flat) without importing sync (avoids circular dependency).
- `plan_from_transcript` prompt templates (engagement + initiative) gain a Quick Resume schema block. The LLM evolves the prior `**Current work:**` / `**Next up:**` / `**Blockers:**` values with new signal from the meeting, returning `null` when the prior still describes state.

**Test surface:** 13 new tests (5 for sync marker wrapping, 1 for fresh scaffold, 7 for QR verbs). 481 passing total. No regressions.

### How tenants upgrade

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.10` to `~= 0.11`.
2. Run `cp sync` once. The cutover migration wraps `## Quick Resume` in markers across all project `cp.md` files (content unchanged on this pass).
3. Single commit: `[cutover] Quick Resume wrapped in engine-managed markers (v0.11.0)`.
4. Next auto-ingest fires per project → that project's Quick Resume gets first LLM-composed values. Over ~2 weeks of normal meeting flow, every active project transitions to LLM-composed Quick Resume content.

**Existing hand-written Quick Resume content WILL be overwritten** by the LLM's version on next auto-ingest. This is by design — Quick Resume is now machine-managed. Git history preserves prior hand-written versions; `git blame` recovers any specific line.

**Tenants who don't want auto-managed Quick Resume:** stay on `~= 0.10`. Auto-ingest still writes sprint files only; project `cp.md` Quick Resume stays hand-territory. Master-cp's One-line summary column still derives from hand-written `**Current work:**` lines.

**Rollback:** revert the tenant cutover commit + bump pin back to `~= 0.10`. Git history preserves the prior Quick Resume content.

Design: `cp/docs/plans/2026-05-26-quick-resume-engine-managed-design.md`.

## v0.10.1 — 2026-05-26

### Added — Sprint planning prep Step 0: empty-summary sweep

`CLAUDE.md.j2` template's Sprint planning prep playbook gets a new
Step 0: scan master-cp's active tables for empty `One-line summary`
cells and fill the corresponding project's `**Current work:**` line
before continuing prep. Runs FIRST so the rest of the prep walks a
fully-populated master-cp.

Triggered by a recurring pattern: pipeline projects (Deal stage) get
scaffolded with a template-placeholder `**Current work:**` line
(`_<what's in flight right now>_`) that `_derive_summary` correctly
reads as empty, leaving the master-cp summary cell blank until
someone hand-fills the line. The placeholder pattern means new
pipeline projects can sit summary-less indefinitely.

This is a process fix (CLAUDE.md guidance only) — engine logic
unchanged. A future v0.10.x can layer in a sync-side fallback
(synthesize `<Stage> — <Account> pipeline; owner <Name>` when the
template placeholder is present) so pipeline projects never show
empty cells even before sprint planning runs. Filed as backlog.

### How tenants upgrade

`~= 0.10` pins pick this up automatically (patch bump). Next `cp sync`
re-generates `CLAUDE.md` with the new playbook step.

## v0.10.0 — 2026-05-26

### Changed — sprint week numbering cuts over from Python `%W` to ISO 8601 (#7)

**Structural change — tenants must opt in by bumping their `engine` pin AND renaming `sprints/<W##>/` dirs.** Pre-v0.10.0 cp-engine used Python's `%W` for sprint week labels. For all of 2026, `%W` produces ISO_week − 1 — a one-week mismatch with calendars, Slack, Google Calendar, MC-2's financials surfaces, and any other tool a tenant operator opens. The discrepancy bit Drew on 2026-05-26 during sprint planning prep (mentally thinking "this is W22" while cp wrote into `sprints/2026-W21/`).

Internal contradictions already existed: `cli.py:_parse_week_iso` used `fromisocalendar` (ISO), `sprints.current_sprint_week_iso` used `%W`. So `cp slack-digest --week 2026-W22` would fetch ISO W22 but try to land it in `sprints/2026-W21/`. This release unifies on ISO 8601 throughout.

**Engine changes:**

- `sprints.current_sprint_week_iso` + `prior_sprint_week_iso`: `monday.strftime('%W')` → `monday.isocalendar()`. Uses ISO year (`isocalendar().year`) for correct Jan-boundary handling — Jan 1 2027 belongs to ISO `2026-W53` because that Friday is in 2026's last week per ISO.
- `aggregators._carry_forward_rollup`: same `%W` → `isocalendar()` fix for the horizon-decision-window math (was using a stale week number that disagreed with the file labels).
- Module docstrings in `sprints.py` + `ingest.py` updated to note the cutover.
- Anchor logic UNCHANGED: Mon/Tue → "this Monday's week", Wed-Sun → "next Monday's week". Only the number written into the label changes.

**Test surface:** 3 new tests in `test_sprints.py` (planning-anchor under ISO + new year-boundary coverage). ~100 hardcoded `2026-W##` test fixtures bumped +1. **468 passing total**, no regressions.

### How tenants upgrade

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.9` to `~= 0.10`.
2. Rename `sprints/2026-W##/` dirs +1 (since for 2026 every `%W` week = ISO − 1):
   ```bash
   for old in sprints/2026-W*; do
     [ -d "$old" ] || continue
     num=$(basename "$old" | sed 's/2026-W//')
     new=$(printf "W%02d" $((10#$num + 1)))
     git mv "$old" "sprints/2026-$new"
   done
   ```
3. Hand-fix cross-references in `weekly-cp.md` and any other handwritten files that mention `[W##]` or `2026-W##`. Engine-managed regions auto-refresh on next sync.
4. Run `cp sync` to verify; commit; push.

MC-2's `toWeekIso` flips to ISO concurrently (companion PR). Database values written pre-cutover stay as-is — they describe the week they were written for under the convention of that time. Cutover date for any audit: **2026-05-26**.

Design: `cp/docs/plans/2026-05-26-iso-week-cutover-design.md`.

## v0.9.1 — 2026-05-23

### Fixed — `link-local` and `capture-session` for multi-repo engagements (#5, #6)

`discover_cp_working_dirs` only globbed `_repo.md` (singular), so working dirs for multi-repo engagements (which have one `_repo-<name>.md` per linked repo, no singular file) were invisible to both `cp link-local` and `cp capture-session`. Captures for those source repos fell through to `cp/exceptions/` even though the repos WERE tracked in MC-2.

Concrete on the live tenant: 5 captures for `ggl-5136-events-calendar` had piled up in `cp/exceptions/` over ~2 weeks (2026-05-11 → 2026-05-23). Same shape affects any multi-repo engagement and `mc-2` / `market-leadership-scorecard` (linked from initiative dirs).

**Fix 1 (commit `c46f3a8`):** glob `_repo*.md` with a name-check guard so accidental matches like `_repository.md` are filtered. Each `_repo-<name>.md` yields its own `CpWorkingDir` entry pointing at the engagement dir; two source repos sharing one engagement dir each get a `.cp-link` targeting that shared dir.

**Fix 2 (commit `dbd0ba4`):** surfaced by Fix 1 — repos that have BOTH a standalone working dir AND appear as an initiative-linked `_repo-<name>.md` reference (e.g. `cp-engine` standalone in `firstpersonsf/cp-engine/` AND linked from Mission Control) yielded two `CpWorkingDir` entries with different paths; the prior dict-by-name builder silently picked whichever came last in rglob order — non-deterministic. Two-pass dedup now prefers the singular `_repo.md` (canonical standalone working dir) over `_repo-<name>.md` (pointer/reference), per CLAUDE.md design. Falls back to the linked-only entry when no standalone exists (`mc-2`'s case). Tie-breaking for unlikely edges uses sorted-path order so results are deterministic.

Live tenant before this release: ambiguous/missing entries, captures landing in `exceptions/`. After: 10 working dirs, each repo with exactly one canonical home, deterministic across runs.

Test coverage: 8 new tests in `tests/test_link_local.py`. **467 passing total (was 459 in v0.9.0)**, no regressions.

### How tenants upgrade

`~= 0.9` pins pick this up automatically (patch bump). No tenant action needed; no migration. The next `cp sync` / `cp capture-session` runs use the fixed discovery.

To verify the fix on a multi-repo tenant: `cp link-local` should now wire `.cp-link` files for repos whose engagement dir uses the `_repo-<name>.md` form, and `cp capture-session` from those source repos should write into the project's working dir rather than `cp/exceptions/`.

## v0.9.0 — 2026-05-22

### Changed — `1p/` scope is now per-account-nested (`1p/<company>/<project>/`)

**Structural change — tenants must opt in by bumping their `engine` pin and running `cp migrate-accounts`.** Triggered by the lack of any right home for cross-project account content (account-wide stakeholders, account-level decisions, multi-project client messages). The flat `1p/<project>/` layout meant a Janet email spanning ibx-5153 + ibx-5167 had to be shoved into one of the two project dirs; there was no surface for it.

After this release, each 1P account gets its own `cp.md` plus a `messages/` sibling for cross-project comms. FPSF and Canonic scopes are unchanged — they already nest by self-company at the scope level.

Engine support:

- New `company_slug(company_name)` and `account_scope_for(project)` in `state.py` — the single source of truth for the per-account layer. Client projects' working dirs land under `1p/<slug>/<dir_slug>/`; self-company projects (FPSF/Canonic) pass through unchanged. `_project_parent_dirs(scope)` yields the per-account subdirs that sync iterates for the deactivation sweep. Per-project inactive bin sits at `1p/<company>/inactive/`.
- New `account-cp.md.j2` template: anchor block, two engine-managed regions (`account-facts`, `projects`), and a handwritten skeleton for Quick Resume / Stakeholders / Account-wide decisions / Risks / Active research. Mirrors the project-cp.md hand-vs-engine discipline.
- New `render_account_cp`, `render_account_facts_body`, `render_account_projects_body` in `render.py`. Sync scaffolds each account's `cp.md` on first sync and re-splices the two engine regions on every subsequent sync; first-scaffold immediately re-splices itself to guarantee byte-equivalence with subsequent syncs (otherwise small whitespace differences between the template's `{{ block }}` spacing and the splicer's strip-and-rejoin made every next sync re-write the file, breaking the no-op promise).
- Master-cp `active-pipeline` + `active-1p` tables gain a leading **Account** column linking to each account's cp.md. Active engagements sort by `(account_slug, code)` so projects from the same account cluster (pipeline keeps stage-progression order). `_project_view` surfaces `account_display` + `account_link` for client projects; both are `None` for non-clients so the template renders an empty cell if it ever lands there.

Migration command:

- New `cp migrate-accounts [--dry-run]` CLI. Reads MC-2 once to build a `code → (slug, display)` map, plans `git mv` for every flat `1p/<dir>/`, hard-fails before any disk writes if any dir's code can't be resolved (no silent surprises, no junk-drawer `1p/unknown/`), executes the moves, absorbs `1p/_teleflex.md` (if present) into `1p/teleflex/cp.md` under a dated `## Legacy notes (migrated YYYY-MM-DD)` heading, runs `cp sync` so account `cp.md` files scaffold, then surfaces residue hints (stale `1p/<code>` references in `weekly-cp.md`) for the operator to hand-fix.
- Idempotent: re-running on an already-migrated tree is a no-op. Account-dir detection uses a `<prefix>-<digits>(-<slug>)?` shape check — dirs that don't look like a project code are silently skipped (account dirs, stray notes), while dirs that do match but can't resolve still hard-fail.
- Doesn't auto-commit: operator reviews with `git status` / `git diff` and commits manually. Mirrors `migrate-projects-flat`'s discipline.

Test coverage: 459 passing total (32 new for Phase 1 + Phase 2). The 20 existing path-assertion tests that broke when paths gained the per-account layer were fixed mechanically.

Live migration ran on the first cp tenant 2026-05-22 — 23 dirs into 7 account directories plus a hand-pre-positioned hexagon/. Idempotent `cp sync` post-migration reports `No changes (37 projects checked).`

Designs: `cp/docs/plans/2026-05-22-account-restructure-design.md` (engine support) and `cp/docs/plans/2026-05-22-migrate-accounts-design.md` (the migration command).

### How tenants upgrade

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.8` to `~= 0.9`.
2. `cp migrate-accounts --dry-run` to preview the moves and residue.
3. Resolve any unresolved dirs surfaced by the dry-run (fix MC-2 or hand-move the dir).
4. Run `cp migrate-accounts` for real in a quiet window (disable any sync/slack-digest crons that would race; don't tag Fathom meetings during the window).
5. Review with `git status` / `git diff`, hand-fix the residue list, commit.

## v0.8.16.6 — 2026-05-22

### Added — meeting count in the sprint-facts region

The deeper-transcripts pipeline writes per-meeting artifacts into each project's `meetings/` dir, but nothing pointed at them — the design deliberately omitted a central index, which left the folder easy to forget. This adds a discovery signal in the sprint file's `sprint-facts` region: a `Meetings` row showing how many meeting artifacts are dated within the sprint window, linking to the project's `meetings/` dir.

The row is sprint-scoped (matching the other `sprint-facts` rows) and emitted only when the count is greater than zero, so projects with no meetings get no dead link. It lives inside the `sprint-facts` region, which sync already re-splices every run, so it self-updates weekly with no new sync wiring.

- `count_sprint_meetings()`: pure helper counting `<YYYY-MM-DD>-<slug>.md` artifacts whose filename date falls in a sprint window. Filename-only — no file reads, no network; ignores `.txt` siblings and stray `.md`.
- `render_sprint_scaffold()`: new `meetings_this_sprint` param + relative link to the project's `meetings/` dir.
- `sprint-cp.md.j2` + `initiative-sprint.md.j2`: conditional `Meetings` row.
- `ensure_sprint_file()`: resolves the project's `meetings/` dir from `sprint_root.parent` and passes the windowed count through.

## v0.8.16.5 — 2026-05-19

### Added — `storyos-mc` sprint planning scope (Phase D.7)

Drew + Tony's standing weekly Canonic + Mission Control product/engineering sync was a mixed-scope meeting (StoryOS is Canonic; Mission Control is FPSF). The Phase D.5 sprint-planning scopes were per-org (`1p` / `fpsf` / `canonic`), so mixed-scope meetings either fanned too broadly (FPSF would hit all 5 initiatives) or had to fall back to a multi-tag work-session that lost the tenant-wide summary.

Add a new named scope `storyos-mc` that maps to a fixed pair of initiative codes via the new `_SCOPE_TO_EXPLICIT_CODES` mapping. `list_active_for_scope` now branches on scope shape: kind-based scopes (`1p`/`fpsf`/`canonic`) iterate `read_projects` and filter by `company_kind`; explicit-code scopes iterate the listed codes and look them up directly. Future named-pair scopes (e.g. when another standing combined meeting emerges) need only one line in `_SCOPE_TO_EXPLICIT_CODES` + `_SCOPE_TO_PSEUDO_COMPANY` + `_SCOPE_LABEL`.

Matching changes:
- MC-2 migration: `fathom_meetings.sprint_planning_scope` CHECK constraint adds `'storyos-mc'`.
- fathom-meeting-sync `VALID_SCOPES` includes `'storyos-mc'`; dashboard adds a fourth Sprint Planning button + chip labelMap entry.

## v0.8.16.4 — 2026-05-19

### Fixed — slack-rollup region wasn't being spliced into existing master-cp.md

v0.8.16.3 added the `slack-rollup` region to `_MASTER_REGIONS` + the first-pass exclusion list, but the second-pass `_write_if_changed` call hardcoded `splice_regions=("agenda", "sprint-facts-strip")` — so `slack-rollup` was never offered to the splicer. Result: on existing tenants the region's markers never appeared in master-cp.md and the rollup content never landed. Fixed by adding `"slack-rollup"` to the second-pass tuple. Schema-evolution rewrite path now triggers correctly when the existing master-cp.md is missing the marker.

## v0.8.16.3 — 2026-05-19

### Added — Phase D.6: Slack digest visibility in cross-cutting surfaces

The weekly Slack digest cron lands `### Slack digest` bullets in each project's current sprint file. Until now, those bullets were only visible inside each individual sprint file — sprint planning prep couldn't see them without opening every file.

- **New engine-managed region `## Recent Slack activity` in master-cp.md.** Reads the most recent `[<W##> · Slack]` bullet from each active project's current sprint file and surfaces them in a fold-out `<details>` block. Sorted by scope + project code. Hidden entirely when no project has a digest this week (quiet sprint, cron hasn't run, etc.).
- **New helper `cp_engine.render._compute_slack_rollup(tenant_root, projects, current_sprint_iso)`** does the extraction via regex (`_SLACK_DIGEST_RE`). Takes the last-matched bullet per file to be defensive against duplicate digest runs. Returns None when nothing matches.
- **Sync wires the new region into the post-sprint-files second pass** (alongside `agenda` and `sprint-facts-strip`) since it reads sprint file content.

### CLAUDE.md template — sprint planning prep section

New `## Sprint planning prep` section in Part 2 documents the prep workflow durably: read open asks, decisions, risks, **most recent Slack digest**, cross-cutting items from weekly-cp.md, and last-week allocations across all active items in the target scope. Then tag the Fathom recording for auto-ingest after the meeting runs.

4 new tests cover `_compute_slack_rollup`'s extraction, the "no projects have digests" case, missing sprint dirs, and the last-bullet-wins behavior for duplicate weeks.

## v0.8.16.2 — 2026-05-18

### Fixed — three coupled bugs surfaced by 1P sprint planning

Live failure path: tagging the 2026-05-18 1P Weekly Scrum as `sprint_planning_scope='1p'` ran `generate_sprint_planning_plan` against 20 active engagements; the response blew past the 4096-token cap and got truncated mid-output. Three fixes:

- **`_call_claude` max_tokens bumped to 16384.** The 4k ceiling was tuned for single-project plans; multi-project (account meetings, sprint planning) routinely needs 8k+. Anthropic bills on actual output, not max — no cost penalty for the headroom.
- **`_call_claude` raises a specific `PlanGenerationError` when `stop_reason == 'max_tokens'`.** Before, truncation cascaded silently into a YAML parse error downstream. Now the caller sees "response truncated at max_tokens — try a larger max_tokens budget or reduce prompt scope."
- **`_extract_yaml` recovers from truncated responses.** When the opening ` ```yaml ` fence is present but the closing fence isn't (truncation, model forgot, etc.), strip the opening fence and return the partial — let YAML parsing decide what to do with it, rather than dumping backticks into yaml.safe_load and getting a column-1 parse error.
- **`webhook._log_run_to_supabase` accepts a new `top_level_errors` parameter.** The two `status="failed"` paths (account / sprint-planning plan generation) now forward the AccountPlanError message into `auto_ingest_runs.errors` so the dashboard observability panel can show what actually went wrong. Before, `ingested=[]` failures landed with `errors=null` — opaque without Railway logs.

4 new tests cover the YAML extractor's three cases (complete fence, opening-only truncation, no fence, no-language-tag fence).

## v0.8.16.1 — 2026-05-15

### Changed — CLAUDE.md template restructured + updated for Phase D

The session-protocol doc was written pre-initiatives and pre-Phase-D. Several sections were stale (`is_internal` framing, working-tree example missing initiative dirs, no mention of account meetings, sprint planning, account summaries, or the auto-ingest pipeline at all).

Restructured into two clean parts:
- **Part 1 — What's in the tree.** Three kinds of work (engagements, initiatives, standalone repos), working tree layout (now showing initiatives + linked repos explicitly), the four meeting-assignment shapes (single project, account meeting, sprint planning, untagged), tenant-wide surfaces in `weekly-cp.md`, engagement-vs-initiative sprint file shape, local-link traversal.
- **Part 2 — How to read and edit it.** Reading modes, mode-switching, gatekeeper rule, trigger phrases, reference style (now with initiative + repo variants — "Updates on **Mission Control**?" not "**mission-control Mission Control**"), hand-written vs. engine-managed sections, deepening from transcript, word-count discipline.

The new tree example shows real initiative dirs (mission-control, storyos, first-person-website, etc.) so the layout is concrete. Reference style explicitly covers the three kinds.

No behavior change. Tenant CLAUDE.md regenerates on the next `cp sync`.

## v0.8.16 — 2026-05-15

### Added — Phase D.5 tenant-scope sprint planning

Sister concept to Phase D.4 account meetings. Where account meetings are per-company, sprint planning is per-scope: 1P sprint planning touches every active client engagement; FPSF/Canonic sprint planning touches every active initiative for that self-company. The user picks ONE scope; the webhook fetches the appropriate active project list at ingest time.

- **New helper `list_active_for_scope(config, scope)`** in `cp_engine.plan_from_account_meeting`. Three scopes mapped via `_SCOPE_TO_KIND`: `'1p'` → all active client engagements; `'fpsf'` → all active self-fpsf initiatives; `'canonic'` → all active self-canonic initiatives. Sister to `list_active_for_company`.
- **New `generate_sprint_planning_plan()`** mirrors `generate_account_plan()` but uses a sprint-planning-framed prompt and stamps the `account_summary`'s `company` field with a pseudo-code (`1p-clients`, `fpsf-internal`, `canonic-internal`) so it's distinguishable from per-account summaries in `weekly-cp.md`'s `## Account summaries` section.
- Server-side defaults stamp `company` + `week` on every emitted `account_summary` and `account_decisions` item so the prompt doesn't have to (defensive injection mirroring the account flow).

### Phase D.5 webhook

`POST /api/auto-ingest-sprint-planning` lives in `webhook/main.py`. Same auth, same per-project commit pattern as `/api/auto-ingest-account`. One additional commit lands the sprint-planning summary to `weekly-cp.md`. Payload: `{meeting_id, scope, transcript_text}` — much smaller than the account variant since scope is a literal three-value enum.

## v0.8.15 — 2026-05-14

### Added — Phase D.4 account meetings

Implements the engine half of `cp/docs/plans/2026-05-14-account-meetings.md`. Account meetings (weekly client syncs touching N engagements, weekly internal-team syncs touching N initiatives) now have first-class support: the user picks ONE company in the dashboard; the webhook fetches the active project list at ingest time and routes content per-project via one Claude call.

- **New verb `record-account-summary`** lands a paragraph bullet per `(company, week)` in `weekly-cp.md`'s new `## Account summaries` section. Section auto-creates if missing. Hash key embeds the week so re-runs are idempotent. The narrative companion to the existing one-liner `account_decisions` flow.
- **New module `cp_engine.plan_from_account_meeting`** — `generate_account_plan()` takes the company + transcript + active project list and asks Claude to route per-project verbs in a single pass. Output is the existing `cp ingest` multi-project plan shape PLUS an `account_summary` block PLUS the existing `account_decisions` block. Server-side injection stamps `company` + `week` on every account_* item so the prompt doesn't have to.
- **New helper `list_active_for_company(config, company_code)`** returns active engagements for client companies and active initiatives for self-fpsf/self-canonic companies, in one call.
- **`_validate_plan` accepts the new `account_summary` block** (single dict or one-element list, mirroring how `account_decisions` is shaped).
- **5 new tests** for the section auto-create, append-to-existing, idempotency, per-week dedup, and required-field validation.

### Phase D.4 webhook

The matching `POST /api/auto-ingest-account` endpoint lives in `webhook/main.py`. Same auth (HMAC), same Supabase observability, same per-project commit pattern as `/api/auto-ingest`. One additional commit lands `## Account summaries` to `weekly-cp.md` after all per-project commits.

## v0.8.14.1 — 2026-05-14

### Added — Initiative-shaped prompts for auto-ingest (Phase 2)

`plan_from_transcript._build_prompt` now branches on whether the target project is an engagement (`<3-letter>-<digits>` code) or an initiative (slug code). Initiative prompts:

- Drop `inbound` and `stakeholders` from the schema — no client side, no net-new external contacts.
- Emphasize `decisions`, `risks`, and `asks` (team-to-team open loops) as the load-bearing verbs.
- Use "internal workstream" framing instead of "client engagement."

The engagement path is unchanged.

New public helper: `plan_from_transcript._is_engagement_code(code)`. Used to discriminate without a separate `source` parameter.

This makes the auto-ingest webhook (`cp-engine-webhook`) correctly handle initiative meetings: when the dashboard's project-assignment dropdown (Phase D.1) writes `project_tags: ["mission-control"]`, the trigger fires, the webhook calls `generate_plan(project_code="mission-control")`, and the resulting plan is initiative-shaped.

## v0.8.14 — 2026-05-14

### Added — Internal initiatives as a first-class workstream

Implements Phase 1 of `docs/plans/2026-05-14-internal-initiatives.md`. Initiatives are internal workstreams (Mission Control, StoryOS, First Person Website, First Person Sales, Market Scorecard, First Person Operations) that don't fit the engagement shape (no client, no budget) and don't fit the standalone-repo shape (an initiative may span 0, 1, or N repos). They sit alongside engagements and repos as a third top-level entity.

- **`EntrySource` extended** with `"initiative"`. A ProjectState with `source="initiative"` is read from the new MC-2 `initiatives` table.
- **`sync_mc2.read_projects()`** now returns three streams: engagements + standalone repos + initiatives. Standalone repos are repos with both `project_id IS NULL` AND `initiative_id IS NULL` — initiative-linked repos surface as `_repo-<name>.md` under their initiative's working dir, mirroring engagement-linked repos.
- **New initiative templates** (`initiative-cp.md.j2`, `initiative-sprint.md.j2`). Slimmer than engagement templates — drop "Client communication" / "Outbound" / "Inbound" / "Stakeholders" (no client side), keep "Open asks" + "Slack digest" + "Decisions" + "Risks" + the standard sprint/notes structure. `render_project_cp` and `sprints.render_sprint_file` branch on `source == "initiative"` to pick the right template.
- **`master-cp.md` rollup** gains two new managed regions: `active-fpsf-initiatives` and `active-canonic-initiatives`. Initiatives appear in sibling tables below their scope's existing engagement/repo tables, with initiative-shaped columns (Code / Initiative / Owner / Status / Last touched / One-line summary / CP).
- **`cp slack-channels` + `cp slack-digest`** discriminate on the new `ChannelMapRow.kind` field (`"engagement"` vs `"initiative"`). The active-filter accepts `"Active"` (initiative status) alongside `"Deal" | "Open"` (engagement status). Initiatives with `slack_channel_ids` get the same weekly-digest treatment.
- **`cp list-active-projects`** surfaces initiatives as `source: "initiative"` rows. Used by the Fathom dashboard in Phase 2.
- **New initiative status vocabulary** in `status.py`: `INITIATIVE_STATUSES = ("Active", "On hold", "Done", "Archived")`. Parallel to `MC_STATUSES` for engagements. `is_active_initiative_status()` returns True only for `"Active"`.

### MC-2 migration

Migration `add_initiatives_table` (applied 2026-05-14) creates the `initiatives` table, adds `repos.initiative_id` FK, and seeds six rows: Mission Control + Market Scorecard + First Person Website / Sales / Operations (FPSF) and StoryOS (Canonic). Four repos linked: `mc-2` + `cp-engine` → mission-control; `storyos` → storyos; `market-leadership-scorecard` → market-scorecard.

### Operational notes

- Repos with `initiative_id` set no longer appear as top-level standalone-repo working dirs. Existing standalone-repo dirs for `mc-2`, `cp-engine`, `market-leadership-scorecard`, `storyos` are auto-deactivated to `<scope>/inactive/` on first sync after this migration; their content has been preserved there. The canonical home is now `<scope>/<initiative-code>/_repo-<name>.md`.
- The `canonic/storyos/` directory has a naming collision (initiative slug `storyos` matches the prior standalone-repo slug). The old `cp.md` and `_repo.md` remain alongside the new initiative-shaped scaffolding; clean up manually if desired.

## v0.8.13 — 2026-05-13

### Added — Multi-channel Slack digests

Projects can now have multiple Slack channels (e.g. a main channel + a `_team` internal channel). The digest pipeline fans out across all of them and produces one weekly bullet with one labeled paragraph per channel. Schema change on MC-2 (new `projects.slack_channel_ids` JSONB column) drives the fan-out; the legacy scalar `slack_channel_id` stays in place as a "primary channel" pointer for display purposes.

- **`fetch_channels(token, channel_ids, week_start)`** — new public function in `cp_engine.slack`. Fans out `fetch_week` across N channels, sharing one WebClient + user/channel-name caches across all calls. Returns a list of `FetchedChannel(channel_id, channel_name, messages)`. `fetch_week()` is now a thin single-channel wrapper.
- **`ChannelMapRow.channel_ids: tuple[str, ...]`** replaces the single `channel_id` field. The dataclass also exposes `primary_channel_id` + `primary_channel_name` for display; the digest pipeline does NOT special-case the primary.
- **`generate_slack_plan(channels=[...])`** takes a list of `FetchedChannel`s instead of a single channel + message list. The prompt describes each channel by name and asks Claude to emit one labeled paragraph per channel (e.g. `**#ibx_5167_ddi_platform_video**: <para>` followed by a blank line and `**#ibx_5167_ddi_platform_video_team**: <para>`) inside the single `slack_digest` entry's `text` field.
- **`cp slack-channels`** table format updated to show `#CH` (channel count) and a comma-separated list of channel IDs per project. JSON output now includes `channel_ids: [...]` + `primary_channel_id` + `primary_channel_name`.
- **`cp slack-fetch`** prints one `## Channel: <id> #<name>` section per channel for multi-channel projects.

### MC-2 migration

Migration `add_slack_channel_ids_array_to_projects` adds a `slack_channel_ids JSONB NOT NULL DEFAULT '[]'` column and backfills it from the existing scalar `slack_channel_id` (1-element array for each non-null row). The admin UI does not need to change immediately — the array is populated correctly by the migration, and additional channels can be added via SQL until the UI catches up.

### Breaking

- `ChannelMapRow.channel_id` removed; use `channel_ids[0]` or `primary_channel_id`. Internal API only — no external consumers.
- `generate_slack_plan` signature change: `channel_id` + `messages` → `channels: list[FetchedChannel]`. Internal API only.

## v0.8.12 — 2026-05-13

### Added — Weekly Slack digest pipeline

- **`cp slack-channels`** — debug command that lists every active engagement project alongside its MC-2 `slack_channel_id`, `enable_slack` flag, and `mc_status`. Used to spot projects that need a channel-id backfill before turning on the digest cron. Supports `--active-only` (Deal | Open) and `--format json`.
- **`cp slack-fetch --code <code> --week <YYYY-W##>`** — pulls one ISO week of top-level messages from a project's Slack channel. Filters bots, channel-join/leave/topic messages, and thread replies; resolves `<@U…>` mentions to display names. Caches user lookups for the run.
- **`cp slack-digest --week <YYYY-W##>`** — generates a `cp ingest` plan from one week of Slack chatter via Claude. Always emits a digest paragraph; emits `inbound`/`asks`/`decisions`/`risks` only when the chat content qualifies confidently. Multi-project mode (no `--code`) iterates every active project with `enable_slack=true` and a `slack_channel_id`, skipping channels with zero messages. With `--apply`, lands the plan via the existing `execute_plan` plumbing (idempotent via content-hash dedup).
- **`record-slack-digest` verb** in the ingest schema. Writes one bullet per `(project, week)` under `## Client communication / ### Slack digest`. Hash key embeds the week so the same digest text in two different weeks doesn't false-dedup; re-runs for the same week are no-ops. Subsection auto-creates if missing.
- **`execute_plan(week_iso=...)` override.** The Sunday cron runs in week N+1 but writes to week N's sprint files. The new optional parameter lets callers pin the target week explicitly instead of deriving it from `today`.
- **New runtime dependency: `slack-sdk>=3.27`.** Wraps the three Slack scopes the bot uses: `channels:history`, `channels:read`, `users:read`.

### Notes

- Slack credentials resolve from `os.environ` first, then `<mc-2 clone>/backend/.env` (same dotenv fallback pattern as `_load_supabase_creds`). The GitHub Actions cron in the cp tenant repo passes `SLACK_BOT_TOKEN` + `ANTHROPIC_API_KEY` as secrets.
- The sprint-file template now scaffolds the `### Slack digest` subsection alongside the existing Outbound/Open asks/Inbound/Stakeholders blocks.

## v0.8.11.1 — 2026-05-13

### Added

- **`TenantConfig.team`** field, populated from optional `[team]\nmembers = [...]` block in `.cp-engine.toml`. Used by `plan_from_transcript` so Claude doesn't auto-add internal team members as "new" project stakeholders during auto-ingest.
- **Account decisions in `plan_from_transcript` context.** Up to 25 most-recent decision titles from `weekly-cp.md` (account-level + cross-project) are surfaced in the prompt under "Recent account-level decisions." Prevents Claude from re-emitting account-wide commitments as project-level ones.

### Fixed

- **`cp ingest-from-transcript --show-prompt`** wasn't passing `config.team` to the prompt builder, so the team block always rendered as "(No team roster declared in tenant config.)" even when configured. The non-`--show-prompt` path was already correct.

## v0.8.11 — 2026-05-13

### Added — Phase C (auto-ingest) from cascade design doc

- **`cp ingest-from-transcript --project <code> --transcript <path>`** — new CLI command (Phase C.1). Generates a `cp ingest` plan from a transcript via Claude, prints YAML for review, or applies it with `--apply`. Requires `ANTHROPIC_API_KEY`. Used as the engine for the webhook service below; also useful standalone for "deepen this one transcript without typing the plan myself."
- **`webhook/` — cp-engine-webhook FastAPI service** (Phase C.2). HMAC-signed `POST /api/auto-ingest` endpoint that auto-ingests Fathom meetings into the cp tenant: HMAC verify → clone-on-each-request via SSH deploy key → Claude plan generation → `execute_plan` → commit `[auto-ingest] <code>: meeting <id-prefix>` → push. Deploys to Railway alongside `fathom-meeting-sync`. Per-call observability writes to the new `auto_ingest_runs` Supabase table (Phase C.4).
- **New runtime dependency: `anthropic>=0.40`.** Adds ~5 MB to the install footprint. The CLI never imports it unless `ingest-from-transcript` is invoked.

### Fixed

- **Anthropic error envelope.** `PlanGenerationError` now surfaces the actual SDK error body (including `BadRequestError` details like "credit balance too low") instead of the generic `Connection error`. Found via a real credit-balance issue during C.2 testing.

### Operational notes

- Production webhook URL: `https://cp-engine-production.up.railway.app`. See `webhook/README.md` for the deploy-key + Railway-env-var setup. Phase C.3 (the fathom-meeting-sync trigger) and Phase C.4 (the `auto_ingest_runs` observability table + dashboard view) live in the `fathom-meeting-sync` repo.

## v0.8.10.2 — 2026-05-13

### Fixed

- **`cp ingest --dry-run` summary now reports `account_decisions_count`.** Was silently absent from the JSON, making it impossible to confirm the plan parsed account-level decisions correctly.
- **Plugin manifests catch up to actual release version.** `plugin/plugin.json` and `.claude-plugin/marketplace.json` had been stuck at 0.8.2 since the last seven releases bypassed `scripts/release.py` and only bumped `pyproject.toml` + `__init__.py`. The `sync-cli-version.sh` SessionStart hook reads `plugin.json` as authoritative, so it was *downgrading* fresh installs back to 0.8.2 every session. Releases must use `scripts/release.py` going forward — never hand-bump version files.

## v0.8.10.1 — 2026-05-13

### Fixed

- **`cp fathom-fetch` JSON output now includes `meeting_type`.** Bug discovered immediately after v0.8.10 ship: the dataclass had `meeting_type`, the file header had `meeting_type`, but the CLI's JSON output dict was missing it. Plugin code that depends on the JSON to route by type couldn't see it. One-line fix.

## v0.8.10 — 2026-05-13

### Added — Phase B from cascade design doc

**Account-meeting ingest workflow.** Phase B of the meeting-type cascade design (`cp/docs/plans/2026-05-12-meeting-type-cascade.md`). Builds on Phase A's `meeting_type` field.

- **`/cp-ingest --account <company>` plugin mode.** New invocation that lists candidate `account-status` meetings for a company, lets the user pick one, fetches + classifies + plans + executes against it. Per-project plan template emphasizes per-project inbound/asks/decisions PLUS a new top-level `account_decisions` block for decisions that touch the whole client account but don't belong to any single project.
- **New `account_decisions` block in the plan YAML schema** — parallel to `themes` (tenant-wide content). Each item has `{text, company, date}` and lands as a numbered entry in `weekly-cp.md`'s handwritten Decisions list with the `(YYYY-MM-DD, source: account: <company-lower>)` suffix per Q5 from the cascade design doc.
- **`_write_account_decision()` internal function** — finds the highest existing decision number in `weekly-cp.md`, appends `<N+1>. **<text>** ...` before the first `cp-engine:start` marker (keeps the entry in the handwritten section, never inside engine-managed strip regions). Idempotent via content hash on `(company, "record-account-decision", text)`.
- **`cp list-active-projects --company <code>`** (case-insensitive) — filters by `company_code`. Used by `/cp-ingest --account` to scope active projects to the company's portfolio.
- **`cp fathom-list --type <meeting-type>`** — Click `--type` choice constrained to the 6-value taxonomy. Maps to `list_meetings(meeting_type=...)` which adds `.eq("meeting_type", ...)` to the Supabase query.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`.
- New plan-block: `account_decisions`. Existing plans without it work unchanged.
- `weekly-cp.md` decisions list grows when account-meeting ingests run. The cross-reference parser in `cp_engine.agenda.parse_weekly_decisions` already handles the `(date, source: <anything>)` suffix; `decisions_for_project` will pick up `source: account: <company>` entries automatically when projects of that company query for related decisions.

### Verification

- 8 new tests in `test_ingest.py` covering: account-decision write to weekly-cp.md, insertion before engine markers (handwritten section), idempotency, renumbering when no existing decisions, required-field validation (text + company), validation rejects non-list, valid alongside other plan blocks, errors when weekly-cp.md missing. Full suite: **388 passing**.

## v0.8.9 — 2026-05-12

### Added — Phase A from cascade design doc

**`meeting_type` field on Fathom meetings.** Phase A of the meeting-type cascade design (`cp/docs/plans/2026-05-12-meeting-type-cascade.md`). Foundation for Phases B (account-meeting ingest), C (project-meeting auto-ingest), and D (recurring-meeting cadence tracker).

- **Schema migration** (in fathom-meeting-sync repo): `migrations/02_meeting_type.sql` adds the `meeting_type TEXT NOT NULL DEFAULT 'untagged'` column to `fathom_meetings` with a CHECK constraint enforcing the 6-value taxonomy: `project-status`, `account-status`, `sprint-planning`, `work-session`, `1-1`, `untagged`. Indexed for filtered queries.
- **Classifier** (in fathom-meeting-sync repo): `extract-meeting-type.js` implements best-guess classification — title patterns first (sprint-planning + account-status are most distinctive), participant analysis second (2 FP-only → `1-1`; 3+ FP-only → `work-session`), project-tag fallback last (1 tag → `project-status`; 2+ → `untagged` for human review).
- **`FathomMeetingSummary` and `FathomMeetingFull` extended** with `meeting_type: str = "untagged"` field. `cp fathom-list` and `cp fathom-fetch` surface it in JSON output. `stage_transcript()` includes `# meeting_type:` in the staged file's metadata header.
- **No behavior change yet**: meeting_type drives auto-poll routing in Phase C and the cadence tracker in Phase D. v0.8.9 just makes the field available so downstream phases have something to read.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`.
- Requires the schema migration to be applied to the Supabase project (`mgheymslksfyhuvhmvmj`). Already applied during the Phase A session via the Supabase MCP.

### Verification

- 3 new tests in `test_fathom.py` covering meeting_type in stage_transcript header, FathomMeetingSummary.to_dict, and the default value. Full suite: **380 passing**.
- 27 new tests in fathom-meeting-sync's `__tests__/extract-meeting-type.test.js` covering title-pattern matching, participant-based classification, project-tag fallback, real W19 meeting titles, and the helper functions. Full fathom-meeting-sync suite: **67 passing**.
- Live verification of the Supabase migration: all 273 existing rows defaulted to `meeting_type='untagged'` as expected; CHECK constraint and index in place.

## v0.8.8.2 — 2026-05-12

### Added

- **`cp prep-agenda --summary`** — JSON output mode emitting structured metrics: week meta, themes/decisions counts, per-section coverage (Quick Resume / inbound / cross-referenced decisions), urgency counts, and `workload_by_owner` bucketed by *normalized* owner key. The `/cp-prep` plugin command consumes this for its highlights summary instead of parsing markdown.
- **`cp prep-agenda` auto-runs `cp sync` if master-cp's last sync is > 10 minutes old.** Avoids the "agenda shows yesterday's owner data" footgun. Opt out with `--no-sync` for environments where the network round-trip isn't acceptable. Threshold is `SYNC_STALENESS_THRESHOLD_MINUTES = 10` in `cp_engine.agenda`.
- New `normalize_owner()` helper collapses MC-2 owner-name variants ("Drew Fiero", "Drew", "Drew + Tony", "Drew and Tony", "Drew and Marcello" all → `"drew"`) for the workload summary. The rendered agenda preserves the literal MC-2 string for fidelity; only the bucketing key is normalized.
- New `master_cp_last_sync()` and `is_sync_stale()` helpers — read the `last-sync-timestamp` engine region in master-cp.md, compare against the threshold.

### Fixed

- **`cp prep-agenda` no longer warns "Skipping sprint file _agenda.md".** Earlier, the agenda generator's `_load_sprint_files` iterated `sprints/<W##>/*.md` and tried to parse `_agenda.md` itself as a sprint file (harmless warning, but noise). Fix: skip any `_`-prefixed file in the sprint folder, plus explicit blocklist for the three known sentinels (`README.md`, `_week.md`, `_agenda.md`).

### Verification

- 9 new tests in `test_agenda.py` covering: owner normalization (Drew variants → "drew", distinct first names stay distinct, empty/None handling), `master_cp_last_sync` parsing (missing file, real engine region), `is_sync_stale` (no master-cp → True; recent → False; > 1hr old → True; documented threshold matches constant). Full suite: **377 passing**.

## v0.8.8.1 — 2026-05-12

### Fixed

- **`cp prep-agenda` now strips `<!-- cp:hash=... -->` idempotency markers from rendered text.** Bug found in live verification: `/cp-ingest` writes bullets with trailing hash markers for re-run dedup; the v0.8.5 strip aggregators preserve those markers in the bullet text; the agenda renderer was passing them straight through to the rendered surface. Fix: new `_strip_hash_marker()` helper applied to themes, cross-cutting decisions, recent inbound, and open-asks bullets in the agenda. 1 new test in `test_agenda.py`. Total: 368 passing.

## v0.8.8 — 2026-05-12

### Added — Tier 2.4 from W19 retro: meeting-prep agent

**`cp prep-agenda` + `/cp-prep` plugin command.** Closes the loop the other direction from `/cp-ingest`: instead of capturing what happened in a meeting, prepares what should happen in one. Reads cp tenant state and assembles a project-grouped markdown agenda that bridges master-cp.md (too sparse) and per-project sprint files (too heavy). Per the W19 retro's Tier 2.4 motivation: would have prevented "ran out of time before Tony's projects" by surfacing per-owner workload up front.

- **`cp prep-agenda`** — engine verb. Default: full sprint planning agenda for all active projects. With `--projects ggl-5168,ibx-5167`: scoped agenda for the named projects only. With `--out <path>`: write to file (default stdout). Sources: master-cp project list + last-week allocations; weekly-cp.md decisions (parsed by `(YYYY-MM-DD, source: <code>)` regex); current-week sprint files (strip rollups + Horizon → Decisions due); per-project cp.md handwritten Quick Resume sections.
- **`/cp-prep` plugin command** — thin wrapper. Determines current planning week via cp-engine's v0.8.7.3 planning-week rule, generates the agenda to `sprints/<W##>/_agenda.md`, surfaces a brief summary (urgency flags, owner-workload callout) without dumping the full agenda inline.
- **Per-project block content (Option C from the design):** Quick Resume excerpt + recent inbound (last 3 from strip region) + open asks aged > 7d + decisions due this/next sprint (from sprint file Horizon section) + cross-referenced weekly-cp.md decisions matching the project code + stakeholders + discussion prompt (only when urgency signal exists).
- **Tenant-wide header:** themes (from `_week.md` `## Themes` sections, current + prior week) + cross-cutting decisions (from weekly-cp.md decisions-strip aggregator) + carry-forward (escalated risks, stale asks, decisions due — same data as master-cp.md `agenda` region).
- **New module:** `cp_engine.agenda` (~430 lines). Reuses existing `cp_engine.aggregators` (no duplicated logic).

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`. New verb is purely additive — no behavior change to existing commands.
- New optional file: `sprints/<W##>/_agenda.md`. Created only when `/cp-prep` runs. Idempotent (overwrites in place).
- Auto-commit deliberately not implemented — `/cp-prep` is a working artifact for the meeting; whether to commit is per-team preference.

### Verification

- 10 new tests in `test_agenda.py` covering: weekly-decision parsing (single source, multi-source, engine-marker truncation, empty input), `decisions_for_project` filtering (case-insensitive, multi-source matches), `extract_quick_resume` (real content, template-placeholder rejection, mixed content). Full suite: **367 passing**.
- Smoke-tested against the live cp tenant: parsed all 19 weekly-cp.md decisions; cross-references correctly match (#3+#5 → ggl-5136, #6+#7 → ggl-5168).

## v0.8.7.3 — 2026-05-12

### Changed — Tier 2.6 from W19 retro: sprint-week alignment with MC-2

- **`current_sprint_week_iso` now uses MC-2's planning-week rule.** Previously: always returned this calendar week's Monday (so Wed-Sun mid-sprint still labeled "this week"). Now matches MC-2's `planningWeekMonday()` in `frontend/src/components/sprint/WeekPicker.tsx`:
  - **Mon (0) + Tue (1)** → THIS week's Monday (e.g. Tue May 12 → W19)
  - **Wed (2) – Sun (6)** → NEXT week's Monday (e.g. Wed May 13 → W20)
- **Why:** the W19 retro flagged that "MC-2 shows W20, cp shows W19" mid-week. Drew's mental anchor is MC-2 — when partners open the planner mid-week, they're planning the *upcoming* sprint, not reviewing the current one. cp tenant labels now match that intent so `/cp-ingest` writes to the file representing "what we're planning right now."
- New helper `_planning_monday(now)` encapsulates the rule. `_monday_of()` retained as a pure utility (calendar-week Monday, no roll). `prior_sprint_week_iso` and `sprint_week_dates` both use the new anchor.

### Tenant impact

- **Behavior change visible mid-week.** On Wednesday morning, `cp sync` will now scaffold `sprints/<NEXT-week>/`, not refresh the previous folder. Sprint files written before the Wed-roll stay in their original week's folder (no data loss); aggregators read the last 4 weeks of sprint content so cross-sprint projections work normally.
- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`. Live tenant on this machine reinstalled at v0.8.7.3.

### Verification

- 3 new tests in `test_sprints.py` covering the full Mon-Sun cycle for `current_sprint_week_iso`, `prior_sprint_week_iso`, and `sprint_week_dates`. Pre-existing `test_sync.py` cases that hard-coded `datetime(2026, 5, 13)` (Wed) updated to use `datetime(2026, 5, 11)` (Mon) so the semantic intent ("plan this week") is preserved. Full suite: **357 passing**.

## v0.8.7.2 — 2026-05-12

### Changed

- **`cp fathom-fetch` filenames now use readable slugs.** Previously staged transcripts at `transcripts/incoming/<meeting-id>.txt` (UUID, unreadable in `ls`). Now stages as `<YYYY-MM-DD>-<slugified-title>.txt` (e.g. `2026-05-12-1p-weekly-scrum.txt`, `2026-05-12-sap-5174-vision-update-2026.txt`). On collision (common for repeated titles like "Impromptu Zoom Meeting"), appends `-2`, `-3` etc. Slug capped at 60 chars to keep filenames manageable across filesystems.
- Meeting id is preserved inside the file's metadata header — auto-poll's id-based idempotency in `.cp-engine/state.json` continues to work correctly even when filenames vary.

### Added

- 5 new tests covering: collision-suffix behavior across 3 same-title meetings, special-char slugging (`#sap_5174_vision_update` → `sap-5174-vision-update`), 60-char cap on long titles, missing-title fallback to "untitled". Full suite: **355 passing**.

## v0.8.7.1 — 2026-05-12

### Fixed

- **`cp fathom-fetch` now handles Supabase's JSONB transcript shape.** Bug discovered in live verification: the `fathom_meetings.transcript` column is a list of `{text, speaker, timestamp}` utterance dicts (not a flat string as initially assumed). v0.8.7 crashed with `TypeError: can only concatenate str (not "list") to str`. v0.8.7.1 introduces `_render_transcript_body()` which converts the JSONB list into the `MM:SS - Speaker / utterance text` format that `cp parse-transcript` expects. Defensive: still passes through string transcripts (legacy/future schema), returns `(no transcript)` placeholder for None/empty/bad shapes, skips garbage list entries silently. Strips leading `00:` from short timestamps so they match Fathom's typical export style. 4 new tests covering all four input shapes (Supabase JSONB list, string, empty, missing fields).

## v0.8.7 — 2026-05-12

### Added — Tier 1 Phase 1.3 (Fathom bridge)

**Third and final release of the v0.8.5/6/7 cluster from the [Tier 1 design doc](https://github.com/FirstPersonSF/cp/blob/main/docs/plans/2026-05-12-tier-1-design.md). With v0.8.7 shipped, the retro's full vision — "every transcript at every level keeps every artifact top of mind" — is operationally live for both file-sourced and Fathom-sourced transcripts.**

- **`cp fathom-list --since <ts>`** — emits JSON list of meetings from the `fathom_meetings` Supabase table (same project as MC-2; populated by the fathom-meeting-sync webhook). Default 50 rows, newest first. Reuses MC-2 credentials so no new auth surface.
- **`cp fathom-fetch <meeting-id>`** — pulls a single meeting's full transcript + metadata and stages it to `transcripts/incoming/<meeting-id>.txt` at the tenant root. Pass `--needs-review` to stage to `transcripts/needs-review/` instead. Includes a metadata header in the staged file (title, id, meeting_date, project_tags, duration).
- **`cp fathom-auto-poll`** — polls for new meetings since the last poll and stages them via the **confidence gate**: meetings with non-empty + non-`untagged` `project_tags` land in `transcripts/incoming/` for downstream `/cp-ingest`; meetings with no real tags land in `transcripts/needs-review/` for manual handling. Idempotent — `.cp-engine/state.json` tracks `last_polled_at` + `processed_ids`. `--dry-run` reports without writing.
- **`/cp-ingest --fathom <meeting-id>`** plugin mode — when the user passes `--fathom <id>` instead of a file path, the plugin runs `cp fathom-fetch` first to stage the transcript, then proceeds with the existing audit → classify → plan → confirm → execute flow.
- **`fathom-auto-poll.yml` GitHub Actions workflow** at `.github/workflows/` of the cp tenant. `workflow_dispatch:` only by default (cron is commented out as a starting point); flip to a cron schedule once you trust the auto-tagger + the confidence gate. Commits staged transcripts with `[fathom-ingest]` prefix matching the existing `[cp-sync]` convention.
- **`.gitignore` template gains `.cp-engine/`** entry so the per-tenant state file doesn't get committed.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install` or workflow run.
- New gitignored directory: `.cp-engine/` (holds `state.json`). Created on first auto-poll run.
- New gitignored directories (with content tracked when populated): `transcripts/incoming/` and `transcripts/needs-review/`. Created on first fetch.

### Verification

- 15 new tests in `test_fathom.py` covering: `has_good_tags` confidence-gate logic, state-file round-trip (load/save/round-trip; corrupt handling; preserves non-fathom top-level keys), `stage_transcript` (writes to correct dir, metadata header, handles empty transcript, needs-review flag). Full suite: **347 passing**.
- Live verification: see Step 9 of the v0.8.7 commit message. Real Fathom meeting fetched + ingested end-to-end against the cp tenant.

## v0.8.6.1 — 2026-05-12

### Fixed

- **`cp ingest` auto-creates missing sprint-file subsections.** Bug discovered during live verification: sprint files scaffolded before v0.8.5 (e.g. the existing W19 files) don't have `### Stakeholders` inside `## Client communication` — so `cp ingest`'s stakeholder write failed with `subsection '### Stakeholders' not found inside '## Client communication'`. v0.8.6.1 makes `_append_bullet_to_subsection` auto-insert a missing subsection at the end of its parent section before appending the bullet. Same bootstrap behavior as `_ensure_strip_markers` for project cp.md regions.

## v0.8.6 — 2026-05-12

### Added — Tier 1 Phase 1.1 (cp ingest verbs + /cp-ingest plugin)

**Per the [Tier 1 design](https://github.com/FirstPersonSF/cp/blob/main/docs/plans/2026-05-12-tier-1-design.md), this is the second of three releases in the v0.8.5/6/7 cluster. v0.8.5 added the engine-managed regions; v0.8.6 adds the verbs that write into them. v0.8.7 will bridge fathom-meeting-sync.**

- **`cp parse-transcript <path>`** — audits a Fathom-style transcript and emits JSON: speakers, duration, audio gaps (>= 2 min by default, configurable via `--gap-threshold`), action items, and mentioned project codes (via `--codes` flag). **Critical retro fix:** flags the kind of audio gaps that caused the W19 deepening's "Tony absent" misattribution.
- **`cp list-active-projects`** — emits JSON list of active projects from MC-2 for transcript classification: `code, name, company_code, company_name, owner, scope, source`. Filterable by `--scope`. Used by the plugin to give Claude the candidate-projects list during classification.
- **`cp ingest --plan <file>`** — validates a YAML plan against the schema and executes it atomically. Plan format documented in the design doc; supports 7 verbs (`record-inbound`, `record-ask`, `close-ask`, `add-decision`, `record-risk`, `record-stakeholder`, `record-theme`) plus shorthand names (`inbound`, `asks`, `decisions`, etc.). Idempotent — each appended bullet gets a trailing `<!-- cp:hash=<sha8> -->` content-hash comment; re-running the same plan is a no-op (counts as `skipped_duplicate`). `--dry-run` validates and reports without writing.
- **`cp write-region <file> <region>`** — escape-hatch verb that splices arbitrary content into an engine-managed region. Logs a warning so routine use is visible. For cases the structured verbs don't cover.
- **`/cp-ingest <transcript-path>` plugin command** — Claude-orchestrated workflow: audit → classify → plan → confirm → execute → log. Saves the executed plan to `sprints/<W##>/_ingest-log/<timestamp>.yaml` as an audit trail. Does **not** commit/push automatically — user reviews and commits.
- **New `cp_engine.ingest` module** — holds `parse_transcript`, `execute_plan`, and the seven internal write functions. Single source of truth for the verb behavior.
- **New dependency: `pyyaml>=6.0`** — for plan parsing.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`. No tenant config changes.
- The v0.8.5 regions and templates are required precursors — `cp ingest` writes bracket-formatted bullets into the new subsections (`### Stakeholders`, etc.) and the v0.8.5 aggregators project them up into the strip regions on next sync.
- Workflow change: instead of hand-writing 13 sprint files for a sprint planning, you can now run `/cp-ingest sprints/2026-W19/<transcript>.txt`. The protocol-defined trigger phrase `deepen from transcript` is now backed by deterministic engine verbs instead of Claude improvising.

### Verification

- 16 new tests in `test_ingest.py` covering parse_transcript (speakers, gaps, action items, mentioned codes, dedup), plan validation (schema, unknown verbs, shorthand names), and execute_plan (write semantics, idempotency, cross-cutting flag, missing-sprint-file error handling, close-ask flip). Full suite: **332 passing**.

## v0.8.5.1 — 2026-05-12

### Fixed

- **HTML escaping in strip-region output.** `_PROJECT_STRIPS_TEMPLATE` and `_WEEKLY_STRIPS_TEMPLATE` (the ad-hoc Jinja templates used for the new engine-managed regions) were rendered via `env.from_string()`, which doesn't apply the env's filename-based `select_autoescape(disabled_extensions=("md", "j2"))` — so apostrophes and ampersands were rendered as `&#39;` and `&amp;` in cp tenant artifacts. v0.8.5.1 introduces a shared `_render_strip_template()` helper that constructs the Jinja `Template` with `autoescape=False` explicitly. Existing escaped output gets corrected on the next sync that produces a content change (no automatic rewrite of files that haven't changed otherwise).
- **Lenient bracket-bullet parsing.** The W19 hand-deepenings often wrapped brackets in backticks (`` - `[open · 2026-05-08 · Prosperous Health]` text ``). The pre-v0.8.5 `_BRACKET_RE` only matched bare brackets (`- [meta] text`), silently dropping backtick-wrapped entries — exactly the format-fragility risk the retro flagged. v0.8.5.1 updates the regex to tolerate optional backtick wrapping on either side of the bracket. **Side effect:** all bracket-formatted asks/risks/horizon/decisions from the W19 hand-deepenings now parse correctly and feed the new aggregators.

## v0.8.5 — 2026-05-12

### Added — Tier 1 Phase 1.2 (engine-managed regions for transcript ingest)

**Per the [Tier 1 design](https://github.com/FirstPersonSF/cp/blob/main/docs/plans/2026-05-12-tier-1-design.md), this is the first of three releases that together deliver the "every transcript at every level keeps every artifact top of mind" vision from the W19 retro. v0.8.5 adds the engine-managed regions that v0.8.6's `cp ingest` verbs will write to. Backward-compatible additions only — tenants pinned `~= 0.8` pick this up automatically.**

- **Four new engine-managed regions in per-project `cp.md`**: `inbound-strip`, `recent-decisions-strip`, `open-asks-strip`, `stakeholders-strip`. All four aggregate handwritten content from the project's sprint files into the project's durable view. Time windows: last 4 weeks for inbound + decisions; all-time for open asks (until closed) and stakeholders (deduplicated by name, most-recent role/context wins).
- **Three new engine-managed regions in `weekly-cp.md`**: `themes-strip`, `decisions-strip`, `carry-forward-strip`. Themes pulled from the new `sprints/<W##>/_week.md` (last 2 weeks); cross-cutting decisions from sprint files (last 4 weeks); carry-forward from open asks aged > 7 days, escalated risks, and decisions due in next +2 sprints. **`weekly-cp.md` is no longer "untouched by sync"** — it's now mostly handwritten with three engine-managed regions inside.
- **New file: `sprints/<W##>/_week.md`** scaffolded on first sync of each week. Holds week-scope handwritten content (themes, attendance, meta) that doesn't belong to any single project. Like `weekly-cp.md`, created once and never overwritten.
- **Sprint-file template gains `### Stakeholders` subsection** inside `## Client communication`. Old W18/W19 sprint files don't have this subsection — they just render empty stakeholder strips until new sprint files have content.
- **New `cp_engine.aggregators` module** with `aggregate_project_strips`, `aggregate_tenant_strips`, and `carry_forward_rollup`. The last is shared between `master-cp.md`'s existing `agenda` region and the new `weekly-cp.md` `carry-forward-strip` (refactor: `_compute_agenda_rollup` in `render.py` now delegates to the shared function).
- **New state types**: `Stakeholder`, `Theme`, `DecisionEntry` (with `cross_cutting: bool` flag). `SprintFile` extended with `stakeholders` and `decisions` fields.
- **New parsers**: `_parse_stakeholders`, `_parse_decisions` (bracket-formatted, distinct from the legacy numbered-list freeform decisions in `MeetingNotes.decisions`), `parse_themes_from_week_file`.
- **Bootstrap path for existing tenants**: sync injects the new strip markers into existing `weekly-cp.md` and project `cp.md` files (anchored before `## Active research` for weekly; after `tracked-issues` end marker for projects). Idempotent — already-present markers are skipped.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed.
- First sync after upgrading: every active project's `cp.md` gains four new strip regions (initially with placeholder content since no v0.8.6 verbs have written into source yet); `weekly-cp.md` gains three new strip regions; current week's sprint folder gains `_week.md`. Handwritten content in any of these files is preserved.
- Empty regions are normal until v0.8.6 ships — they show "_No X captured yet._" placeholders rather than real aggregated content. Existing W19 sprint files don't yet have bracket-formatted stakeholders/decisions/inbound, so the regions will look mostly empty until new transcripts get deepened.

### Verification

- 13 new tests in `test_sprints.py` (parsers for stakeholders, decisions, themes) and a new `test_aggregators.py` (project + tenant aggregation, dedup, time windows, horizon-target filtering). Full suite: **316 passing**.
- Existing `test_weekly_cp_is_pure_skeleton` updated to reflect the new shape (renamed to `test_weekly_cp_template_has_three_engine_regions`); behavior of master-cp.md `agenda` region preserved across the shared-aggregator refactor.

## v0.8.4 — 2026-05-11

### Added

- **Linked repos in engagement project working dirs.** When an MC-2 `repos` row points at an engagement project (`repos.project_id` is non-null), `cp sync` now writes a `_repo-<repo-name>.md` file into the engagement's working dir for each linked repo. Previously this design promise (called out in `state.py:134` and `sync_mc2.py:12`) had no implementation — the data sat in MC-2 but never surfaced anywhere. **Concrete win:** `cp/1p/ggl-5136-go-safety-website/` now picks up `_repo-ggl-5136-events-calendar.md` on next sync, surfacing Tony's OnFire calendar app with its GitHub URL and any matching `[local-repos.<user>]` clone paths. Closes [#3](https://github.com/FirstPersonSF/cp-engine/issues/3).
- New `LinkedRepo` dataclass on `cp_engine.state` (also re-exported from `cp_engine` and `cp_engine.sync`). New `render_linked_repo_md()` renderer. New `linked-repo.md.j2` template — distinct from `repo.md.j2` so the file's framing makes the *linked* relationship explicit ("Linked source repository" header + named parent engagement) rather than treating it as a primary working-dir record.
- `_engagement_row_to_state` now populates `ProjectState.linked_repos` from the new PostgREST embed (`repos!project_id(...)`). `_parse_linked_repos` filters Inactive repos and skips rows missing org/name.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed.
- Engagement working dirs that have linked repos in MC-2 will gain new `_repo-<name>.md` files. Engagement working dirs without linked repos are unchanged.

### Verification

- 12 new tests in `test_sync_mc2.py` (linked-repo parsing: happy path, Inactive filter, missing org/name, sort order, empty/invalid payload, default status, end-to-end via `_engagement_row_to_state`) and `test_render.py` (linked-repo template: GitHub link + engagement context, local-clone lines, no-clones fallback, no-description fallback). Full suite: 303 passing.

## v0.8.3 — 2026-05-11

### Fixed

- **`cp sync` now auto-loads `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` from `<mc-2 clone>/backend/.env`** when they're absent from the environment. Previously every fresh shell hit `Sync failed: MC-2 backend requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment.` and required `set -a; source .../mc-2/backend/.env; set +a` before each session. The clone path is read from `TenantConfig.local_repos["mc-2"]` (per-machine, gitignored — already required for `cp link-local` and `cp capture-session`), so no new config is needed. Env vars still take precedence, preserving the CI/GitHub Actions path. When the file fallback is used, `cp sync` prints a one-line note to stderr (`Loaded SUPABASE_* from /path/to/mc-2/backend/.env`) so the implicit dependency stays visible. Closes #2.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed, provided `[local-repos]."mc-2"` is set in `.cp-engine.local.toml` (it already is for anyone who's run `cp init` since v0.4).

### Verification

- New tests in `test_sync_mc2.py` cover the four cases: env present (no file read), env absent + .env present (loads from file, prints stderr note), neither source has the keys (raises `BackendUnavailable` with both paths named), and "no MC-2 clone configured" message variant. Full suite passing.

## v0.8.2 — 2026-05-11

### Fixed

- **Completes the v0.8.1 fix at the right layer.** v0.8.1 patched the orchestrator (`_is_active_for_sprint`) but `sync.py` was already pre-filtering the project list with `not is_internal and is_active_status(status)` before passing it down. That stripped FPSF/Canonic repos before the orchestrator could include them. v0.8.2 removes the upstream pre-filter and lets the orchestrator own the rule end-to-end. Verified on real tenant data: the next sync writes `mc-2.md`, `cp-engine.md`, `storyos.md`, etc. alongside the client engagement files.

### Verification

- Updated `test_sync_tenant_writes_sprint_files_for_active_projects` to cover six cases: active engagement (included), holding engagement (excluded), internal engagement (excluded), FPSF repo with status=Active (included), Canonic repo with status=Active (included), inactive repo (excluded). 287 tests passing.

## v0.8.1 — 2026-05-11

### Fixed

- **Sprint files are now generated for FPSF and Canonic projects.** The v0.8.0 orchestrator filtered the active set with `is_active_status` only, which understands MC-2's `Deal`/`Open` vocabulary but not the literal `Active` status used by repo-source projects (FPSF internal tooling, Canonic projects). The result on real tenant data: only client engagements got sprint files; internal projects were silently skipped. Fix mirrors `render.py`'s `is_active` rule — engagement → `is_active_status` + `not is_internal`; repo → `status == "Active"` — so the sprints directory now contains a file for every project the master CP also surfaces.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed.
- The next sync after upgrading will write sprint files for FPSF/Canonic projects that were previously missing. Expected to add ~5–7 new files per tenant on the next sync.

### Verification

- New regression test `test_ensure_sprint_files_includes_repo_source_active_projects` locks in the four cases: engagement (Open) → included; FPSF repo (Active) → included; Canonic repo (Active) → included; inactive repo → excluded. Full suite: 287 passing.

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
