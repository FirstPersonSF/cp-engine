---
Project: Context Protocol Engine
Provenance: Version 01 | 2026-05-05
Filename: cp-engine-spec-v01.md
Author: T.Welch + Claude
---

# Context Protocol Engine — Architecture Spec v01

> A company-wide "memory engine" for First Person and Canonic. Master CP indexes every active client engagement at First Person plus Canonic's current sprint, with per-project / per-sprint CPs underneath. Lives in a new GitHub repo, kept in sync with Mission Control 2 by automation, driven from a Monday meeting workflow. Pattern-inspired by the working multi-level CP at `_claude/1P/ggl-repos/` but operates at a higher altitude (founder-meeting summary level) and is functionally independent from it.

---

## Related Documents

- **Bootstrap (v2):** `/_claude/bootstraps/context-protocol-bootstrap-v02.md` — the operational discipline this spec inherits (word-count thresholds, archive rotation, research-output discipline, three-tier memory model).
- **Spec-writing guide:** `/_claude/personal/tasker/docs/bootstraps/spec-writing-guide.md` — the format this spec follows.
- **Working precursor:** `/_claude/1P/ggl-repos/` — the proof-of-concept that demonstrates the multi-level CP pattern in practice. Its `CLAUDE.md` and `project-cp.md` are the closest existing models.
- **MC-2 source-of-truth surfaces:** `/_claude/1P/mc-2/project-cp.md` — confirms the existing schema this spec depends on (`projects.code`, `projects.mc_status`, `projects.name`, `companies.code`, `full_job_name` trigger).
- **Sprint protocol:** `/_claude/bootstraps/clickup-sprint-protocol-bootstrap-v01.md` — Canonic's existing sprint-tracking mechanism, which this spec sits *above* as a summary layer.

---

## Pre-Flight Questions (Resolved 2026-05-05)

All three resolved in conversation; resolutions are encoded directly in the spec body. Audit trail kept here.

### Q1 — MC-2 status enumeration & "active" subset

**Question:** the sync from MC-2 to `master-cp.md` filters projects by status. We need (a) the enumerated set of statuses MC-2 supports, and (b) which count as "active" for the Monday review.

**Resolution:** MC-2's status vocabulary is being renamed (PR `FirstPersonSF/mc-2#17`) to align with the cp-engine sync's filter:

| Status | Active subset? |
|---|---|
| `Potential` | yes |
| `Open` (renamed from `Active`) | yes |
| `Closed` (renamed from `Complete`) | no |
| `Archived` | no |
| `Internal` | no (system-seeded only — Canonic / 1P-internal projects route through `canonic/sprint-cp.md`, not the master CP) |

**Active subset = `Potential ∪ Open`.** Drives §3 (master CP schema), §5 (sync rules), §7 (Monday workflow scope). The pre-rename "On Hold" subtable proposed in early drafts is removed — there is no `On Hold` status in MC-2.

### Q2 — `ggl-repos` coexistence vs. migration

**Question:** how does the new repo relate to Tony's existing `ggl-repos/` Google CP system?

**Resolution: independent systems.** `ggl-repos/` continues unchanged as Tony's day-to-day Google work surface (deep workstream-level detail, Dropbox symlinks). The new repo's `1P/google/` folder holds founder-meeting-level summary CPs that **do not** point at, link to, or reference `ggl-repos/`. Different altitudes, different audiences. No migration is planned.

### Q3 — Initial seeding scope

**Question:** which clients are populated in the repo at v1 launch?

**Resolution: all six active clients seeded by Monday.** Google, SentinelOne, Infoblox, Hexagon, SalesLoft, Teleflex. v1 launches with full master CP backfill — no incremental onboarding.

(Bucket 1 decisions made silently, documented in Appendix A: branching workflow, project-name display strategy, MC-2 events that fire sync, per-CP template defaults, repo permissions model.)

---

## Builder's Contract

### Hard Constraints (must be followed)

- **MC-2 is the sole writer of project status.** Nobody — human or AI — edits the Status column of `master-cp.md` by hand or via Claude. Status changes go through MC-2's project edit page, which fires the sync. Hand-edits are reverted by the next sync. (See §5.)
- **AI must always reference projects by code + human-readable name.** Spoken or written reference to a project is `GGL-5176 London Safety Video Phase I`, not `GGL-5176` alone. (See §2.4.)
- **CP filenames carry only the code.** `ggl-5176-cp.md`, not `ggl-5176-london-safety-video-cp.md`. The human-readable name lives in the file's H1 title and the master-CP table. (See §2.3.)
- **Zoom transcripts are never committed.** Transcripts pasted into the deepening pass are ephemeral session input only. Only the *extracted CP edits* are committed. (See §7.3.)
- **Word-count discipline per bootstrap v2.** Every CP file is checked on wrap-up: >2,500 words triggers a duplication audit; >3,500 words forces archive rotation before commit. (See §4.5.)
- **File anchors required on every `.md` file.** Per global `CLAUDE.md`. (See §2.5.)
- **Two-pass Monday flow is mandatory, not optional.** Live pass (lightweight tags during the meeting) → deepening pass (post-meeting transcript ingestion) → wrap-up commit. Skipping the deepening pass is forbidden because it's the step that captures the discussion's substance. (See §7.)
- **No status field cross-write.** MC-2 writes status to GitHub; nothing in this system writes status back to MC-2. The repo is a downstream consumer, not a peer. (See §5.4.)

