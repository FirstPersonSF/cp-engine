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

## Working tree layout (v0.7+)

```
<tenant root>/
├── master-cp.md                          ← index across everything
├── weekly-cp.md                          ← tenant-wide partners' review surface
│
├── 1p/<code>/cp.md                       ← client engagements
│   ├── ggl-5168-activation/cp.md
│   ├── ibx-5153-ai-campaign/cp.md
│   └── …
│
├── firstpersonsf/                        ← First Person internal: initiatives + standalone repos
│   ├── mission-control/cp.md             ← initiative (slug code, no number)
│   │   ├── _repo-mc-2.md                 ← linked repo surfaces here
│   │   └── _repo-cp-engine.md            ← linked repo surfaces here
│   ├── first-person-website/cp.md        ← initiative (no linked repo yet)
│   ├── first-person-sales/cp.md
│   ├── first-person-operations/cp.md
│   ├── market-scorecard/cp.md
│   ├── 1p-component-library/cp.md        ← standalone repo
│   └── fathom-meeting-sync/cp.md         ← standalone repo
│
├── canonic/                              ← Canonic internal: initiatives + standalone repos
│   ├── storyos/cp.md                     ← initiative (with linked storyos repo)
│   └── unf-forge/cp.md                   ← standalone repo
│
└── sprints/<YYYY-W##>/                   ← per-sprint working files
    ├── <code>.md                          ← one per active engagement OR initiative
    ├── _week.md                           ← tenant-wide weekly cross-references
    └── _ingest-log/                       ← auto-ingest audit log
```

**Initiative-linked repos** surface as `_repo-<name>.md` files under their
initiative's working dir, not as separate top-level dirs. A repo dual-linked to
both an engagement and an initiative appears in both places.

**Inactive items** (status changed, archived, marked internal) move to
`<scope>/inactive/<code>/`. They flip back to live automatically if the row
re-enters sync's view; nothing is destroyed.

Each working dir is a place for *everything* related to that item: the `cp.md`
itself, optional `_dropbox.md` link to media, plus any hand-added text artifacts
(transcripts, syntheses, action items). Binary content lives in Dropbox per
`.gitignore`.

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

## Sprint files

Each active engagement OR initiative has a per-sprint working file at
`sprints/<YYYY-W##>/<code>.md`. The file scaffolds itself on first sync of the
week.

**Engagement sprint files** carry a `## Client communication` section with
subsections: `Outbound`, `Inbound`, `Open asks`, `Slack digest`, `Stakeholders`.

