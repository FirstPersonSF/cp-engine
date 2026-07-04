---
allowed-tools: Bash(cp:*), Bash(cat:*), Bash(ls:*), Bash(mkdir:*), Bash(date:*), Bash(test:*), Bash(jq:*), Bash(echo:*), Read, Write
description: Prepare a forward-looking sprint planning doc from current cp tenant state.
---

# /cp-prep

Generate a forward-looking, prioritized sprint-planning doc for the
upcoming sprint. The engine emits a **bundle** — every active project's
full Exec Summary plus deterministic metrics (capacity binding, urgent
flags, forward calendar, open commitments) — and **you (the model)
synthesize** that bundle into `_planning.md` in-session: a Focus list of
the projects that need the room, the decisions & blockers the partners
must resolve, cross-cutting patterns, and per-owner commitments.

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
(Objective / Status / Where it stands / Next up / Blockers / Updates),
urgent flags, the forward calendar (dated ClickUp milestones), and open
commitments — plus tenant-level metrics (capacity binding, hours,
cross-cutting decisions). The engine still does the deterministic heavy
lifting (ClickUp milestone fetch, urgent-flag detection, capacity-binding
analysis, cross-cutting decisions parse from weekly-cp.md); it just hands
you the material instead of pre-formatting a doc.

(If you prefer, `cp prep-planning --bundle --out <path>` writes the
bundle to a scratch file you can Read; capturing into `$BUNDLE` and
reading it directly is fine too.)

**3b. Synthesize `_planning.md` and write it.**

Read the bundle — roughly one Exec Summary per active project (~29 of
them tenant-wide) — and synthesize **across** them into a prioritized
plan. Then write it to `$PLANNING_PATH` with the **Write** tool. Do NOT
just transcribe the bundle; the value is in the synthesis. The doc
should contain:

- **Focus list** — the 5–8 projects that need the room this sprint,
  each with a one-line reason: a decision is due, there's a blocker,
  a deadline is close, or it's slipping. Everything else is "steady";
  name it briefly but don't spend meeting time on it. Lead with this —
  it's the agenda.
- **Decisions & blockers needing the partners** — pulled from the Exec
  Summaries' Blockers/Next-up fields and the cross-cutting decisions,
  **deduped across projects** (the same shared blocker shouldn't appear
  three times). Say who's needed for each.
- **Cross-cutting patterns** — capacity binding (an owner on 5+
  projects), competing deadlines in the same week, blockers shared
  across projects. These are the things only visible when you read all
  the summaries at once.
- **Per-owner commitments** — what each partner owes going into the
  sprint, rolled up across their projects (us → them and them → us).

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

### 5. Surface open ClickUp tasks per project (read-only)

Meeting action items are tracked as ClickUp tasks (see the cp ClickUp
tasks pipeline). For sprint-planning prep, surface each active project's
open ClickUp tasks alongside the planning doc so the partners see
committed follow-ups, not just what's in the cp sprint files.

Note: `_planning.md` already includes milestones in each project's
forward calendar (from ClickUp tasks tagged `milestone`), so this step
should filter those out — surface only non-milestone open tasks.

This is **read-only** — never create, complete, or modify ClickUp tasks
from `/cp-prep`.

For each project in scope that has a `clickup_list_id` in MC-2's
`public.projects`:

1. Query MC-2 for the project's `clickup_list_id` (skip projects where
   it is null — they have no ClickUp list yet).
2. Call the ClickUp MCP `clickup_filter_tasks` with
   `list_ids: ["<id>"]` and `include_closed: false` to fetch open
   tasks for the list. The tool has **no native tag-exclude
   parameter** (its `tags` argument is a positive include with OR
   logic across multiple tags), so **filter milestones client-side**:
   after the call returns, drop any task whose `tags` array contains
   an entry named `"milestone"` before surfacing it. Milestones are
   already in the forward calendar of `_planning.md` and would
   double-count. Action-item-tagged and client-ask-tagged tasks pass
   through unchanged.
3. Surface a short per-project block to the user: task name + assignee +
   status. Flag `from-fathom`-tagged tasks that are still unassigned.

If the ClickUp MCP is not available in the session, skip this step and
note it — the rest of the planning doc still stands. This step never
blocks doc generation.

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
in ClickUp).

## What good looks like

- The doc is a **synthesized, prioritized plan**, not an inventory. The
  old engine-rendered doc was a ~426-line account-grouped dump of every
  project; the new one is a short plan you built by reading across all
  the Exec Summaries — and can defend live ("why is ggl-5168 on the
  focus list?").
- **A real Focus list** leads: 5–8 projects that need the room, each
  with a one-line reason (decision due / blocker / deadline / slipping).
  Steady projects are named but don't consume meeting time.
- **Decisions & blockers are deduped** across projects — a shared blocker
  appears once, with who's needed to clear it.
- **Cross-cutting patterns** (capacity binding, competing deadlines,
  shared blockers) surface because you read all the summaries at once —
  the thing no single project block shows.
- **Per-owner commitments** roll up what each partner owes across their
  projects, both directions.
- **Urgency flags only when real**: slip risk, decision due, past-due ask,
  or escalated risk. A quiet project doesn't draw attention.
- **Forward calendar** (from ClickUp milestones) still grounds the
  timeline — it's the material behind the deadline reasons in the Focus
  list.

## Failure modes

- **Bare `cp prep-planning` exits non-zero.** Intentional (the deprecated
  engine-rendered inventory used to be the default and kept overwriting
  `_planning.md` with the pre-synthesis dump). The supported flows are
  `--bundle` and `--summary` — this skill already uses them. The old dump
  remains available behind `--legacy-render` if someone explicitly wants
  it.
- **`cp prep-planning` fails with config error.** Run `cp init` if
  `.cp-engine.toml` is missing. Otherwise check the error and resolve.
- **`CLICKUP_API_TOKEN` unset or invalid.** `cp prep-planning --summary`
  currently returns `milestone_counts: {total: 0, fetched: 0, errored: 0}`
  and `errors: []` — i.e. the failure is silent on the summary output
  (engine fix tracked for v0.16). If you see all-zero milestone counts
  AND no errors AND the doc renders `_Could not fetch milestones — check
  ClickUp connection._` for affected projects, verify the token:
  `echo $CLICKUP_API_TOKEN | head -c 20` should start with `pk_`. Set it
  in your shell or `mc-2/backend/.env`.
- **Forward calendar shows `_(ClickUp list not set — milestones not
  tracked)_`.**
  <!-- TODO(v0.16): split this rendering at the engine level so the
       skill can disambiguate the two cases without guessing. -->
  The message is ambiguous — either:
  (a) the project genuinely has no `clickup_list_id` in MC-2 (fix: set
      one via MC-2 dashboard or a migration), OR
  (b) the list IS set but has zero tasks tagged `milestone` (fix: back-
      populate milestones via Task 29's pipeline, OR add a milestone
      task directly in ClickUp).
  Today's first runs of `cp prep-planning` will see (b) for every
  project until back-population happens; that's normal.
- **Forward calendar empty for all projects.** No milestones have been
  added to ClickUp yet. Fresh tenants and the first run after v0.15
  ships look thin until back-population happens.
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
- Doesn't write to ClickUp. Pure read on the ClickUp side too — never
  creates, updates, or completes tasks.
- Doesn't ingest transcripts (that's `/cp-ingest`).
- Doesn't update master-cp.md or weekly-cp.md (those have their own
  paths via `cp sync` and `/cp-ingest` respectively).
- Doesn't auto-commit.