### Builder's Implementation Decisions (guidance, not mandates)

- **GitHub auth model.** GitHub App is recommended over fine-grained PAT (per-repo install scoping, no user-attribution drift, easier secret rotation). Drew's call. (See §5.5.)
- **Diff-presentation UI for the deepening pass.** Recommended: Claude emits a structured diff in chat (per-CP, per-section), human approves before commit. Alternative: Claude commits to a `deepening-WW##` branch and the driver reviews via GitHub's PR diff UI. Driver chooses based on meeting cadence. (See §7.3.)
- **Branching strategy.** Recommended: push directly to `main` with a "one driver at a time" convention during the Monday meeting; branch + PR only for structural repo changes (new client folder, schema change, automation tweak). Four-person team, low-stakes file repo — branch-and-PR for every edit creates more friction than it prevents. (See §8.3.)
- **Master CP active-subset display.** Recommended: a single Active table at the top, a collapsed Closed-recent list (last 30 days) below it. Builder may rearrange if a different layout proves easier to scan during the meeting. (See §3.)
- **Per-CP "Active research" section.** Recommended for any project that's commissioned Perplexity / Claude Research artifacts (per bootstrap v2 §Research Outputs). Optional for projects without active research workstreams. (See §4.4.)

---

## §1 System Overview

### 1.1 The three actors

```
   Humans (4 founders)              MC-2 (Mission Control)         GitHub (context-protocol)
   ────────────────────             ───────────────────────         ───────────────────────────
   Tony, Drew, Marcello, Brandon    Owns: project status, name,     Owns: CP narrative content
                                          owner, full_job_name             (Quick Resume, Decisions,
   Tools:                                                                   Done, Project Notes,
   - Tony, Drew → Claude Code       Source of truth for the                 Active research)
   - Marcello, Brandon →             "what projects exist and
     Claude Desktop (filesystem      where do they stand" question.   Sync: pulled from MC-2 on
     + GitHub MCP)                                                          status/name change
                                                                            (one-way, MC-2 → GitHub)
```

Three principles fall out of this layout:

1. **MC-2 is the project registry.** The set of projects, their codes, names, and statuses is canonical there. The master CP is a *projection* of MC-2's state into a Markdown surface that AI can read efficiently.
2. **The repo is the narrative layer.** What we discussed, what we decided, what's blocked, what's next — that's the stuff humans + AI capture in CP files. MC-2 doesn't try to replicate it.
3. **All four founders read and write the repo.** No tool asymmetry: Marcello and Brandon use Claude Desktop with filesystem + GitHub MCP, which gives them write access on parity with Tony and Drew's Claude Code workflow.

### 1.2 Memory tier mapping

Per the bootstrap v2 three-tier model:

| Tier | What lives here | Update cadence |
|------|-----------------|----------------|
| **Hot** | `CLAUDE.md` at the repo root (routing, trigger phrases, status enum, key conventions). Always loaded. | Edited rarely (architectural changes only). |
| **Warm** | `master-cp.md` + each per-project / per-sprint CP. Loaded per session as needed. | Updated every Monday meeting + ad-hoc during the week. Word-count gated. |
| **Cold** | `archive/` directories. Loaded on demand only. | Created by archive rotation when a CP exceeds 3,500 words. |

### 1.3 The week's rhythm

```
Mon 10am-11am  →  All four meet on Zoom.
                  Driver opens Claude Code in repo.
                  "run weekly review"  → live pass (light tags).
                  Meeting ends.
                  Transcript posted.
                  "deepen from transcript"  → richer captures, diff for review.
                  "wrap up"  → word-count checks, master-CP roll-up, push.

Tue–Sun        →  Anyone can update any project's CP at any time.
                  Mid-week edits don't trigger automation.
                  MC-2 status changes during the week auto-sync to master-cp.md
                  in near-real-time (commit-on-change).

Next Mon       →  Cycle repeats. Diff against last Monday's commit shows
                  what shifted (and what was silent).
```

---

## §2 Repository Structure

### 2.1 Directory tree

```
context-protocol/                          ← new GitHub repo
├── CLAUDE.md                              ← team routing + trigger phrase reference
├── README.md                              ← human-readable overview, onboarding instructions
├── master-cp.md                           ← the index: all projects, statuses, owners, names
├── 1P/
│   ├── google/                            ← companies.code = "ggl"
│   │   ├── ggl-5176-cp.md
│   │   ├── ggl-5168-cp.md
│   │   ├── ggl-5185-cp.md
│   │   ├── ...
│   │   └── archive/
│   ├── sentinelone/                       ← companies.code = "snl" (TBD — match MC-2)
│   ├── infoblox/                          ← companies.code TBD
│   ├── hexagon/                           ← companies.code TBD
│   ├── salesloft/                         ← companies.code TBD
│   └── teleflex/                          ← companies.code TBD
├── canonic/
│   ├── sprint-cp.md                       ← rolling current-sprint summary (one file, archived per sprint)
│   └── archive/
│       ├── sprint-W18-cp.md
│       ├── sprint-W19-cp.md
│       └── ...
└── docs/
    └── specs/
        └── cp-engine-spec-v01.md          ← this file (lands here on first commit)
```

