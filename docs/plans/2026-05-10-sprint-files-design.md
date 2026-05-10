---
Project: cp-engine
Provenance: Brainstorm output | 2026-05-10
Filename: 2026-05-10-sprint-files-design.md
Author: Drew + Claude (Opus 4.7)
---

# Sprint files: per-project weekly sprint planning surface

## Why

The master CP today is a strong index across projects, and project CPs hold
the durable per-project state. What's missing is a *per-project, per-sprint*
surface that:

1. Gives the partners' Monday review a structured agenda — one click from the
   master into the project's sprint view.
2. Forces the meeting to address things that today get lost: outbound client
   messages, things we need *from* the client, dependencies and risks, and —
   most importantly — long-horizon planning that gets crowded out by
   this-week thinking.
3. Acts as a scaffold for the meeting itself, so the partners' transcript
   has somewhere structured to land via the existing `deepen from transcript`
   pass.

Today, project `cp.md` carries durable state. The sprint file carries
*time-bound* state for one week.

## Architecture decisions

**Sprint file is canonical for sprint-bound state.** Open client asks,
risks, horizon items, deliverables, and meeting decisions all live in the
weekly sprint file. Project `cp.md` gets a small engine-managed "Current
sprint" block that links to the sprint file and surfaces the top items.

**Sprint files are written, not edited inline.** The view is read-only as a
render target. Partners edit the underlying markdown directly (in their
editor or via Claude in the existing `run weekly review` mode). The mockups
illustrate the rendered state.

**The `deepen from transcript` pass is extended, not replaced.** Existing
mode-4 `run weekly review` already loads master + weekly + every active
project's `cp.md`, runs the deepening pass, and finalizes via `wrap up`.
Sprint files become an additional target surface for that flow — when a
sprint directory exists for the current week, the deepening writes
sprint-bound content there instead of `cp.md`'s hand-written sections.
`cp.md` still receives durable updates (Quick Resume line, Project Notes,
Stakeholders).

**Capacity is mc-2's data, not cp-engine's.** Allocation hours live in the
sprint file (existing `[allocation]` data). Capacity (per-person weekly
hours) is owned by mc-2; when mc-2 eventually renders this view it joins
its own capacity store. cp-engine does not track or display capacity in
v0.8.0.

**mc-2 integration is deferred.** This design is scoped to cp-engine.
A `cp parse-sprint --json` CLI subcommand is included so any future
consumer (mc-2 or otherwise) can read structured sprint data without
re-implementing the parser.

## File layout

```
<cp-tenant>/
├── master-cp.md                     ← gains "Sprint" links per active project
├── weekly-cp.md                     ← unchanged
├── 1p/<code>/cp.md                  ← gains "Current sprint" engine block
├── canonic/<code>/cp.md             ← gains "Current sprint" engine block
├── self/fpsf/<code>/cp.md           ← gains "Current sprint" engine block
└── sprints/
    └── 2026-W19/                    ← one directory per sprint
        ├── README.md                ← engine-generated index
        ├── peb.md                   ← one file per active project
        ├── orb.md
        └── …
```

## Sprint file structure

Per-project file at `sprints/<YYYY-W##>/<project-code>.md`. Sections in
order, top to bottom. **(engine)** = managed via `cp-engine:start/end`
markers; **(hand)** = hand-written, parsed forgivingly.

### 1. Frontmatter and header (engine)

```markdown
---
Project: peb — Pebble Foods
Provenance: Version 0.8.0 | 2026-05-11
Filename: sprints/2026-W19/peb.md
Author: cp-engine (scaffold) + partners (deepening)
Sprint: 2026-W19
PriorSprint: 2026-W18
---

# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)
```

### 2. Sprint facts (engine)

| | |
|---|---|
| Stage | Negotiation |
| Owner | Drew |
| Budget | $45,000 |
| Last touched | 2 days ago |
| Last sprint hours | Drew 6.5h · Tony 2h |
| Sessions this week | 3 |
| Open issues | 3 |
| Prior sprint | [W18](../2026-W18/peb.md) |

### 3. Where it stands (engine)

- **Last stand-up** — date, person, one-paragraph summary from latest
  session.
