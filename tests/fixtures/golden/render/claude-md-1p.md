---
Project: 1P Test
Provenance: cp-engine v0.0.0-golden | 2026-05-13
Filename: CLAUDE.md
Author: cp-engine (generated)
---

# 1P Test — Session Protocol

> **GENERATED FILE.** Do not hand-edit. A `CLAUDE.md` change is a
> `cp-engine` template change. Open an issue against `cp-engine` if you
> need a behavior tweak.

---

# Part 1 — What's in the tree

## One kind of work: the workstream

Everything the tenant tracks is a **workstream** — one MC-2 `projects` row
under a **company**. Companies are the top level (Google, Infoblox, SAP,
Salesloft, Teleflex; First Person and Canonic are the self-companies).
Workstreams nest: `parent_code` puts one under another, to any depth.
A workstream may carry an **agreement** — the commercial envelope (budget,
deal stage) — or not. Status vocabulary is one set for all of them:
`Deal | Open | Holding | Closed | Archived`; active = `Deal ∪ Open`.

The word you see beside a workstream is a **derived label**, first match wins:

| Label | Rule | Example |
|---|---|---|
| **account** | no parent, under a client company | `ggl-5216-google` — Google's root node, owns `1p/google/` |
| **program** | has children | `ggl-5300-go-safety` — a grouping with jobs beneath it |
| **job** | has an agreement | `ggl-5168-activation` — a client engagement |
| **initiative** | none of the above | `1pi-9005-mission-control` — internal work, no envelope |

Codes are `<co>-<number>-<slug>` for every workstream (`ggl-5168-activation`,
`1pi-9005-mission-control`); the short form `ggl-5168` resolves too. Every
client company has exactly one account node; self-companies have none —
their top-level workstreams are programs or initiatives.

Each workstream has the same five surfaces: **context** (`cp.md` — Exec
Summary, decisions, notes), **meetings** (`meetings/`,
sprint-file bullets), **players** (Stakeholders cards in MC-2; `cp.md`
strip), **work** (the sprint file `sprints/<W##>/<code>.md`), and **scope**
(agreement, envelope, deliverables). A parent's `cp.md` also lists its
children and, with an agreement, its envelope; a parent's sprint file rolls
its subtree up. `master-cp.md` renders the whole tree company-grouped, the
account node first and children indented; Holding and Closed-recent
collapse to `<details>` blocks; Archived items drop off.

Code repos attach to the workstream they serve as `_repo-<name>.md` files
inside that workstream's directory — a repo is never a workstream.

## How meetings get into the tree

Fathom meetings flow into per-sprint files via four assignment shapes from the
dashboard (`fathom-meeting-sync-production.up.railway.app`). The user picks one in
the meeting card's "+ Assign" dropdown:

| Shape | What it does | Lands in |
|---|---|---|
| **Single workstream** | Tag a meeting with one workstream code | That workstream's `sprints/<W##>/<code>.md` |
| **Parent workstream** | Tag a meeting with any node that has children (an account, a program) | Per-child bullets across every active workstream below it + one `## Account summary` paragraph on the node's own sprint file; account-level decisions on the node's `cp.md` `## Decisions` |
| **Sprint planning** | Tag a meeting with a scope (`1p`, `fpsf`, `canonic`, `storyos-mc`) | Per-child bullets across every active workstream in scope + one `## Sprint planning summaries` paragraph in `sprints/<W##>/_week.md`; tenant-wide decisions in `master-cp.md`'s hand-written Decisions |
| **Untagged** | Default. No ingest. | (nothing) |

All four flow through the cp-engine-webhook auto-ingest pipeline. Each workstream
that receives bullets gets its own `[auto-ingest] <code>: meeting <id>` commit;
parent/sprint-planning summaries get an additional
`[auto-ingest] account:<code>:` or `[auto-ingest] sprint-planning:<scope>:` commit.

Speaker labels are not proof of who spoke: a bullet naming someone the
transcript never heard carries `[attribution unverified]`. Mis-heard names:
`[names] aliases` in `.cp-engine.toml`.

The dashboard also fires a weekly Slack digest cron (Sunday) that fetches each
active workstream's mapped Slack channel(s) and writes a `### Slack digest` bullet
into the workstream's current sprint file.

## Where cross-cutting content lives

There is no tenant-wide meeting file. Content that crosses one workstream
lives on the parent that owns it:

- **Decisions** — the hand-written `## Decisions` of the account or program
  `cp.md` they belong to (auto-ingest writes them with a
  `source: account: <code>` annotation; `promote uphill` copies one up a
  level). Decisions that belong to no company live in `master-cp.md`'s
  hand-written `## Decisions (cross-cutting, hand-written)`. Agendas and
  sprint prep read a workstream's ancestors plus `master-cp.md`.
