---
Project: Canonic Test
Provenance: cp-engine v0.0.0-golden | 2026-05-13
Filename: CLAUDE.md
Author: cp-engine (generated)
---

# Canonic Test — Session Protocol

> **GENERATED FILE.** Do not hand-edit. A `CLAUDE.md` change is a
> `cp-engine` template change. Open an issue against `cp-engine` if you
> need a behavior tweak.

---

# Part 1 — What's in the tree

## Three kinds of work

The tenant tracks three kinds of trackable items:

- **Engagements** — client projects, sourced from MC-2's `projects` table.
  Status vocabulary: `Deal | Open | Holding | Closed | Archived`.
  Active subset: `Deal ∪ Open` with `is_internal=false`.
  Canonical codes: `<company-lowercase>-<number>` (e.g. `ggl-5168`, `ibx-5153`).

- **Initiatives** — internal workstreams (Mission Control, StoryOS, First Person
  Website, Sales, Operations, Market Scorecard), sourced from MC-2's `initiatives`
  table. Parallel to engagements but with no client side.
  Status vocabulary: `Active | On hold | Done | Archived`.
  Active subset: `Active`.
  Canonical codes: slug form (e.g. `mission-control`, `storyos`) — no number.

- **Standalone repos** — code repos in MC-2 not linked to an engagement or
  initiative.
  Status vocabulary: `Active | Holding | Inactive`.
  Canonical codes: the raw repo slug (e.g. `cp-engine`, `mc-2`).

Each kind appears in its own table on `master-cp.md`'s active section. Holding
and Closed-recent collapse to `<details>` blocks; Archived items drop off.

`is_internal` on the engagement table is a defensive filter, not a status —
initiatives are the proper home for internal work and surface as first-class
top-level items.

## How meetings get into the tree

Fathom meetings flow into per-sprint files via four assignment shapes from the
dashboard (`fathom-meeting-sync-production.up.railway.app`). The user picks one in
the meeting card's "+ Assign" dropdown:

| Shape | What it does | Lands in |
|---|---|---|
| **Single project** | Tag a meeting with one engagement or initiative code | That item's `sprints/<W##>/<code>.md` |
| **Account meeting** | Tag a meeting with one company (Google, Infoblox, …) | Per-project bullets across all active items under that company + one `## Account summaries` paragraph in `weekly-cp.md` |
| **Sprint planning** | Tag a meeting with a scope (`1p`, `fpsf`, `canonic`) | Per-project bullets across every active item in scope + one `## Account summaries` paragraph (pseudo-company `1p-clients` / `fpsf-internal` / `canonic-internal`) |
| **Untagged** | Default. No ingest. | (nothing) |

All four flow through the cp-engine-webhook auto-ingest pipeline. Each project
that receives bullets gets its own `[auto-ingest] <code>: meeting <id>` commit;
account/sprint-planning summaries get an additional
`[auto-ingest] account:<co>:` or `[auto-ingest] sprint-planning:<scope>:` commit.

The dashboard also fires a weekly Slack digest cron (Sunday) that fetches each
active project's mapped Slack channel(s) and writes a `### Slack digest` bullet
into the project's current sprint file.

## Tenant-wide surfaces (`weekly-cp.md`)

Cross-cutting content lives in `weekly-cp.md`:

- **Quick Resume** — handwritten "where are we" entries by week.
- **Account summaries** — one paragraph bullet per `(company, week)` for every
  account meeting and sprint planning meeting that ran. Written by the
  auto-ingest webhook; format `[<W##> · <COMPANY>] <paragraph>`.
- **Decisions (cross-cutting, last 4 weeks)** — numbered handwritten list of
  decisions that cross project boundaries. Account-level decisions from
  auto-ingest land here with `source: account: <company>` annotations; the
  `cp_engine.agenda` parser surfaces these to relevant project agendas.
- **Active research** — handwritten pointers to in-flight research workstreams.

Sync never overwrites `weekly-cp.md` (almost entirely human territory plus
auto-appended bullets). Word-count discipline applies.

## Local-link traversal (v0.5+)

Each repo-source working directory (standalone repo dir OR initiative-linked
repo file `_repo-<name>.md`) lists local clone paths via
`**Local clone (<User>):**` lines. The source comes from `.cp-engine.toml`'s
`[local-repos.<user>]` sections.