- **Recent activity** — combined card with sub-eyebrows:
  - Commits (last 7 days)
  - Issues (open tracked issues, mirroring project cp.md)

### 4. Carried over from prior sprint (engine)

Auto-rolled from W-1's parsed file:
- Open client asks (status open)
- Active risks (status active or escalated)
- Horizon items not marked resolved

Each renders with a `kind` tag (ask / risk) and the original raised date.

### 5. Client communication (hand)

Section header carries a contacts subtitle: `Pebble · Maria & Sam`.
Contacts are sourced from project metadata (see "Schema additions" below).

Three subsections:

- **Outbound** — bullets, each with status (`sent` / `draft` / `queued`),
  date, and message description.
- **Open asks** — bullets, each with `Nd open` badge, who it's blocking on,
  date asked, optional why-it-matters note.
- **Inbound** — bullets, each with date, who, what they said.

### 6. Dependencies & risks (hand)

Bulleted list. Each item has:
- Severity tag: `escalated` / `watching` / `dependency`
- Category tag from a fixed enum in tenant config (default:
  `contract`, `pricing`, `people`, `technical`, `scope`, `timeline`)
- Raised date
- Body text + optional "Why it matters" line

### 7. This sprint (hand)

- **Allocation** — pills per person, e.g. `Drew · 6h`, `Tony · 2h`. Total
  computed from sum. (Capacity comparator is mc-2-side only — not in the
  markdown.)
- **Deliverables** — ordered list (numbered). Priority is positional.
  No checkbox state in markdown.
- **Definition of done** — short prose paragraph.

### 8. Horizon (hand) — 4–8 weeks out

Three buckets, each a bulleted list:
- **Milestones** — calendar events with date.
- **Decisions due** — choices with by-date.
- **Opportunities** — early-planning notes, no required date.

The window subtitle "4–8 weeks out" is hard-coded copy in the section
header. Partners self-police what belongs there.

### 9. Meeting notes & decisions (hand, deepened)

Header carries meeting metadata: source ("From sprint planning · May 11"),
attendees ("Drew + Tony"), duration. Two subsections:

- **Decisions** — numbered list, each with date.
- **Discussion notes** — prose paragraph(s), max ~400 words before word-
  count discipline kicks in.

## Project `cp.md` change

Insert a new engine-managed block right after the existing `project-facts`
block, before `tracked-issues`:

```markdown
<!-- cp-engine:start current-sprint -->
## Current sprint — [W19 (May 11 – May 17)](../../sprints/2026-W19/peb.md)

**Allocation:** Drew 6h · Tony 2h
**Open client asks** (2):
- Revised volume forecast from Maria (asked 2026-05-04)
- Legal redline on data-use clause (asked 2026-05-09)

**Active risks** (1):
- Legal turnaround may slip past contract target

_See [sprint file](../../sprints/2026-W19/peb.md) for full plan, horizon,
and meeting notes._
<!-- cp-engine:end current-sprint -->
```

Top 3 asks, top 3 risks. Regenerated every `cp sync` from the parsed
sprint file. If no sprint file exists for the current week (off-week sync
or first-time setup), the block points at the prior sprint with a note.

## Master CP changes

Each active-projects table (Pipeline / 1P Engagements / FPSF / Canonic)
gains a Sprint column linking to that project's current sprint file. The
existing `allocation_line` stays as it is.

A new sprint-index file is generated at `sprints/<YYYY-W##>/README.md`
(engine-managed) listing every project with a sprint file that week, plus
counts of open asks / active risks / decisions due. Mirrors the master CP
shape but scoped to one sprint.

The master CP gains a top-level "Agenda" section above the existing
tables, with three rollup cards:
- **Risks needing decision** — escalated risks across all sprint files
- **Stale client asks** — open asks aged > 7 days
- **Horizon items maturing** — decisions due within 2 sprints

This rollup is engine-generated from the same parsed sprint files.

Section h2s in the master become two-part: an eyebrow ("1P · Pipeline")
and an auto-generated summary sentence ("Three deals in flight"). The
summary is templated per section: `"{N} {noun} {state-phrase}"` from a
small per-section config table.

## Generation, parsing, deepening

Three engine touchpoints drive a sprint file's life.

### A. Scaffold generation (during `cp sync`, sprint-Monday window)

