---
allowed-tools: Bash(cp:*), Bash(cat:*), Bash(ls:*), Bash(mkdir:*), Bash(date:*), Bash(test:*), Bash(jq:*), Bash(echo:*), Read
description: Prepare a forward-looking sprint planning doc from current cp tenant state.
---

# /cp-prep

Generate a forward-looking, account-grouped sprint-planning doc for the
upcoming sprint. Pulls per-project milestones from ClickUp + open
commitments + urgent flags + capacity-binding constraints + decisions
partners owe each other into one document partners can read before
(or scroll during) sprint planning.

Replaces the older backward-looking agenda generator. As of cp-engine
v0.15.0 the engine command is `cp prep-planning`. `cp prep-agenda`
still works but is deprecated.

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

```bash
WEEK_ISO=$(cp prep-planning --summary 2>/dev/null | jq -r .week_iso 2>/dev/null)
# Fall back to today's ISO week if --summary fails.
test -n "$WEEK_ISO" && test "$WEEK_ISO" != "null" || WEEK_ISO=$(date -u +%Y-W%V)
echo "Planning week: $WEEK_ISO"
```

Per cp-engine v0.8.7.3, the engine resolves the planning week itself —
the skill just reads it back.

### 3. Generate the planning doc

If invoked with no args:

```bash
mkdir -p "sprints/$WEEK_ISO"
PLANNING_PATH="sprints/$WEEK_ISO/_planning.md"
cp prep-planning --out "$PLANNING_PATH"
```

If invoked with project codes (e.g. `/cp-prep ggl-5168 ibx-5167`):

```bash
mkdir -p "sprints/$WEEK_ISO"
# Sanitize args into a comma-separated string.
CODES=$(echo "$@" | tr ' ' ',')
PLANNING_PATH="sprints/$WEEK_ISO/_planning-${CODES//,/-}.md"
cp prep-planning --projects "$CODES" --out "$PLANNING_PATH"
```

The engine's `cp prep-planning` does the heavy lifting (ClickUp milestone
fetch per project, urgent-flag detection, capacity-binding analysis,
cross-cutting decisions parse from weekly-cp.md, account grouping,
markdown rendering). The plugin just orchestrates.

Note: a sync of an existing tenant may still have an `_agenda.md` from
prior runs of the older `cp prep-agenda` command. Both files can
coexist; `_planning.md` is the current source of truth.

### 4. Surface highlights via the engine's summary mode

Use `cp prep-planning --summary` to get structured JSON metrics.

```bash
SUMMARY=$(cp prep-planning --summary ${CODES:+--projects "$CODES"})
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
  "milestone_counts": {"total": 87, "fetched": 84, "errored": 3},
  "urgent_counts": {"slip_risk": 0, "decision_due": 0, "past_due_ask": 0, "escalated_risk": 0},
  "capacity_binding": [
    {"owner": "Tony", "count": 6},
    {"owner": "Marcello", "count": 5}
  ],
  "cross_cutting_decisions_count": 3,
  "errors": []
}
```

Render to the user:

```
Generated planning doc → sprints/$WEEK_ISO/_planning.md
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

- If `capacity_binding` is empty, render
  `Capacity binding: none flagged (no owner ≥ 5 projects)`.
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

- The doc surfaces **everything you'd need to walk into the meeting prepared**:
  per-project Where → Forward calendar → Open commitments.
- **Urgency flags only when real**: slip risk, decision due, past-due ask,
  or escalated risk. A quiet project doesn't draw attention.
- **Capacity binding** ensures the meeting addresses workload imbalance
  before someone's projects silently get skipped.
- **Cross-cutting decisions** appear at the top so partners arrive
  knowing what they owe each other.
- **Forward calendar** (from ClickUp milestones) is the load-bearing
  shift from the old prep-agenda — it makes the doc forward-looking
  instead of backward-looking.

## Failure modes

- **`cp prep-planning` fails with config error.** Run `cp init` if
  `.cp-engine.toml` is missing. Otherwise check the error and resolve.
- **`CLICKUP_API_TOKEN` not set.** Per-project milestone fetch returns
  errors. The doc renders `_Could not fetch milestones — check ClickUp
  connection._` for affected projects. Set `CLICKUP_API_TOKEN` on the
  system running `cp prep-planning`.
- **Project has no `clickup_list_id`.** Block renders `_(ClickUp list
  not set — milestones not tracked)_`. Add the list id via MC-2
  dashboard or migration.
- **Forward calendar empty for all projects.** No milestones have been
  added to ClickUp yet. Fresh tenants and the first run after v0.15
  ships look thin until back-population happens.
- **Planning doc is mostly empty.** Likely a fresh tenant or a sprint
  with no recent ingest activity. The structure is right; data flows
  in as `/cp-ingest` runs against transcripts.
- **Quick Resume / Where blocks show as missing.** Project cp.md files
  have only template placeholders. Need durable cp.md updates from
  prior `/cp-ingest` runs to populate them.

## What this command doesn't do

- Doesn't write to project cp.md files. Pure read.
- Doesn't write to ClickUp. Pure read on the ClickUp side too — never
  creates, updates, or completes tasks.
- Doesn't ingest transcripts (that's `/cp-ingest`).
- Doesn't update master-cp.md or weekly-cp.md (those have their own
  paths via `cp sync` and `/cp-ingest` respectively).
- Doesn't auto-commit.