**Initiative sprint files** carry a `## Team communication` section instead,
with `Open asks` and `Slack digest` only (no Outbound/Inbound/Stakeholders since
there's no client side).

Both types share:

- `## Where it stands` (engine-managed)
- `## Carried over from <prior-W##>` (engine-managed)
- `## Dependencies & risks`
- `## This sprint` (Allocation, Deliverables, Definition of done)
- `## Horizon — 4–8 weeks out` (Milestones, Decisions due, Opportunities)
- `## Meeting notes & decisions` (Decisions, Discussion notes)

The auto-ingest verbs auto-detect which section to write into based on which
header the file uses, so the same ingest plan works for both shapes.

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
Loaded: `master-cp.md` + the `cp.md` for one project (at `<scope>/<dir_slug>/cp.md`)
NOT loaded: other projects' `cp.md` files, `weekly-cp.md`
Triggered by: `update <code>`, `check status <code>`, `switch to <code>`, or
opening a session in a tracked project repo (auto-loads its CP).

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

This rule is relative to the *set of currently connected tenant repositories*,
not a single repo. A user with `cp-1p` and `cp-canonic` connected via Claude
Desktop's GitHub MCP has both in scope; Claude reads from whichever the
conversation is about.

## Trigger phrases

| Phrase | Mode | Action |
|---|---|---|
| (no phrase) | 1 | Default. Read `master-cp.md` only. |
| `update <code>` | 2 | Open the project's `cp.md` (at `<scope>/<dir_slug>/cp.md`) for editing. |
| `check status <code>` | 2 | Read the project's `cp.md`; summarize without editing. |
| `update sprint` | 3 | Open `canonic/sprint-cp.md` for editing. |
| `check status sprint` | 3 | Read `canonic/sprint-cp.md`; summarize without editing. |
| `run weekly review` | 4 | Begin live pass of partners'-review workflow. |
| `also load <code>` | additive | Layer that project's `cp.md` onto the current mode. |
| `switch to <code>` | replace → 2 | Discard previous mode; load that project's `cp.md`. |
| `deepen from transcript` | (during weekly review) | Begin deepening pass. |
| `wrap up` | (during weekly review) | Finalize: author each touched project's Exec Summary, word-count checks, master-CP roll-up, commit, push. |
| `rotate the CP` | any | Manually trigger archive rotation on the focused CP. |

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

After the meeting runs, the canonical capture path is to tag the
Fathom recording in the dashboard as `sprint_planning_scope='1p' |
'fpsf' | 'canonic' | 'storyos-mc'`. The auto-ingest webhook handles
per-project routing + a tenant-wide summary in `weekly-cp.md`'s
`## Account summaries`. Don't hand-write per-project bullets when the
auto-ingest path is available.

## Authoring the Exec Summary at wrap up

Each project `cp.md` carries a model-authored `## Exec Summary` region
(between `<!-- cp-engine:start exec-summary -->` and
`<!-- cp-engine:end exec-summary -->`) with six fields — **Objective /
Status / Where it stands / Next up / Blockers / Updates** — plus a
`**Last session:**` line. **You author the six fields; the engine does
not.** The engine only scaffolds the region, migrates the old Quick
Resume into it on `cp sync`, and reads it for `/cp-prep`.

**Exception: the `**Last session:**` line is DERIVED, not authored** —
it's a projection of the newest file under the working dir's `sessions/`
directory, recomputed by `cp capture-session` and re-converged on every
`cp sync`. Don't hand-edit it (the next sync overwrites it), and if a
merge ever conflicts on that line, keep either side and run `cp sync` —
it self-heals to the newest capture. Auto-ingest no longer writes
project `cp.md` state at all — per-meeting truth lands in the sprint file
only, and the Exec Summary is refreshed by you at `wrap up`.

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
6. **Stamp `· updated <today>`** on the `## Exec Summary` heading line.
   `/cp-prep` reads this stamp to flag stale project state going into
   sprint planning — an unstamped or old summary gets a STALE warning in
   the planning bundle.

This is the durable project-state surface; transient weekly material
belongs in the sprint file, not the Exec Summary.

**Also at wrap up — sweep `weekly-cp.md`'s cross-cutting decisions.**
The `## Decisions (cross-cutting, last 4 weeks)` section accretes
auto-ingested entries that nothing expires, and it feeds sprint
planning's "Decisions partners owe each other" list. Once per wrap up:

- Scan the section for entries that are DONE or EXPIRED — the decision
  was made, the scheduled event has passed, the due date embedded in the
  text is behind us. Append `[resolved: <today> — <one-line outcome>]`
  to each (the planner then drops them). When the outcome isn't obvious,
  ask rather than guess.
- Entries with no date at all: add the date if it's recoverable from
  context; otherwise leave them — the planner flags them as undated.
- Never delete entries; the resolved marker IS the archive.

## Deepening from transcript

During `deepen from transcript`, write meeting notes, decisions, new client
asks, outbound drafts, and risk updates into the *sprint file's* hand-written
sections. Engine-managed regions inside the sprint file (`sprint-facts`,
`where-it-stands`, `carry-forward`) MUST NOT be edited — sync owns them.

Project `cp.md` still receives durable updates — the **Exec Summary**
(authored at `wrap up`, above), plus Project Notes and Stakeholders;
transient weekly material belongs in the sprint file, not in `cp.md`.

`wrap up` extends to commit the entire `sprints/<YYYY-W##>/` directory alongside
the master roll-up and to run word-count discipline on each sprint file.

## Proposing journey steps at wrap up

Spine content-writes journal themselves (one review-gated auto-step per
element per day). Hand-propose a step (`propose_spine_step`) only for a
move the auto-step doesn't capture, ≤2 per session. Full discipline:
`/cp-tools`.

## Word-count discipline

Per bootstrap v2:
- >2,500 words on a CP file → duplication audit on next wrap-up
- >3,500 words → archive rotation forced before commit

The engine enforces both checks during `cp render` and `wrap up`.

**Exempt:** per-meeting artifacts under any `meetings/` directory
(`<scope>/<code>/meetings/*.md` and `*.txt`). These carry a meeting
synthesis plus a verbatim transcript and are legitimately long — they
are a fixed-per-meeting record, not an accreting CP file. Do not audit
or rotate them.

## Spine lint at wrap up

Alongside word-count discipline, run `cp spine-lint <code>` once for each
project the session touched. It is WARN-ONLY (never blocks, never
auto-fixes) and flags mechanical hygiene drift: elements flagged important
yet unbound and serving nothing; Agreements whose body says "attach as
source" with no attached source (close the loop with `add_element_source`);
scaffold template placeholders still sitting in `cp.md`. Surface any
findings to the user and fix only what they confirm.

## Spec

Tenant config: `.cp-engine.toml` (committed) + `.cp-engine.local.toml` (gitignored).
Engine version: `0.0.0-golden`.
Canonical spec: see `cp-engine/docs/specs/cp-engine-spec-v02.md`.
