---
allowed-tools: Bash(cp:*), Bash(cat:*), Bash(ls:*), Bash(mkdir:*), Bash(date:*), Bash(test:*), Read
description: Prepare a sprint planning agenda from current cp tenant state.
---

# /cp-prep

Generate a structured agenda for the upcoming sprint planning meeting.
Pulls per-project Quick Resume + recent inbound + open asks aged + decisions
due + cross-referenced weekly-cp.md decisions into one document partners
can read before (or scroll during) the meeting.

The retro Tier 2.4 motivation: the W19 sprint planning ran out of time
before reaching Tony's projects because there was no pre-meeting agenda
to surface workload up front. This closes that gap.

**Arguments (optional):**
- (no args) → full sprint planning agenda for all active projects.
- `<code> [<code> ...]` → scoped agenda for the named projects only
  (useful for ad-hoc client-meeting prep, not just sprint planning).

## What you do

### 1. Confirm cp tenant root

```bash
test -f "$(pwd)/.cp-engine.toml"
```

If not, stop and tell the user: "Run /cp-prep from the cp tenant root
(e.g. ~/Documents/Python/cp)."

### 2. Determine the current planning week

```bash
WEEK_ISO=$(cp prep-agenda --projects __nonexistent__ 2>/dev/null | head -1 | sed -n 's/^# Sprint \([0-9-W]*\) Planning.*$/\1/p')
# If that fails, fall back to today's date.
test -n "$WEEK_ISO" || WEEK_ISO=$(date -u +%Y-W%V)
echo "Planning week: $WEEK_ISO"
```

Per cp-engine v0.8.7.3, this matches MC-2's planning-week rule
(Mon/Tue → this week, Wed-Sun → next week).

### 3. Generate the agenda

If invoked with no args:

```bash
mkdir -p "sprints/$WEEK_ISO"
AGENDA_PATH="sprints/$WEEK_ISO/_agenda.md"
cp prep-agenda --out "$AGENDA_PATH"
```

If invoked with project codes (e.g. `/cp-prep ggl-5168 ibx-5167`):

```bash
mkdir -p "sprints/$WEEK_ISO"
# Sanitize args into a comma-separated string.
CODES=$(echo "$@" | tr ' ' ',')
AGENDA_PATH="sprints/$WEEK_ISO/_agenda-${CODES//,/-}.md"
cp prep-agenda --projects "$CODES" --out "$AGENDA_PATH"
```

The engine's `cp prep-agenda` does the heavy lifting (parsing weekly-cp.md
decisions, reading per-project cp.md Quick Resumes, computing strip
rollups, rendering markdown). The plugin just orchestrates.

### 4. Surface highlights

Read the generated file. Surface a brief summary to the user — don't
dump the whole agenda inline. Focus on what the partners need to know
before opening the file:

```
Generated agenda → sprints/2026-W19/_agenda.md

Tenant context:
- N themes carried from prior week
- M cross-cutting decisions to reference
- K projects with carry-forward urgency (open asks > 7d, decisions due,
  escalated risks)

Per-project urgency flags (🔴):
- `ggl-5168`: 2 open asks > 7d (oldest 12d), 1 decision due
- `ibx-5167`: 1 escalated risk
- `tel-5149`: 0 strip activity in last 4 weeks (potentially stalled)

Owner workload (from master-cp last-week-workload):
- Tony: 8 projects (consider splitting his block in the agenda flow)
- Brandon: 4 projects
- Marcello: 4 projects
- Drew: 5 projects

Read `sprints/$WEEK_ISO/_agenda.md` to see the full per-project blocks.
```

The "consider splitting" callout is what would have prevented W19's
"ran out of time before Tony" miss. If one owner has >>others,
suggest splitting the meeting time accordingly.

### 5. Don't commit

The agenda is a working artifact for the meeting. Whether to commit it
is a per-team call (some prefer the audit trail; others find the file
churn distracting). Default: don't auto-commit. Tell the user:

> Agenda is at `sprints/<W##>/_agenda.md`. Commit when you're satisfied
> with it (or leave uncommitted if you treat it as ephemeral).

### 6. Re-running

`/cp-prep` is idempotent — re-running overwrites `_agenda.md` in place
with fresh state. Safe to re-run after a `cp sync`, after a `/cp-ingest`,
or anytime new content lands.

## What good looks like

- The agenda surfaces **everything you'd need to walk into the meeting prepared**:
  per-project quick resume, what's burning, what's been decided.
- **Urgency flags only when real**: a quiet project doesn't get a "discuss"
  prompt. Partners' eye gets pulled to what matters.
- **Cross-referenced weekly decisions** appear under each project — so when
  you hit ggl-5168, you see Decision #6 (Roadshow ships alongside pop-ups)
  right there, not 30 lines down in weekly-cp.md.
- **Owner-workload callout** ensures the meeting doesn't silently skip
  someone's projects.

## Failure modes

- **`cp prep-agenda` fails with config error.** Run `cp init` if `.cp-engine.toml`
  is missing. Otherwise check the error and resolve.
- **Agenda is mostly empty.** Likely a fresh tenant or a sprint with no
  recent ingest activity. The structure is right; data flows in as
  `/cp-ingest` runs against transcripts.
- **Quick Resume blocks all show as missing.** Project cp.md files have
  only template placeholders (no real Quick Resume content yet). Need
  durable cp.md updates from prior `/cp-ingest` runs to populate them.
  The retro Tier 2.4 design's biggest dependency.

## What this command doesn't do

- Doesn't write to project cp.md files. Pure read.
- Doesn't ingest transcripts (that's `/cp-ingest`).
- Doesn't update master-cp.md or weekly-cp.md (those have their own paths
  via `cp sync` and `/cp-ingest` respectively).
- Doesn't auto-commit.