### 2.2 Folder naming

- **Client folders use `companies.code` from MC-2 directly** (lowercase). This guarantees no impedance mismatch between the two systems and lets the sync logic compute the path mechanically as `1P/{companies.code}/{projects.code}-cp.md`.
- **`canonic/` is special-cased** — sprint-based, not client-based. One CP file plus archive.

### 2.3 File naming

| File pattern | Where | Contents |
|---|---|---|
| `<projects.code>-cp.md` | `1P/<client>/` | One CP per active First Person job. Code is lowercase, hyphenated (`ggl-5176`). |
| `sprint-cp.md` | `canonic/` | Rolling Canonic sprint summary. Always this name, always at this path. |
| `sprint-W##-cp.md` | `canonic/archive/` | Archived per-sprint summaries. `W##` matches Canonic's existing sprint identifier convention (per the ClickUp sprint protocol). |
| `<filename>-cp-NNN.md` | any `archive/` | Archive-rotation outputs. Three-digit sequence. |

The CP filename **never includes the human-readable project name**. Names rot (renames happen); codes are stable.

### 2.4 Human-readable name discipline

The name lives in three places where humans need it:

1. The CP file's **H1 title**: `# GGL-5176 London Safety Video Phase I — Project CP`
2. The **master CP's Project column**: every row pairs code + name (`ggl-5176 — London Safety Video Phase I`).
3. The CP file's **anchor block** (`Project: GGL-5176 London Safety Video Phase I`).

When AI talks to a human in this repo, it must always say the name. "Updates on GGL-5176?" is forbidden; "Updates on **GGL-5176 London Safety Video Phase I**?" is required. This is encoded in `CLAUDE.md`.

The name comes from MC-2's `full_job_name` trigger (`companies.code || ' ' || projects.number || ' ' || projects.name`). The sync writes it into the master CP and into each per-project CP's anchor + H1 on creation/rename.

### 2.5 File anchor block