To answer "what shipped in this repo recently?" or "what's the current state of
`<feature>`?" without network calls, navigate to whichever clone matches the
current user (or any clone for a fresh agent) and read `git log`, recent diffs,
file contents, etc.

A separate per-machine map at `.cp-engine.local.toml` under `[local-repos]`
(gitignored) drives `cp link-local` and `cp capture-session` self-healing.

Conversely, when the working directory is a tracked source repo rather than a cp
working dir, look for `.cp-link` at the repo root. It contains the absolute path
of the corresponding cp working dir — `cd` there to read the project's `cp.md`,
session history, and decisions. The `/cp-summarize` slash command writes a
session summary back to the cp working dir at the end of a development session.

## MC-2 storage: sources + spine (the `cp-sources` MCP server)

**MC-2 (Supabase) is the source of truth; reach it LIVE through the
`cp-sources` MCP server** (`cp mcp` per `.mcp.json`). The on-disk mirrors
(`_sources.md`, `spine/`) lag — prefer the MCP tools; never screenshots.
Every tool takes a `<code>` first (engagement, initiative, or standalone
repo). Five stores: **1 — RAG source store** (ingested Drive/Dropbox
docs); **2 — Spine** (the distilled-memory index: elements, versions,
relations, provenance); **3 — Inbound frameworks** (INTERNAL-only);
**4 — Commitments** (dated obligations); **5 — Notes** (partner pings).

**Full verb catalog: run `/cp-tools`** before any spine-authoring, source,
framework, commitment, or notes work — per-verb signatures and usage
discipline live there, not here.

## Authority precedence (enforced)