- **Account summaries** — `## Account summary` on the parent node's sprint
  file, one paragraph per meeting.
- **Sprint-planning summaries** — `sprints/<W##>/_week.md`, `## Sprint
  planning summaries`, `[<W##> · <SCOPE>]` prefix.
- **Active research / Future capability ideas** — Mission Control's `cp.md`
  Project Notes.

Sync never touches hand-written sections. Word-count discipline applies.

## Local-link traversal (v0.5+)

Each linked-repo file (`_repo-<name>.md`, under the workstream the repo
attaches to) lists local clone paths via `**Local clone (<User>):**` lines.
The source comes from `.cp-engine.toml`'s `[local-repos.<user>]` sections.

To answer "what shipped in this repo recently?" or "what's the current state of
`<feature>`?" without network calls, navigate to whichever clone matches the
current user (or any clone for a fresh agent) and read `git log`, recent diffs,
file contents, etc.

A separate per-machine map at `.cp-engine.local.toml` under `[local-repos]`
(gitignored) drives `cxp link-local` and `cxp capture-session` self-healing.

Conversely, when the working directory is a tracked source repo rather than a cp
working dir, look for `.cp-link` at the repo root. It contains the absolute path
of the corresponding cp working dir — `cd` there to read the project's `cp.md`,
session history, and decisions.

## MC-2 storage: sources + spine (the `cp-sources` MCP server)