When `cp sync` detects we're inside the existing sprint-planning anchor
window (v0.7.4 logic), it ensures `sprints/<YYYY-W##>/` exists and writes
or updates one file per active project (same active subset the master CP
uses).

For each project:
- Engine sections re-render via `cp-engine:start/end` markers.
- Hand-written sections, if the file already exists, are preserved
  verbatim.
- Carry-forward block is rebuilt from W-1's parsed file: open asks,
  active risks, unresolved horizon items.

Re-running sync mid-week is idempotent.

### B. Parsing

`parse_sprint_file(path) -> SprintFile` reads engine regions from the HTML
markers and parses hand-written sections via simple bullet regexes.
Forgiving: bullets without structured prefixes pass through as plain text
items with no badges.

### C. Deepening (extends `deepen from transcript` in mode 4)

The existing deepening flow gains sprint-file awareness via additions to
`CLAUDE.md.j2`. When `sprints/<current-week>/<code>.md` exists, the
deepening writes sprint-bound output there: meeting notes, decisions,
new client asks, outbound drafts, risk updates. It never touches engine
regions. Project `cp.md`'s hand-written sections still receive durable
updates as today.

`wrap up` extends to:
- Run word-count discipline on each sprint file (same > 2,500 / > 3,500
  thresholds).
- Commit and push the entire `sprints/<YYYY-W##>/` directory in the same
  final push as the master roll-up.

## Schema additions

### Tenant config (`.cp-engine.toml`)

```toml
[risk_categories]
values = ["contract", "pricing", "people", "technical", "scope", "timeline"]
```

### Per-project metadata (`[[projects]]` blocks)

```toml
[[projects]]
code = "peb"
# ... existing fields ...
contacts = [
  { name = "Maria", role = "Ops lead" },
  { name = "Sam", role = "Legal" },
]
```

### Sprint file frontmatter

Adds two fields: `Sprint: 2026-W19`, `PriorSprint: 2026-W18`.

## CLI surface

One new subcommand:

```
cp parse-sprint sprints/2026-W19/peb.md --json
```

Emits a JSON serialization of the parsed `SprintFile` for downstream
consumers (mc-2 integration, future tooling, debugging the parser). No
new flags on `cp sync`; all sprint-file behavior is automatic when in
the sprint window.

## What the master facts strip exposes

The revised master mockup includes a top-level facts strip with eight
fields. All are aggregates over parsed sprint files:

- **Total hours** — sum of all allocation pills across active sprint files
- **Drew / Tony** — per-person sums
- **Active** — count of active sprint files this week
- **Stale asks** — count of open asks aged > 7 days
- **Escalated** — count of risks tagged `escalated`
- **Decisions due** — count of horizon decisions whose by-date is within
  the next 2 sprints (distinct from agenda card 3, which lists items that
  *also* need attention this week)
- **Prior sprint** — link to W-1's master

`Last sync 8:01 AM` shown in the section nav comes from
`master-cp.md`'s existing `last-sync-timestamp` block.

## Mockups

Reference renders for both views:

- `docs/mockups/2026-05-10-sprint-view-peb-w19.html` — original
- `docs/mockups/2026-05-10-sprint-master-w19.html` — original
- `docs/mockups/claude-design-versions/Sprint W19 _ Pebble Foods _V1_.html`
  — iterated direction, taken as the data-contract source of truth above
- `docs/mockups/claude-design-versions/Sprint W19 _ Master.html` — same

## Out of scope (deferred)

- mc-2 integration. Capacity overlay, web-rendered viewer, and any direct
  read path from mc-2 into the cp tenant repo are a separate exploration.
- Inline editing UI. Sprint files remain markdown-edited.
- Per-project sprint-hour budgets / project-level capacity. The `8h
  against 10h capacity` comparator is mc-2's responsibility.
- Cross-tenant aggregation (e.g. cp-firstpersonsf + cp-canonic combined
  views). The v02 spec leaves room for this; it doesn't ship in v0.8.0.

## Versioning

Ships as v0.8.0. Single coherent release: scaffold generation, parsing,
master-CP block, project-CP block, deepening-pass extension, wrap-up
hook, and the JSON CLI export all together.
