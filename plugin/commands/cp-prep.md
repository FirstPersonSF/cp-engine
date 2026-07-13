---
allowed-tools: Bash(cp:*), Bash(cat:*), Bash(ls:*), Bash(mkdir:*), Bash(date:*), Bash(test:*), Bash(jq:*), Bash(echo:*), Read, Write
description: Prepare a forward-looking sprint planning doc from current cp tenant state.
---

# /cp-prep

Generate a forward-looking, prioritized sprint-planning doc for the
upcoming sprint. The engine emits a **bundle** — every active project's
full Exec Summary plus deterministic metrics (capacity binding, urgent
flags, forward calendar, open commitments) — and **you (the model)
synthesize** that bundle into `_planning.md` in-session, following a
fixed six-section contract: Focus list, decisions & blockers,
cross-cutting patterns, per-owner commitments, forward calendar, and a
roster table covering every non-focus project (every active project
appears exactly once).

The doc is a synthesized plan you author and can defend live ("why is
ggl-5168 on the focus list?"), not a pre-rendered inventory. The engine
supplies the raw material; you do the prioritization.

Replaces the older backward-looking agenda generator. As of cp-engine
v0.15.0 the engine command is `cp prep-planning`. `cp prep-agenda`
still works but is deprecated. The `--bundle` synthesis flow supersedes
the earlier `--out`-renders-the-doc flow.

**Arguments (optional):**
- (no args) → full planning doc for all active projects.
- `<code> [<code> ...]` → scoped doc for the named projects only
  (useful for ad-hoc client-meeting prep, not just sprint planning).

## What you do

### 1. Confirm cp tenant root

```bash
test -f "$(pwd)/.cp-engine.toml"
```

If not, stop and tell the user: "Run /cp-prep from the cp tenant root
(e.g. ~/Documents/Python/cp)."

### 2. Determine the current planning week

`cp prep-planning --summary` emits `week_iso` as JSON — use it as the
authoritative source so this matches MC-2's planning-week rule
(Mon/Tue → this week, Wed-Sun → next week) without any text-parsing.
Capture the whole JSON object once into `$SUMMARY` so later steps can
reuse it without re-shelling out.

```bash
SUMMARY=$(cp prep-planning --summary 2>/dev/null)
WEEK_ISO=$(echo "$SUMMARY" | jq -r .week_iso 2>/dev/null)
# Fall back to today's ISO week if --summary fails.
test -n "$WEEK_ISO" && test "$WEEK_ISO" != "null" || WEEK_ISO=$(date -u +%Y-W%V)
echo "Planning week: $WEEK_ISO"
```

The engine resolves the planning week itself — the skill just reads it
back. `$SUMMARY` (the full JSON object) is also reused in Step 4 below.

### 3. Generate the planning doc (bundle → in-session synthesis)

The engine no longer renders the final doc. It emits a **bundle** — the
structured raw material — and **you synthesize `_planning.md` from it**.

**3a. Get the bundle.**

If invoked with no args:

```bash
mkdir -p "sprints/$WEEK_ISO"
PLANNING_PATH="sprints/$WEEK_ISO/_planning.md"
BUNDLE=$(cp prep-planning --bundle)
```

If invoked with project codes (e.g. `/cp-prep ggl-5168 ibx-5167`):

```bash
mkdir -p "sprints/$WEEK_ISO"
# Sanitize args into a comma-separated string.
CODES=$(echo "$@" | tr ' ' ',')
PLANNING_PATH="sprints/$WEEK_ISO/_planning-${CODES//,/-}.md"
BUNDLE=$(cp prep-planning --bundle --projects "$CODES")
```

The bundle contains, per project: code + name, the **full Exec Summary**
(Objective / Status / Where it stands / Next up / Blockers / Updates)
with a **freshness verdict** on its heading line (parsed from the
`· updated <date>` stamp; >14 days old renders a ⚠ STALE warning),
urgent flags, the forward calendar, open commitments, and **last
week's Slack digest** (the Sunday cron's per-project channel summary,
carried from the prior week's sprint file — cp-engine #78; it is FRESHER
than the Exec Summary by construction, since the digest lands Sunday
night after every wrap-up, so weigh it when the two disagree) — plus
tenant-level metrics (capacity binding, hours, cross-cutting decisions,
each decision aged in days). Milestones come from **MC-2's estimator
schedule** (day-granular milestones + feedback windows maintained in the
Jobs workspace — entries labeled `(MC-2 schedule)`); open commitments
come from **MC-2's `commitments` table** (them→us client-asks +
us→them/internal obligations, with `[proposed]`/`[slipped]` markers on
dates the team hasn't ratified yet), deduped against sprint-file asks by
their shared cp:hash. ClickUp is out of the prep path entirely
(commitments consolidation). The engine still does the deterministic
heavy lifting; it just hands you the material instead of pre-formatting
a doc.