**MC-2 (Supabase) is the source of truth; reach it LIVE through the
`cp-sources` MCP server** (`cxp mcp` per `.mcp.json`). The on-disk mirrors
(`_sources.md`, `spine/`) lag — prefer the MCP tools; never screenshots.
Every tool takes a `<code>` first (any workstream code). Five stores: **1 — RAG source store** (ingested Drive/Dropbox
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
NOT loaded: any workstream's `cp.md`
Triggered by: any session opened in this tenant without a scoping phrase.

### Mode 2 — Single-project
Loaded: `master-cp.md` + the `cp.md` for one workstream
NOT loaded: other workstreams' `cp.md` files
Triggered by: `update <code>`, `check status <code>`, `switch to <code>`, or
opening a session in a tracked project repo (auto-loads its CP).

**Get the path from `master-cp.md` (or `.cp-engine/paths.json`); never
construct it** — dirs nest under the company and the parent, to any depth.

`<code>` is any workstream code: a job (`ggl-5168`), an account
(`ggl-5216-google`), a program, an initiative (`1pi-9005-mission-control`).

### Mode 3 — Weekly review
Loaded: `master-cp.md` + every **account** and **program** `cp.md` (the
files that carry the cross-cutting decisions)
NOT loaded: job and initiative `cp.md` files — `also load <code>` opens one
Triggered by: `run weekly review`.

## Mode-switching

**Replace.** These phrases switch the active mode entirely:
- `switch to <code>` → mode 2 with `<code>`
- `run weekly review` → mode 3

**Additive.** These phrases layer context onto the current mode:
- `also load <code>` → adds that project's `cp.md`

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
| `run weekly review` | 3 | Begin live pass of partners'-review workflow. |
| `also load <code>` | additive | Layer that project's `cp.md` onto the current mode. |
| `switch to <code>` | replace → 2 | Discard previous mode; load that project's `cp.md`. |
| `deepen from transcript` | (during weekly review) | Begin deepening pass. |
| `wrap up` | any | Close out the session — **run `/cp-wrapup`**, which carries the full ritual (Exec Summaries, decision + commitment sweeps, spine and word-count checks, commit, push). Also closes a `run weekly review` block. **Without the plugin** (a hosted-only session), see "Wrapping up from a hosted session" below. |
| `rotate the CP` | any | Manually trigger archive rotation on the focused CP. |
| `sweep improvements` | any | Harvest `improvements.md`: cluster entries, propose issues. |
| `promote uphill <code>` | any | Copy a decision or commitment from `<code>` to its PARENT — the `promote_uphill` verb (`cxp promote-uphill`). One level per call. |

## Every capture names its level

A write lands on the workstream you NAME — `capture_project_state`,
`capture_session`, `create_commitment`, `propose_spine_step`, the ingest
plan. Default to the deepest workstream in focus (mode 2's loaded
project). To record something at the account or program level, name THAT
code, or record it where it happened and `promote uphill`. Nothing in the
engine reads a decision's text and decides it "sounds account-level".

## Reference style

When announcing an item to a human, the form follows the label:

- **account, program, initiative** — name alone (the code is the slug form
  of the name): "Updates on **Google**?", "Updates on **Mission Control**?"
  Add the label in parentheses only when it disambiguates:
  "**Go Safety (program)**".
- **job** — short code + short name: "Updates on **ggl-5168 Activation**?"
  (the code carries the company and number, so the name drops them).
- **repos** — backticked slug: "what shipped in `mc-2` this week?"

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
markers is human territory — auto-ingest appends bullets to known
hand-written sections (`## Decisions`, `## Account summary`, `## Sprint
planning summaries`) and never inside a marker.

`spine/` is generated from MC-2: never edit it — a hand edit is
quarantined (`exceptions/region-edits/`), then overwritten. A workstream's
`meeting-history.md` is hand-owned.

Deliberations land in the sprint file's `### Open questions`, beside
`### Decisions`.

## Sprint planning prep

When the user asks to prep an agenda for sprint planning ("run weekly
sprint planning", "prep sprint planning agenda", or similar), run
`/cp-prep`. The engine emits a
**bundle** (`cxp prep-planning --bundle`) — every active project's full
Exec Summary plus deterministic metrics — and **you synthesize**
`sprints/<W##>/_planning.md` from it in-session. The command itself
carries the full six-section contract and the every-active-project-
appears-exactly-once invariant. Ad-hoc scoped prep (e.g. one client
meeting): `/cp-prep <code> [<code> ...]`.

After the meeting, tag the Fathom recording in the dashboard as
`sprint_planning_scope='1p' | 'fpsf' | 'canonic' | 'storyos-mc'` — the
auto-ingest webhook handles per-project routing + the tenant-wide
summary in `sprints/<W##>/_week.md`'s `## Sprint planning summaries`
(unsettled decisions: its `## Open questions`).
Don't hand-write per-project bullets when auto-ingest is available.

## Deepening from transcript

During `deepen from transcript`, write meeting notes, decisions, new client
asks, outbound drafts, and risk updates into the *sprint file's* hand-written
sections. Engine-managed regions inside the sprint file (`sprint-facts`,
`where-it-stands`, `carry-forward`, `open-asks`) are sync's: never edit them.
Asks live in MC-2: add below the `open-asks` markers, close in MC-2.
Others close in their owning week: horizon `[done · …]`,
an open question with `[answered · …]`, either `~~strikethrough~~`.

Project `cp.md` still receives durable updates — the **Exec Summary**
(authored at `wrap up`; run `/cp-wrapup`), plus Project Notes (people:
spine cards); transient weekly material belongs in the sprint file.

## Improvements log

When the system fights you — friction, workarounds, unused surfaces —
log a dated bullet in `improvements.md` (tenant root) **at the moment
of friction**: `- <date> · \`area\` — <observation>`. Full protocol
lives in that file's header. Real bugs still go to GitHub issues; never
delete entries. `wrap up` sweeps for unlogged friction;
`sweep improvements` harvests.

## Wrapping up from a hosted session

`/cp-wrapup` is a Claude Code **plugin** skill. It edits `cp.md` directly and
shells out to `cxp`, so a session working only through the `cp-hosted` MCP
server cannot run it — and the trigger table above would otherwise send you to
a command that does not exist.

The ritual is still available; it is a sequence of verbs rather than one skill:

1. **`capture_project_state`** — the Exec Summary. **Pass every field you mean
   to be current**, not just `status`: omitted fields keep their text, but the
   `· updated` stamp still advances — a stale summary made to look fresh. On
   a summary stamped 14+ days ago a partial call is refused; pass the omitted
   fields, or list those you read and found true in `still_current`.
2. **`spine_lint`** — important-yet-unbound elements, dead-end activities,
   stale canon, archived-but-referenced documents, Exec Summary field budgets.
3. **`commitments_sweep`** — what is owed, both directions. An UNDATED
   commitment expires at 14 days; this is where that gets noticed in time.
4. **`seal_sweep`** — for each shipped deliverable, what fed it. Read it before
   acting; `seal_to_deliverable` is how you act on it.
5. **`word_count_check`** — reporting only. **`rotate_word_count`** acts on
   it: Updates older than 28 days move to the archive file, in one commit.
6. **`capture_session`** — the session record, so the next reader knows this
   happened.

**What a hosted session cannot do:** commit and push the tree, or rotate
hand-written sections. The server holds no write access by construction —
every write it makes is performed upstream under your own identity rather
than minted by the server. Steps 1 and 5 go through that path and commit
for you; the rest is read-only.


## Restart `cxp mcp` after a release or credential change

MCP servers keep what they loaded at startup: old bytecode after `cxp sync`
upgrades the CLI, old credentials after a rotation or `.env`/1Password
change. If a tool ignores a shipped fix or 401s after re-authenticating,
restart the connection (`/mcp`) before assuming it's broken.

## Spec

Tenant config: `.cp-engine.toml` (committed) + `.cp-engine.local.toml` (gitignored).
Engine version: `0.0.0-golden`.
Canonical spec: see `cp-engine/docs/specs/cp-engine-spec-v02.md`.