When sources conflict: **partner directive** > **project canon** (the
standing Inputs & Briefing element and what's pinned to it) >
**delivered artifacts** > **stakeholder signals** > **working
notes/ephemera**.

**Stakeholder memory advises; it never vetoes.** Execute the partner's
direction and surface the conflict in one line ("Heads up — Janet said X
on 5/27; proceeding as you asked").

---

# Part 2 — How to read and edit it

## Reading modes

This session is in exactly one mode at any moment. Modes can change mid-session
(see "Mode-switching" below).

### Mode 1 — Index-only (default)
Loaded: `master-cp.md`
NOT loaded: `<scope>/<code>/cp.md` files, `weekly-cp.md`, `canonic/`
Triggered by: any session opened in this tenant without a scoping phrase.

### Mode 2 — Single-project
Loaded: `master-cp.md` + the `cp.md` for one project
NOT loaded: other projects' `cp.md` files, `weekly-cp.md`
Triggered by: `update <code>`, `check status <code>`, `switch to <code>`, or
opening a session in a tracked project repo (auto-loads its CP).

**Get the path from `master-cp.md`; never construct it** — engagement dirs
are company-nested and the slug is longer than the code.

`<code>` works the same way for engagements (`ggl-5168`), initiatives
(`mission-control`), and standalone repos (`cp-engine`).

### Mode 3 — Sprint
Loaded: `master-cp.md` + `canonic/sprint-cp.md`
NOT loaded: `<scope>/<code>/cp.md` files, `weekly-cp.md`
Triggered by: `update sprint`, `check status sprint`.

### Mode 4 — Weekly review
Loaded: `master-cp.md` + `weekly-cp.md` + every active project's `cp.md` + `canonic/sprint-cp.md`
NOT loaded: (none — this is the heavyweight mode)
Triggered by: `run weekly review`.

## Mode-switching

**Replace.** These phrases switch the active mode entirely:
- `switch to <code>` → mode 2 with `<code>`
- `switch to sprint` → mode 3
- `run weekly review` → mode 4

**Additive.** These phrases layer context onto the current mode:
- `also load <code>` → adds that project's `cp.md`
- `also load sprint` → adds `canonic/sprint-cp.md`

`run weekly review` mid-session loads on top of the current mode rather than
discarding it. Use `wrap up` to close the weekly-review block; the prior mode
persists if the session continues.

## Gatekeeper rule (enforced)

Do NOT auto-glob, read, or search any path outside the connected tenant
repositories unless the user explicitly references it by path or by repo name.
Project source repos (those configured in `.cp-engine.toml [[projects]]`) are
opt-in: the user must say "also load <code>" or "switch to <code>", or open a
session inside that repo's directory.

The rule spans *all* connected tenant repositories, not one: with `cp-1p`
and `cp-canonic` both connected, read from whichever the conversation is
about.

## Trigger phrases

| Phrase | Mode | Action |
|---|---|---|
| (no phrase) | 1 | Default. Read `master-cp.md` only. |
| `update <code>` | 2 | Open the project's `cp.md` (path via `master-cp.md`) for editing. |
| `check status <code>` | 2 | Read the project's `cp.md`; summarize without editing. |
| `update sprint` | 3 | Open `canonic/sprint-cp.md` for editing. |
| `check status sprint` | 3 | Read `canonic/sprint-cp.md`; summarize without editing. |
| `run weekly review` | 4 | Begin live pass of partners'-review workflow. |
| `also load <code>` | additive | Layer that project's `cp.md` onto the current mode. |
| `switch to <code>` | replace → 2 | Discard previous mode; load that project's `cp.md`. |
| `deepen from transcript` | (during weekly review) | Begin deepening pass. |
| `wrap up` | (during weekly review) | Finalize: author each touched project's Exec Summary, word-count checks, improvements-log sweep, master-CP roll-up, commit, push. |
| `rotate the CP` | any | Manually trigger archive rotation on the focused CP. |
| `sweep improvements` | any | Harvest `improvements.md`: cluster entries, propose issues. |

## Reference style

When announcing an item to a human, use the most readable form for the kind:

- **Engagements** — code + name: "Updates on **ggl-5168 Ggl 5168**?"
- **Initiatives** — name alone (the code IS the slug form of the name):
  "Updates on **Mission Control**?" or "Updates on **Mission Control
  (initiative)**?" if ambiguity matters.
- **Standalone repos** — backticked slug: "what shipped in `mc-2` this week?"

Anchor blocks at the top of every `.md` file follow this format:

```markdown
---
Project: <code+name OR tenant name>
Provenance: Version <NN> | <YYYY-MM-DD>
Filename: <filename>.md
Author: <person | "Claude" | "cp-engine">
---
```

## Hand-written vs. engine-managed

Sections marked with HTML comments like `<!-- cp-engine:start <name> -->` /
`<!-- cp-engine:end <name> -->` are written by cp-engine sync. Outside those
markers is human territory. Sync never touches `weekly-cp.md` outside of
auto-appended bullets in known sections.

## Sprint planning prep

When the user asks to prep an agenda for sprint planning ("run weekly
sprint planning", "prep sprint planning agenda", or similar), run
`/cp-prep`. The engine emits a
**bundle** (`cp prep-planning --bundle`) — every active project's full
Exec Summary plus deterministic metrics — and **you synthesize**
`sprints/<W##>/_planning.md` from it in-session (you author the doc, so
you can defend and revise it live in the meeting). The command itself
carries the full six-section contract and the every-active-project-
appears-exactly-once invariant. Ad-hoc scoped prep (e.g. one client
meeting): `/cp-prep <code> [<code> ...]`.

After the meeting, tag the Fathom recording in the dashboard as
`sprint_planning_scope='1p' | 'fpsf' | 'canonic' | 'storyos-mc'` — the
auto-ingest webhook handles per-project routing + the tenant-wide
summary in `weekly-cp.md`'s `## Account summaries`. Don't hand-write
per-project bullets when auto-ingest is available.

## Authoring the Exec Summary at wrap up

Each project `cp.md` carries a model-authored `## Exec Summary` region
(between `<!-- cp-engine:start exec-summary -->` and
`<!-- cp-engine:end exec-summary -->`) with six fields — **Objective /
Status / Where it stands / Next up / Blockers / Updates** — plus a
`**Last session:**` line. **You author the six fields; the engine does
not.** The engine only scaffolds the region, migrates the old Quick
Resume into it on `cp sync`, and reads it for `/cp-prep`.

**Exception: the `**Last session:**` line is DERIVED, not authored** —
a projection of the newest file under `sessions/`, recomputed by
`cp capture-session` and re-converged on every `cp sync`. Don't
hand-edit it; on a merge conflict keep either side and run `cp sync` —
it self-heals. Auto-ingest never writes project `cp.md` state —
per-meeting truth lands in the sprint file; you refresh the Exec
Summary at `wrap up`.

At `wrap up`, for **each project the session touched**, refresh its Exec
Summary by editing directly between the `exec-summary` markers (Edit
tool). It's a **merge, not a regenerate** — a session that touched one
aspect must not wipe the rest:

1. **Read the prior Exec Summary** — its six fields and the full Updates
   history.
2. **Read this session's changes** — the sprint-file edits, spine
   updates, and any recent meeting ingests for this project.
3. **Rewrite the six fields against current reality**, carrying forward
   everything that's still true and revising only what changed.
4. **Append ONE dated Update** capturing this session's delta:
   `- <today> — <what changed>`.
5. **Roll off Updates older than ~4 weeks** so the history stays tight.
6. **Stamp `· updated <today>`** on the `## Exec Summary` heading line —
   `/cp-prep` flags an unstamped or old summary as STALE in the
   planning bundle.

This is the durable project-state surface; transient weekly material
belongs in the sprint file, not the Exec Summary.

**Also at wrap up — sweep `weekly-cp.md`'s cross-cutting decisions.**
`## Decisions (cross-cutting, last 4 weeks)` accretes auto-ingested
entries that nothing expires, and feeds sprint planning. Once per wrap
up: append `[resolved: <today> — <outcome>]` to entries that are done or
expired (decision made, event passed, date behind us) so the planner
drops them; ask when the outcome isn't obvious. Never delete — the
resolved marker IS the archive.

**Also at wrap up — sweep the touched projects' open commitments.**
`cp commitments-sweep <code>` per touched project: resolve what the
session completed, drop what it made moot, question undated rows ≥2
weeks old — the TTL expires them otherwise.

## Deepening from transcript

During `deepen from transcript`, write meeting notes, decisions, new client
asks, outbound drafts, and risk updates into the *sprint file's* hand-written
sections. Engine-managed regions inside the sprint file (`sprint-facts`,
`where-it-stands`, `carry-forward`) MUST NOT be edited — sync owns them.

Project `cp.md` still receives durable updates — the **Exec Summary**
(authored at `wrap up`, above), plus Project Notes and Stakeholders;
transient weekly material belongs in the sprint file.

`wrap up` extends to commit the entire `sprints/<YYYY-W##>/` directory alongside
the master roll-up and to run word-count discipline on each sprint file.

## Proposing journey steps at wrap up

Spine content-writes journal themselves (one review-gated auto-step per
element per day). Hand-propose a step (`propose_spine_step`) only for a
move the auto-step doesn't capture, ≤2 per session; authoring runs on
`cp-hosted`, so the write carries your identity. Full discipline:
`/cp-tools`.

## Word-count discipline

Per bootstrap v2:
- >2,500 words on a CP file → duplication audit on next wrap-up
- >3,500 words → archive rotation forced before commit

`cp render` warns on both thresholds (warn-only — it never blocks a
commit and never edits). Acting on the warning is yours: the audit and
the rotation are manual.

**Exempt:** per-meeting artifacts under any `meetings/` directory
(fixed per-meeting records — synthesis + verbatim transcript —
legitimately long) and `spine/Retrospective/meeting-history.md`; do not
audit or rotate them.

## Improvements log

When the system fights you — friction, workarounds, unused surfaces —
log a dated bullet in `improvements.md` (tenant root) **at the moment
of friction**: `- <date> · \`area\` — <observation>`. Full protocol
lives in that file's header. Real bugs still go to GitHub issues; never
delete entries. `wrap up` sweeps for unlogged friction;
`sweep improvements` harvests.

## Spine checks at wrap up

Alongside word-count discipline, for each project touched:

`cp spine-lint <code>` — WARN-ONLY: important-yet-unbound elements,
Agreements missing their source (close via `add_element_source` on
`cp-hosted`), scaffold placeholders in `cp.md`. Surface findings; fix
only what the user confirms.

`cp seal-sweep <code>` — for each deliverable that shipped a version,
what fed it plus the `seal_to_deliverable` call. Absorbing a round's
inputs keeps the spine distilled. Read its output carefully:
`/cp-tools`.

## After a cp-engine release: restart `cp mcp`

`cp mcp` outlives a release and keeps serving old bytecode after
`cp sync` upgrades the CLI. If a spine tool ignores a shipped fix,
restart the MCP connection (`/mcp`) before assuming it's broken.

## Spec

Tenant config: `.cp-engine.toml` (committed) + `.cp-engine.local.toml` (gitignored).
Engine version: `0.0.0-golden`.
Canonical spec: see `cp-engine/docs/specs/cp-engine-spec-v02.md`.