(If you prefer, `cp prep-planning --bundle --out <path>` writes the
bundle to a scratch file you can Read; capturing into `$BUNDLE` and
reading it directly is fine too.)

**3b. Synthesize `_planning.md` and write it.**

Read the bundle — roughly one Exec Summary per active project (~29 of
them tenant-wide) — and synthesize **across** them into a prioritized
plan. Then write it to `$PLANNING_PATH` with the **Write** tool. Do NOT
just transcribe the bundle; the value is in the synthesis.

The doc has a **fixed six-section contract**, in this order. The
sections are the meeting's walk order; don't invent, merge, or drop
sections week to week — the whole point of the contract is that W(N)
and W(N+1) come out the same shape.

1. **Focus list** — the 5–8 projects that need the room this sprint,
   ranked, each with a one-line reason: a decision is due, there's a
   blocker, a deadline is close, or it's slipping. Lead with this —
   it's the agenda. **Respect the freshness verdicts**: a project whose
   Exec Summary is flagged ⚠ STALE must not be planned from its written
   Status/Next-up — either put it on the Focus list with the reason
   "state unverified — confirm verbally" or mark its roster row
   "(state as of <date>, unconfirmed)".
2. **Decisions & blockers needing the partners** — pulled from the Exec
   Summaries' Blockers/Next-up fields and the cross-cutting decisions
   (include the aged ones, with their age in days), **deduped across
   projects** (the same shared blocker shouldn't appear three times).
   Say who's needed for each.
3. **Cross-cutting patterns** — capacity binding (an owner on 5+
   projects), competing deadlines in the same week, blockers shared
   across projects. These are the things only visible when you read all
   the summaries at once.
4. **Per-owner commitments** — what each partner owes going into the
   sprint, rolled up across their projects (us → them and them → us),
   from the bundle's open commitments.
5. **Forward calendar** — the dated, tenant-wide milestone/feedback
   table from the bundle (MC-2 schedule entries).
6. **Roster — everything else** — one table row per active project NOT
   on the Focus list:

   `| Project | Owner | State | Waiting on | Next dated event | Room? |`

   - **State** is YOUR one-line synthesized verdict from the Exec
     Summary — never pasted Quick Resume / Status text. If you can't
     write a defensible one-liner (no Exec Summary, placeholders only),
     the cell is `⚠ no state` — that's itself useful in the meeting.
   - **Room?** is `confirm` (worth a 10-second verbal "still true?") or
     `skip` (parked/waiting, reason visible in State/Waiting-on). This
     makes the roster walkable at speed: ~30 rows ≈ 5 minutes.
   - Staleness/freshness caveats go **in the row** (e.g. a ⚠ on the
     State cell), not in a caveat paragraph.

**The invariant: every active project in the bundle appears exactly
once — as a Focus entry or a roster row.** Before writing the file,
count: Focus entries + roster rows must equal the bundle's project
count. A project missing from both is a rot risk; a project in both is
noise. (This invariant is why the roster exists: the meeting needs a
full roll call so nothing rots silently, at one-line density, while the
Focus list gets the room's actual time.)

Because you author this in-session, you can defend and revise it live in
the meeting ("why is ggl-5168 on the focus list — what's the blocker?").

Note: a sync of an existing tenant may still have an `_agenda.md` from
prior runs of the older `cp prep-agenda` command, or a `_planning.md`
from the pre-`--bundle` engine-rendered flow. `_planning.md` is the
current source of truth; overwrite it.

### 4. Surface highlights via the engine's summary mode

Use the `$SUMMARY` already captured in Step 2 for structured JSON
metrics. If invoked with project codes (Step 3 set `$CODES`), re-capture
the summary scoped to those projects so the metrics line up with the
generated doc.

```bash
test -n "$CODES" && SUMMARY=$(cp prep-planning --summary --projects "$CODES")
echo "$SUMMARY" | jq .
```

The JSON shape:

```json
{
  "week_iso": "2026-W24",
  "week_dates": "Jun 8 – Jun 14",
  "project_count": 30,
  "estimated_minutes": 60,
  "tenant_hours_last_week": {"Drew": 52, "Tony": 52, "Marcello": 42, "Derek": 28},
  "tenant_hours_planned": {"Drew": 40, "Tony": 46},
  "milestone_counts": {"total": 87, "fetched": 84, "errored": 3},
  "urgent_counts": {"slip_risk": 0, "decision_due": 0, "past_due_ask": 0, "escalated_risk": 0},
  "capacity_binding": {
    "basis": "planned_allocations",
    "owners": [
      {"owner": "Tony", "planned_hours": 46, "project_count": 6}
    ]
  },
  "cross_cutting_decisions_count": 3,
  "cross_cutting_decisions_stale_count": 0,
  "cross_cutting_decisions_undated_count": 1,
  "errors": []
}
```

`tenant_hours_planned` is the PLANNING week's allocations (forward
capacity) — `{}` means no allocation rows are entered for the planning
week yet; surface that as its own line ("planned allocations not entered
yet"), it's a planning signal, not an error. `capacity_binding.basis`
says which fact the owners list is built on: `planned_allocations`
(owners with ≥40 planned hours or ≥5 allocated projects that week —
entries carry `planned_hours` + `project_count`) or `owner_of_record`
(fallback when no planning-week allocations exist — entries carry
`count`, a projects-of-record tally, an account-management fact rather
than a capacity fact; say so when rendering it).
`cross_cutting_decisions_stale_count` > 0 means weekly-cp.md's decisions
section holds entries older than the 4-week window (they're filtered
out of the doc, but the section needs a roll-off pass at the next wrap
up — mention it). `..._undated_count` counts entries with no date stamp
(kept, but worth dating at the next pass).

Render to the user:

```
Synthesized planning doc → sprints/$WEEK_ISO/_planning.md
  30 active projects · est. 60 min target
  Tenant hours last sprint: Drew 52, Tony 52, Marcello 42, Derek 28
  Milestones: 84 fetched (3 ClickUp errors)
  Urgent attention items: 4 slip risks · 2 decisions due · 7 past-due asks · 1 escalated risk
  Capacity binding: Tony (6 projects), Marcello (5 projects)
  Cross-cutting decisions partners owe each other: 3 — see weekly-cp.md
```

Conditional rendering rules:

- **Urgent attention items — drop zero counters per-type.** For each of
  the four counters (`slip_risk`, `decision_due`, `past_due_ask`,
  `escalated_risk`), only include it in the bullet if its value is `> 0`.
  Counter labels: `<n> slip risk[s]`, `<n> decision[s] due`,
  `<n> past-due ask[s]`, `<n> escalated risk[s]` — pluralize only when
  `n != 1`. Join the surviving counters with ` · `. If ALL four are zero,
  render `Urgent attention items: none flagged` instead.

  Examples:
  - `slip_risk=3, decision_due=0, past_due_ask=0, escalated_risk=1`
    → `Urgent attention items: 3 slip risks · 1 escalated risk`
  - `slip_risk=0, decision_due=0, past_due_ask=0, escalated_risk=2`
    → `Urgent attention items: 2 escalated risks`
  - `slip_risk=0, decision_due=0, past_due_ask=0, escalated_risk=0`
    → `Urgent attention items: none flagged`

  A jq filter that produces the joined string (empty when all zero):

  ```bash
  echo "$SUMMARY" | jq -r '
    .urgent_counts as $u
    | [
        ($u.slip_risk      | select(. > 0) | "\(.) slip risk\(if . == 1 then "" else "s" end)"),
        ($u.decision_due   | select(. > 0) | "\(.) decision\(if . == 1 then "" else "s" end) due"),
        ($u.past_due_ask   | select(. > 0) | "\(.) past-due ask\(if . == 1 then "" else "s" end)"),
        ($u.escalated_risk | select(. > 0) | "\(.) escalated risk\(if . == 1 then "" else "s" end)")
      ]
    | join(" · ")
  '
  ```

- Capacity binding rendering depends on `capacity_binding.basis`:
  - `planned_allocations` + owners → `Capacity binding: Tony (46h planned,
    6 projects)`.
  - `planned_allocations` + empty owners → `Capacity binding: none flagged
    (no owner ≥ 40h planned or ≥ 5 allocated projects)`.
  - `owner_of_record` (fallback) → `Capacity binding (owner-of-record
    fallback — planning-week allocations not entered): Brandon (10
    projects of record)`; with empty owners render
    `Capacity binding: none flagged (no owner ≥ 5 projects of record)`.
- If `cross_cutting_decisions_count` is 0, render
  `Cross-cutting decisions partners owe each other: none in last 4 weeks`.
- If `milestone_counts.errored` is 0, drop the parenthetical and render
  `Milestones: <fetched> fetched`.

The **capacity-binding callout** is the load-bearing flag — when one
partner owns 5+ projects, sprint planning has to budget time
accordingly (and partners should consider rebalancing). This supersedes
the older `prep-agenda` workload-by-owner bullet.

### 5. Commitments ride in the bundle — no side lookup

Meeting action items, milestones, and client asks are tracked as MC-2
**commitments** (dated, direction-typed, ratification-stated), and the
bundle already carries each project's open commitments in its Open
Commitments table — there is no separate task-system lookup step
anymore. The weekly Slack dates loop (`cp dates-loop`) is the surface
that chases dates between sprint plannings; `/cp-prep` just reads the
current state.

### 6. Don't commit

The planning doc is a working artifact for the meeting. Whether to commit
it is a per-team call (some prefer the audit trail; others find the file
churn distracting). Default: don't auto-commit. Tell the user:

> Planning doc is at `sprints/<W##>/_planning.md`. Commit when you're
> satisfied with it (or leave uncommitted if you treat it as ephemeral).

### 7. Re-running

`/cp-prep` is idempotent — re-running overwrites `_planning.md` in place
with fresh state. Safe to re-run after a `cp sync`, after a `/cp-ingest`,
or anytime new content lands (including after adding/closing milestones
in the MC-2 schedule or commitments in MC-2).

## What good looks like

- The doc is a **synthesized, prioritized plan**, not an inventory. The
  old engine-rendered doc was a ~426-line account-grouped dump of every
  project; the new one is a short plan you built by reading across all
  the Exec Summaries — and can defend live ("why is ggl-5168 on the
  focus list?").
- **The six sections appear in order, every week** — same shape W(N)
  and W(N+1). Novelty goes in the content, not the structure.
- **A real Focus list** leads: 5–8 projects that need the room, each
  with a one-line reason (decision due / blocker / deadline / slipping).
- **The roster is a walkable table, not a paragraph.** Every non-focus
  project gets a row with a synthesized State verdict and a
  confirm/skip call. Collapsing the steady projects into a run-on
  paragraph (the W28 failure mode) reads as "projects are missing";
  a full-entry-per-project dump (the W27 failure mode) reads as hollow.
  One row each is the density the roll call needs.
- **The invariant holds**: Focus entries + roster rows = the bundle's
  active-project count. Every project appears exactly once.
- **Decisions & blockers are deduped** across projects — a shared blocker
  appears once, with who's needed to clear it.
- **Cross-cutting patterns** (capacity binding, competing deadlines,
  shared blockers) surface because you read all the summaries at once —
  the thing no single project block shows.
- **Per-owner commitments** roll up what each partner owes across their
  projects, both directions.
- **Urgency flags only when real**: slip risk, decision due, past-due ask,
  or escalated risk. A quiet project doesn't draw attention.
- **Forward calendar** (MC-2 schedule milestones first, ClickUp tags
  second) grounds the timeline — it's the material behind the deadline
  reasons in the Focus list.

## Failure modes

- **Bare `cp prep-planning` exits non-zero.** Intentional (the deprecated
  engine-rendered inventory used to be the default and kept overwriting
  `_planning.md` with the pre-synthesis dump). The supported flows are
  `--bundle` and `--summary` — this skill already uses them. The old dump
  remains available behind `--legacy-render` if someone explicitly wants
  it.
- **`cp prep-planning` fails with config error.** Run `cp init` if
  `.cp-engine.toml` is missing. Otherwise check the error and resolve.
- **Forward calendar shows `_(no milestones in the MC-2 schedule …)_`.**
  The calendar's sole source is MC-2's estimator schedule (day-granular
  milestones + feedback windows on the Gantt). An empty calendar means
  the project has none — fix it where the work is planned: add milestones
  to the project's Schedule in the MC-2 Jobs workspace (and set the
  project `start_date` — without it the week math has no anchor and
  schedule items can't resolve to dates).
- **Planning doc is mostly empty.** Likely a fresh tenant or a sprint
  with no recent ingest activity. The structure is right; data flows
  in as `/cp-ingest` runs against transcripts.
- **Exec Summary not yet authored.** A project's Exec Summary region in
  its cp.md still has only template placeholders (`_<...>_`) — the model
  hasn't authored it at a wrap up yet, or the region was just migrated
  from the old Quick Resume and seeded from stale content. The bundle
  shows that project as thin. Fix: author its Exec Summary at the next
  `wrap up` (that's the model's job — see the wrap-up authoring
  instructions in the tenant CLAUDE.md), then re-run `/cp-prep`.

## What this command doesn't do

- Doesn't write to project cp.md files. It reads their Exec Summaries
  (via the bundle); it doesn't author them. The Exec Summary is authored
  at `wrap up`, not here.
- The engine doesn't render `_planning.md` — **you** do (from the
  bundle, with the Write tool). The engine only emits the bundle and the
  deterministic metrics.
- Doesn't write to MC-2 commitments — pure read. (Nothing in the prep
  path touches ClickUp at all anymore.)
- Doesn't ingest transcripts (that's `/cp-ingest`).
- Doesn't update master-cp.md or weekly-cp.md (those have their own
  paths via `cp sync` and `/cp-ingest` respectively).
- Doesn't auto-commit.