Every `.md` file in the repo starts with the standard anchor block (per Tony's global `CLAUDE.md`):

```markdown
---
Project: <client+code+name OR Canonic OR Context Protocol Engine>
Provenance: Version <NN> | <YYYY-MM-DD>
Filename: <filename>.md
Author: <T.Welch | Drew | Marcello | Brandon | Claude | combinations>
---
```

For per-project CPs, `Project` is the full code+name (e.g., `Project: GGL-5176 London Safety Video Phase I`). For `master-cp.md` and `CLAUDE.md`, `Project` is `Context Protocol Engine`.

---

## §3 Master CP — `master-cp.md`

The master CP is the single document every Monday meeting starts from. It must be scannable in <30 seconds.

### 3.1 Schema

```markdown
---
Project: Context Protocol Engine
Provenance: Version <NN> | <YYYY-MM-DD>
Filename: master-cp.md
Author: T.Welch + Claude (and MC-2 automation)
---

# Master CP — First Person + Canonic

> Index of every active client engagement and Canonic's current sprint state.
> Status column is automation-managed by MC-2; do not hand-edit.
> Last automation sync: <ISO timestamp>

## Quick Resume
**Last Monday review:** <date>
**Active projects:** <count>
**This week's themes:** <one-line meeting takeaway>

## Active — First Person

| Client | Code | Project | Status | Owner | Last touched | CP |
|---|---|---|---|---|---|---|
| Google | ggl-5176 | London Safety Video Phase I | Open | Tony | 2026-05-04 | [→](1P/google/ggl-5176-cp.md) |
| Google | ggl-5168 | Playbooks (Activation) | Open | Tony | 2026-05-03 | [→](1P/google/ggl-5168-cp.md) |
| Google | ggl-5180 | (new opportunity) | Potential | Tony | 2026-05-02 | [→](1P/google/ggl-5180-cp.md) |
| ... | | | | | | |

## Active — Canonic

| Sprint | Theme | Status | CP |
|---|---|---|---|
| W19 | <theme> | In Progress | [→](canonic/sprint-cp.md) |

## Decisions (cross-cutting, last 4 weeks)
1. <decision> (<date>, source: <project code or "Monday review">)
2. ...

## Active research
<pointers to Perplexity / Claude Research artifacts per bootstrap v2 discipline>

## Closed — recent (last 30 days)
- ggl-5XXX — <name> — closed <date>
- ...

## Archive Index
<links to historical master-cp archives, if rotation has fired>
```

### 3.2 What's automation-managed vs human-managed

| Field | Owner | Updated by |
|---|---|---|
| Client column | MC-2 | sync (from `companies` join) |
| Code column | MC-2 | sync (from `projects.code`) |
| Project (name) column | MC-2 | sync (from `projects.name` / `full_job_name`) |
| Status column | **MC-2** | **sync (from `projects.mc_status`) — never hand-edited** |
| Owner column | MC-2 | sync (from `projects.account_manager`) |
| Last touched | sync OR Monday review | sync writes ISO date on any field change; Monday review can override with the meeting date |
| CP link | computed | always `1P/{client}/{code}-cp.md` |
| Quick Resume | humans | every Monday review |
| Decisions | humans | accumulated during Monday reviews |
| Active research | humans | per bootstrap v2 |
| Closed list | sync | sync moves rows here on `Closed` status |

### 3.3 Active subset rule

`Active — First Person` contains only rows where `mc_status ∈ {Potential, Open}` (the active subset per resolved Q1).
`Closed — recent` contains rows where `mc_status = Closed` and `Last touched` is within 30 days.
Rows with `mc_status = Archived` or `Closed` older than 30 days → archived to `archive/master-cp-NNN.md` and removed from the live file.
Rows with `mc_status = Internal` are skipped entirely (Canonic and 1P-internal work route through `canonic/sprint-cp.md`).

---

## §4 Per-Project & Per-Sprint CPs

### 4.1 First Person project-CP template

Stored at `1P/<client>/<code>-cp.md`. Sections in order:

```markdown
---
Project: <CLIENT-CODE>-<NUMBER> <Human Name>
Provenance: Version <NN> | <YYYY-MM-DD>
Filename: <code>-cp.md
Author: <whoever last touched it>
---

# <CLIENT-CODE>-<NUMBER> <Human Name> — Project CP

> One-line description of what this project is and who it's for.

## Quick Resume
**Last session:** <date>
**Current work:** <what's in flight right now>
**Next up:** <next 1–3 concrete actions, dated where possible>
**Blockers:** <or "None">

## Current Work
<2–10 paragraphs of substantive notes on the active workstream.>

## Decisions
1. <decision> (<date>)

## Done
<recent completions, grouped by date or session>

## Active research
<per bootstrap v2 — pointers only, never raw research dumps>

## Project Notes
<durable conventions specific to this project that any AI assistant should know>

## Stakeholders
<key contacts on the client side and internal owners>

## Archive Index
<links to archived content, if any>
```

### 4.2 Canonic sprint-CP template

Stored at `canonic/sprint-cp.md`. One file, rolling — when a sprint closes, its content is archived to `canonic/archive/sprint-W##-cp.md` and the live file resets to the new sprint's frame.

```markdown
---
Project: Canonic — Sprint W##
Provenance: Version <NN> | <YYYY-MM-DD>
Filename: sprint-cp.md
Author: T.Welch + Drew + Marcello + Brandon + Claude
---

# Canonic — Sprint W## CP

> Summary layer over ClickUp sprint W##. ClickUp is canonical for task-level state;
> this CP captures narrative, decisions, blockers, and what the four founders need
> to remember about the sprint that doesn't fit in a ClickUp ticket.

## Sprint Frame
**Sprint:** W##
**Theme:** <one-line>
**Started:** <date>
**Closes:** <date>
**Goal:** <what success looks like>

## In Flight
<what's actively being worked on, by whom>

## Shipped This Sprint
<what landed since sprint start>

## Decisions
1. <decision> (<date>)

## Blockers / Risks
<call-outs that require a founder decision>

## Cross-Project Spillover
<anything from First Person work that affected Canonic this sprint, or vice versa>

## Archive Index
<links to past-sprint archives>
```

### 4.3 Per-CP word-count gating

Every CP follows bootstrap v2's discipline:
- >2,500 words → duplication audit on next wrap-up
- >3,500 words → archive rotation before commit
- archive files land in the same directory's `archive/` subfolder, three-digit sequence

### 4.4 "Active research" section convention

Per bootstrap v2 §Research Outputs. When a project commissions a Perplexity, Claude Research, or competitive-analysis artifact, the artifact lives at `1P/<client>/research/<code>-<topic>-research-vNN.md` (with the prompt at `<code>-<topic>-prompt.md` and a framing header on the response per bootstrap v2). The CP's `Active research` section carries a one-paragraph pointer including the phrase "Do NOT auto-apply any recommendations."

---

## §5 MC-2 → GitHub Sync

### 5.1 Trigger surface

The sync fires on three project-level field changes in MC-2:
- `projects.mc_status` (any value)
- `projects.name` (rename)
- `projects.account_manager` (owner change)

It also fires on:
- New project created in MC-2 → append row to master CP, scaffold a per-project CP if `mc_status ∈ Active subset`
- Project archived in MC-2 → move row to Closed list, leave per-project CP in place (it's history)

### 5.2 Action per trigger

| Trigger | What sync does |
|---|---|
| `mc_status` change | Update Status column in master CP. If the new status moves the row across subtables (Active ↔ Closed-recent ↔ Archived), move it. Update `Last touched` timestamp. |
| `name` change | Update Project column in master CP. Update H1 title and anchor `Project:` field in the per-project CP file. |
| `account_manager` change | Update Owner column in master CP. |
| New project, status ∈ Active subset | Append row to Active table. Create `1P/<client>/<code>-cp.md` from the empty template (anchor + H1 + skeletal sections). |
| New project, status ∉ Active subset | Append row to appropriate subtable. No per-project CP scaffolded. |

### 5.3 Commit format

One commit per sync event. Commit message:

```
[mc-2 sync] <client.code> <projects.code> — <field>: <old> → <new>

Project: <full_job_name>
MC-2 user: <auth.users.email>
Timestamp: <ISO>
```

Example: `[mc-2 sync] ggl ggl-5176 — status: Open → Closed`

The "MC-2 user" line records who made the change in MC-2 — preserves attribution even though the commit author is the GitHub App.

### 5.4 No write-back

The sync is strictly one-way: MC-2 → GitHub. Nothing in this system writes status back to MC-2. If a Monday discussion concludes "let's pause GGL-5176," the action item is *go to MC-2 and change the status there* — not edit the markdown.

If a human accidentally edits the Status column directly, the next sync overwrites it. (No auto-revert on detection — git diff makes it visible if someone wants to investigate.)

### 5.5 Auth & implementation

- **Recommended auth:** GitHub App installed on the `context-protocol` repo. App credentials stored in MC-2's existing Supabase / Railway secret store, alongside Dropbox, GDrive, Slack, ClickUp, Miro tokens. Mirrors the pattern in `mc-2/backend/src/dropbox_client.py`, `gdrive_client.py`, etc.
- **New module:** `backend/src/github_client.py`. Exports a single function `sync_project_change(project_id, change_type, old_value, new_value)`. Called from MC-2's existing project-change webhook / save handler (parallel to the Sheets sync).
- **Failure handling:** transient GitHub failure → MC-2 queues retry (existing retry pattern from Sheets sync). Persistent failure (>3 retries) → surfaces in the project edit page as a "Sync failed" badge with last error.
- **Idempotency:** sync action computes target file state from MC-2 truth; running it twice produces the same commit (or no-op).

### 5.6 Initial backfill

On first deployment of the sync, MC-2 runs a one-time backfill: enumerate every active project, generate the master CP from scratch, scaffold per-project CPs that don't exist yet. Backfill commits as `[mc-2 sync] initial backfill — N projects`.

---

## §6 Trigger Phrases & Session Protocols

These phrases are documented in the repo's `CLAUDE.md`. Any AI session that reads `CLAUDE.md` honors them.

| Phrase | Action |
|---|---|
| `run weekly review` | Begin the live pass of the Monday workflow (§7.1). |
| `deepen from transcript` | Begin the deepening pass with the pasted-in transcript (§7.3). |
| `wrap up` | Finalize: word-count checks across touched CPs, master-CP roll-up, commit, push (§7.4). |
| `check status` | Read master CP + named project's CP if specified; summarize without editing. |
| `update <client> <code>` | Open that project's CP in edit mode for ad-hoc mid-week updates. |
| `rotate the CP` | Manually trigger archive rotation on whichever CP is in focus (per bootstrap v2 §Archive Rotation). |
| `start a new client <name>` | Scaffold `1P/<code>/` directory + first project CP. (Used when MC-2 hasn't yet seeded the client and a Monday discussion needs to capture pre-seed work.) |

### 6.1 On-session-start protocol

Encoded in `CLAUDE.md`:

```markdown
1. Read master-cp.md.
2. If user has specified a project, also read 1P/<client>/<code>-cp.md.
3. If user has specified a sprint, also read canonic/sprint-cp.md.
4. Check the "Last automation sync" timestamp on master-cp.md. If older than 1 hour
   during business hours, mention it (signal that MC-2 sync may have stalled).
5. Use loaded files as primary context — do not ask the user to re-explain anything
   already documented.
```

---

## §7 The Monday Meeting Workflow

### 7.1 Live pass — "run weekly review"

The driver (typically Tony or Drew) opens Claude Code in the repo and types `run weekly review`. Claude:

1. Reads `master-cp.md`. Lists every active project, grouped by client, in a fixed scan order (alphabetical by client, then by code within client).
2. For each project in turn:
   a. Reads `1P/<client>/<code>-cp.md`.
   b. Announces in chat: *"**GGL-5176 London Safety Video Phase I**. Status: Open. Last touched 2026-05-04 (Tony). Quick Resume: [the Quick Resume from the CP, ≤2 sentences]."*
   c. Asks: *"Updates? Decisions? Status thoughts? (type or say)."*
   d. Driver types light tags: `update`, `decision: <text>`, `blocker: <text>`, `next: <text>`, `skip` (no change), or free text. Verbal discussion happens in parallel — Claude doesn't need to capture it now.
3. After all active projects are walked: Claude asks the same for `canonic/sprint-cp.md`.
4. Claude makes minimal CP edits in real time based on the driver's typed tags. No deep prose yet.

The live pass should fit in 30–40 minutes of the 60-minute meeting. The remaining 20–30 minutes is open discussion that the transcript captures.

### 7.2 Verbal anchors

The room uses two natural phrases that help the deepening pass do its job. They are **not robotic ceremony** — just consistent shorthand:

- **Project transitions:** "Next, **GGL-5176 London**…" or "Moving to **GGL-5176**…" — gives Claude an unambiguous segment boundary in the transcript.
- **Decision elevation:** "**Decision:** [statement]" before stating something that should be promoted to the Decisions list.

That's it. No "action item" ceremony, no "blocker:" labels — Claude can infer those.

### 7.3 Deepening pass — "deepen from transcript"

After the meeting ends, the driver downloads the Zoom transcript (or pastes it from Zoom AI Companion, etc.) directly into the same Claude Code session and types `deepen from transcript`. Claude:

1. Acknowledges the transcript is in-session, **explicitly does not commit it to the repo** (per Hard Constraint).
2. Builds a transcript alias map: matches "fifty-one seventy-six" → `ggl-5176`, "five one seven six" → `ggl-5176`, etc. Carries name spellings for all four founders + key client contacts.
3. For each CP touched in the live pass, plus any project mentioned in the transcript that wasn't typed-tagged:
   a. Locates the project's segment in the transcript using the verbal anchors + alias map.
   b. Extracts decisions, blockers, context, color, sub-decisions.
   c. Produces a **structured diff** in chat showing proposed additions/changes to the CP, broken out per section (Quick Resume / Current Work / Decisions / etc.).
4. Driver reviews each diff. Approves, edits, or rejects per section.
5. Approved changes are written into the CP files. Transcript stays in session and is discarded at session end.

**Conflict handling:** if the deepening pass extracts something that contradicts what the live pass captured (e.g., live tag said "blocker: source from Jim" but transcript says "Jim sent the source on Friday"), Claude surfaces the conflict and asks — never silently overwrites.

### 7.4 Wrap-up — "wrap up"

After the deepening pass, the driver types `wrap up`. Claude:

1. Runs `wc -w` on every CP touched this session. Triggers archive rotation per bootstrap v2 if any exceeds 3,500 words. Audits for duplication if any exceeds 2,500 words.
2. Updates `master-cp.md`'s Quick Resume (`Last Monday review`, `This week's themes`).
3. Promotes any cross-cutting decisions surfaced in the deepening pass to the master CP's Decisions section (with source attribution: `(2026-05-05, source: ggl-5176 deepening)`).
4. Commits all changes. Single commit message:
   ```
   [monday review] 2026-W19 — N projects updated

   Live pass + transcript deepening.
   Updated: ggl-5176, ggl-5168, ggl-5185, sprint-cp
   ```
5. Pushes to `main`.

### 7.5 Mid-week updates

Anyone can trigger `update <client> <code>` (or just open the file in their editor / Claude Desktop and type into it) at any time during the week. Mid-week edits do **not** trigger automation, do **not** require a deepening pass, and do **not** require touching the master CP — the sync from MC-2 keeps that surface fresh independently.

The only mid-week touch on `master-cp.md` is when MC-2 fires a sync, which is automated.

---

## §8 Multi-Person Access Model

### 8.1 Tool baseline per person

| Person | Primary tool | File access | GitHub access |
|---|---|---|---|
| Tony | Claude Code (terminal) | Direct filesystem (repo cloned) | git CLI |
| Drew | Claude Code (terminal) | Direct filesystem | git CLI |
| Marcello | Claude Desktop app | Filesystem MCP + Dropbox MCP | GitHub MCP |
| Brandon | Claude Desktop app | Filesystem MCP + Dropbox MCP | GitHub MCP |

All four have functionally equivalent capabilities for this workflow. Brandon and Marcello's onboarding to Claude Desktop with these MCPs is a precondition for v1 launch.

### 8.2 Shared CLAUDE.md, no per-person variants

The repo has one `CLAUDE.md`. It encodes the team's protocol, not any individual's preferences. If any of the four wants AI behavior tuned to their personal style, that goes in their global `~/.claude/CLAUDE.md`, not the repo.

### 8.3 Branching & coordination

- **Push directly to `main`** for all routine CP edits (Monday review commits, mid-week per-project updates, MC-2 sync commits).
- **Branch + PR** for: structural repo changes (new client folder, schema changes to templates, automation tweaks, spec edits).
- **Monday meeting rule:** only one driver edits during the meeting. The other three contribute verbally and don't push concurrently to avoid merge churn.

### 8.4 Conflict resolution

If two people commit conflicting CP edits between syncs, standard `git pull --rebase` workflow. CP files are small enough that conflicts are rare and resolvable in <2 minutes.

---

## §9 Canonic Integration

### 9.1 Two systems, one summary layer

ClickUp stays canonical for sprint task tracking — backlog, planned, in-progress, blocked, review, done, cancelled, per the existing sprint protocol bootstrap. Nothing about that changes.

`canonic/sprint-cp.md` is a *summary* layer that captures what the four founders need to remember about each sprint that doesn't fit in a ClickUp ticket: narrative goals, key decisions, cross-project spillover, founder-level blockers.

### 9.2 Update cadence

- **Monday review** is the primary touchpoint. After First Person projects are walked, Claude does the same for `canonic/sprint-cp.md`: announces sprint frame, reads what's in flight, asks for updates / decisions / blockers.
- **End of sprint** (when ClickUp sprint closes per the sprint protocol's W## rotation): driver runs `rotate the CP` on `canonic/sprint-cp.md`. The rotation moves the closed sprint's content to `canonic/archive/sprint-W##-cp.md` and resets the live file with the new sprint's frame.

### 9.3 No automation between ClickUp and the CP

ClickUp tasks do not auto-write to `sprint-cp.md`. Sprint summaries are produced by humans + Claude during the Monday review. (If a future iteration wants ClickUp → CP automation, design it then; v1 keeps it manual.)

---

## UX & Interaction Design

This is an internal tool with two surfaces: (1) the Monday meeting in Claude Code / Claude Desktop, and (2) MC-2's existing project edit page (where status changes happen). MC-2's UX is unchanged by this spec — adding GitHub sync is invisible to the user except for the new "Sync failed" badge described in §5.5. The interesting UX is the meeting flow.

### Click paths

**Monday review (driver, Tony or Drew):**
1. Open terminal → `cd ~/repos/context-protocol` → `claude` (Claude Code session).
2. Type `run weekly review`. Claude reads master CP, begins per-project walkthrough.
3. For each project: read Claude's announcement → discuss verbally → type light tag (`decision: ...`, `blocker: ...`, `next: ...`, `skip`).
4. After all projects + Canonic sprint: meeting concludes verbal discussion freely.
5. Download Zoom transcript → paste into Claude session → type `deepen from transcript`.
6. Review each diff Claude proposes. Approve / edit / reject per section.
7. Type `wrap up`. Claude commits + pushes. Driver verifies on github.com.

**Mid-week update (any of the four, any tool):**
1. Open Claude Desktop or Claude Code in the repo.
2. Type `update ggl-5176` (or open the file directly).
3. Make edits. AI assists per their preference.
4. Commit + push (manually if file-editing, or via `wrap up` for AI-driven sessions).

**Status change in MC-2 (any of the four):**
1. Open MC-2 web UI → project list → click project.
2. Edit status field → save.
3. Sync fires automatically. Status change reflected in `master-cp.md` within seconds.
4. (If sync fails: project edit page shows "Sync failed" badge with last error.)

### State matrix per CP file

| State | What's visible | When |
|---|---|---|
| **Empty** | Anchor block + H1 + skeletal sections only | New project just created via MC-2 sync |
| **Populated, current** | All sections filled with active content | Normal working state |
| **Populated, stale** | Last session > 14 days ago, status still active | AI flags on read: "this CP hasn't been touched in N days — anything change?" |
| **Word-count warning** | >2,500 words | Wrap-up triggers duplication audit |
| **Word-count critical** | >3,500 words | Wrap-up forces archive rotation before commit |
| **Archive rotated** | Old content moved to `archive/`, live file trimmed, Archive Index updated | After rotation runs |
| **Closed** | Status moved to Closed in master CP, file remains in place as history | After MC-2 status → Closed |

### Diff presentation in deepening pass

Claude emits structured diffs in chat, one per CP, broken out per section. Format:

```
## ggl-5176-cp.md — proposed changes

### Quick Resume
- **Current work:** (changed)
  - was: "Storyboards R2 due Mon 5/4"
  - now: "Storyboards R2 received Mon 5/4 (Marc reviewed Tue 5/4); R3 in flight"

### Decisions
- **+ NEW:** "Voice handled in-house via Tony's ElevenLabs profile, Google-approved." (2026-05-05)

### Blockers
- **- REMOVED:** "Maria adding Tony to London ClickUp project" (resolved per transcript line 247)
```

Driver types `approve all`, `approve quick resume`, `reject decision 1`, etc. — flexibility for whatever level of granularity the meeting needs. v1 supports section-level approve/reject; finer granularity (per-bullet) is v1 stretch.

---

## Code Reconciliation

| Spec Concept | Codebase Status | Action |
|---|---|---|
| MC-2 `projects.mc_status` field | Exists (per `1P/mc-2/project-cp.md` §Schema) | No action — sync reads it as-is |
| MC-2 `projects.code` field | Exists | No action |
| MC-2 `projects.name` field | Exists | No action |
| MC-2 `companies.code` field | Exists | No action |
| MC-2 `full_job_name` Postgres trigger | Exists | No action |
| MC-2 `projects.account_manager` field | Exists (text, not FK) | No action |
| `mc-2/backend/src/github_client.py` | **Does not exist** | New module required (mirrors `dropbox_client.py` / `gdrive_client.py` shape) |
| GitHub App for `context-protocol` repo | **Does not exist** | Needs registration + install + secrets in Railway |
| MC-2 sync trigger on project save | Exists for Google Sheets sync | Extend with parallel github_client call |
| Bootstrap v2 (operational discipline) | Exists at `/_claude/bootstraps/context-protocol-bootstrap-v02.md` | Repo's `CLAUDE.md` references it |
| Spec-writing guide | Exists at `/_claude/personal/tasker/docs/bootstraps/spec-writing-guide.md` | This spec follows it |
| `context-protocol` GitHub repo | **Does not exist** | Created on v1 launch; this spec is the inaugural commit |
| MC-2 status enumeration | Vocabulary `Potential / Open / Closed / Archived / Internal` per `FirstPersonSF/mc-2#17` (centralized in `frontend/src/lib/status.ts`). Active subset = `Potential ∪ Open`. | PR open; merge before cp-engine v1 launch |
| `1P/ggl-repos/` (existing precursor) | Active and in daily use | Independent of this system — no relationship; ggl-repos continues unchanged |

### Gaps / prerequisites

1. Drew implements `github_client.py` and registers the GitHub App. Estimate: small task — pattern matches existing integrations.
2. Marcello + Brandon onboarded to Claude Desktop with filesystem + GitHub MCPs. Estimate: 30 min each.
3. Pre-flight Q1 resolved with the actual MC-2 status enum (Drew confirms).

---

## Phasing

### v1 Must-Have (target: launch before next Monday review where the team is colocated)

- New `context-protocol` GitHub repo created, all four founders added as collaborators
- Repo seeded with `CLAUDE.md`, `master-cp.md`, `README.md`, `docs/specs/cp-engine-spec-v01.md` (this file), per-project CPs for **all active projects across all six active clients** (Google, SentinelOne, Infoblox, Hexagon, SalesLoft, Teleflex), `canonic/sprint-cp.md` for current sprint
- `master-cp.md` populated by MC-2 backfill (all projects in active subset = `Potential ∪ Open`, across all six clients)
- `FirstPersonSF/mc-2#17` (status vocabulary rename) merged before cp-engine v1 launch
- `github_client.py` module + GitHub App installed; sync fires on `mc_status`, `name`, `account_manager` changes
- Trigger phrases (`run weekly review`, `deepen from transcript`, `wrap up`, `check status`) implemented via `CLAUDE.md` instructions
- Marcello + Brandon onboarded to Claude Desktop with required MCPs
- One full Monday review run end-to-end, including transcript deepening

### v1 Stretch

- Per-bullet diff granularity in the deepening pass (instead of section-level)
- Slack notification on MC-2 sync events (so non-driver founders see status changes in real time)
- Auto-detection of "stale CP" (>14 days untouched on an active project) surfaced in master CP
- `start a new client` trigger phrase scaffold
- `rotate the CP` trigger phrase (per bootstrap v2)

### v2+

- ClickUp → `canonic/sprint-cp.md` automation (auto-populate "Shipped This Sprint" from Done tasks)
- Cross-CP search ("which projects mentioned this client contact?") via embeddings
- Read-only web view of the repo for stakeholders who don't use AI tools
- "Check alignment" cross-CP comparison per bootstrap v2

---

## Appendix A: Bucket-1 Decisions (silent)

These were decided per the spec's defaults without surfacing as pre-flight questions. Documented for traceability.

1. **Branching strategy: push-to-main with serial-driver Monday rule.** Branch + PR only for structural changes. Four-person team; PR-per-edit creates more friction than it prevents.
2. **Project name display: denormalized into `master-cp.md` and per-CP H1.** Avoids round-tripping to MC-2 for every read; survives MC-2 outages.
3. **MC-2 sync events: status, name, account_manager changes only (in v1).** Other field changes don't surface in the master CP, so no sync needed.
4. **Per-CP "Active research" section is recommended, not required.** Optional unless the project has commissioned research artifacts.
5. **One shared `CLAUDE.md`, no per-person variants.** Per-person preferences live in each user's global `~/.claude/CLAUDE.md`.
6. **Repo permissions: all four founders are direct collaborators with push access.** No protected `main` branch in v1.
7. **MC-2 commit author: GitHub App.** Original MC-2 user email recorded in commit body for attribution.
8. **Filenames lowercase, hyphenated (`ggl-5176-cp.md`).** Matches MC-2's project code formatting.
9. **Anchor block `Project:` field uses full code+name for per-project CPs**, to keep the human-readable name carried in metadata for any tool that scans frontmatter.

---

## Appendix B: Post-flight Questions

*(Filled during v01 drafting and any subsequent revision. Per spec-writing-guide §Question-Resolution Workflow, all post-flight questions resolve before the spec closes.)*

None surfaced during v01 drafting. (Empty by intent; fill on v02+ if revisions surface new ambiguity.)

---

## Appendix C: Glossary

| Term | Meaning |
|---|---|
| **CP** | Context Protocol — the bootstrap pattern this spec extends. |
| **Master CP** | `master-cp.md` — the index of all projects + sprints. |
| **Per-project CP** | One file per First Person job: `1P/<client>/<code>-cp.md`. |
| **Sprint CP** | `canonic/sprint-cp.md` — the rolling current-sprint summary. |
| **Live pass** | Monday meeting's first phase: driver types light tags during discussion. |
| **Deepening pass** | Monday meeting's second phase: transcript-driven richer extraction with diff review. |
| **Sync** | One-way MC-2 → GitHub propagation of status / name / owner changes. |
| **Active subset** | The set of `mc_status` values that count as "active" — `Potential ∪ Open` per resolved Q1. |
| **Driver** | The founder running the AI session during the Monday meeting (Tony or Drew in v1). |
| **Verbal anchor** | One of two natural phrases ("Next, [code-name]" / "Decision:") that aid transcript parsing. |
