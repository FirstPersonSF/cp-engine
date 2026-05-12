---
allowed-tools: Bash(cp:*), Bash(cat:*), Bash(mktemp:*), Bash(rm:*), Bash(ls:*), Bash(mkdir:*), Bash(date:*), Bash(git:*), Read, Write
description: Ingest a transcript into the cp tenant — classify, plan, confirm, execute.
---

# /cp-ingest

Ingest a meeting transcript into the cp tenant. Read it, classify which
projects it touches, produce a structured plan, confirm with the user,
then execute via the deterministic `cp ingest --plan` verb. Saves a
plan-log artifact regardless of outcome.

**Argument:** path to a transcript file (Fathom export, custom export,
or anything with the standard `MM:SS - Speaker` line format).

## What you do

Run these steps in order. **Do not skip steps; do not improvise.**

### 1. Confirm you're in the cp tenant root

```bash
test -f "$(pwd)/.cp-engine.toml"
```

If not, stop and tell the user: "Run /cp-ingest from the cp tenant root
(e.g. ~/Documents/Python/cp) — that's where transcripts get routed from."

### 2. Audit the transcript

```bash
# Pass the active project codes via --codes so mentioned_codes lights up.
ACTIVE_PROJECTS=$(cp list-active-projects --scope all)
CODES=$(echo "$ACTIVE_PROJECTS" | jq -r '[.[].code] | join(",")')
AUDIT=$(cp parse-transcript "<TRANSCRIPT_PATH>" --codes "$CODES")
echo "$AUDIT"
```

Surface the audit to the user. Use this template **exactly** — adapt the
content but keep the shape:

```
Transcript: <basename of path>
Duration: <duration_minutes> min · Speakers: <comma-separated speaker list>

⚠️ Audio gaps detected (will skip writes if scope unclear):
  - <start> → <end> (<duration> min)

Action items (Fathom-extracted):
  - <text> [<timestamp>]

Mentioned project codes: <comma-separated, or "(none — Claude will classify)">

Proceed with classification? [Y/n]
```

If `gaps` is empty, omit the warning block. If `mentioned_codes` is empty,
note that Claude will do full classification from transcript content.

**Wait for user confirmation before continuing.** If the user wants to
abort here (e.g. "the transcript is bad, skip it"), stop and tell them
no writes happened.

### 3. Classify projects via Claude

Read the transcript yourself. Identify which active projects it touches.
For each, draft the entries that should land in the sprint file:

- **Inbound updates** — what the client told us this meeting.
- **Open asks** — what we still need from the client (or each other).
- **Decisions** — concrete decisions made (flag cross-cutting ones).
- **Risks** — newly surfaced or escalated risks.
- **Stakeholders** — new people mentioned with roles + context.

For tenant-wide content:
- **Themes** — high-level threads spanning multiple projects.

Use the active-projects JSON from step 2 to map references like
"Google 5168 activation" → canonical code `ggl-5168`.

### 4. Build the plan YAML

Schema:

```yaml
transcript:
  source: file  # or "fathom" if from /cp-ingest-fathom (v0.8.7)
  path: <relative or absolute path to the transcript>

projects:
  <code>:                          # canonical project code
    inbound:
      - text: "..."
        date: "YYYY-MM-DD"          # ISO date of the inbound signal
        who: "<who said it>"
    asks:
      - text: "..."
        who: "<who we're asking>"
        by: "YYYY-MM-DD or W##"     # optional deadline
        status: "open"               # default; "closed" for resolved
        date: "YYYY-MM-DD"           # optional; defaults to today
    decisions:
      - text: "..."
        date: "YYYY-MM-DD"
        cross_cutting: false         # true → also surfaces in weekly-cp.md decisions-strip
    risks:
      - text: "..."
        severity: "watching"         # or "escalated" or "dependency"
        category: "schedule"         # or contract, scope, technical, etc.
        date: "YYYY-MM-DD"
    stakeholders:
      - name: "<name>"
        role: "<role>"               # optional
        context: "<one-line context>"# optional

themes:
  - text: "<theme spanning multiple projects>"
    date: "YYYY-MM-DD"
```

**Skip empty sections** — don't include `decisions: []`. Only include
verbs that have at least one entry.

Write the plan to a temp file:

```bash
PLAN=$(mktemp -t cp-ingest-plan.XXXXXX.yaml)
# Use the Write tool to put the YAML into $PLAN.
```

### 5. Dry-run validate

```bash
cp ingest --plan "$PLAN" --dry-run
```

Output is JSON describing what would happen. Surface a summary to the
user:

```
Plan validated. Would touch:
  - <code>: <N> inbound, <N> asks, <N> decisions, ...
  - <code>: ...

Themes: <N>

Execute? [Y/n]
```

**Wait for user confirmation before continuing.** If they want changes,
edit the plan and re-validate.

### 6. Execute

```bash
cp ingest --plan "$PLAN"
```

Output is JSON: `{files_written, skipped_duplicate, errors}`. Surface
the summary:

```
Wrote N files. Skipped M duplicates. Errors: 0.

Files:
  - sprints/2026-W19/ggl-5168.md
  - sprints/2026-W19/ggl-5151.md
  - ...
```

If `errors` is non-empty, **don't claim success.** Show the errors
list to the user and ask whether to retry or abort.

### 7. Save the plan to the ingest log

```bash
WEEK=$(cp parse-sprint --json sprints/2026-W19/cp.md 2>/dev/null | jq -r .week_iso 2>/dev/null || echo "$(date +%Y-W%V)")
LOG_DIR="sprints/$WEEK/_ingest-log"
mkdir -p "$LOG_DIR"
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
cp "$PLAN" "$LOG_DIR/$TS.yaml"
```

The plan stays as the audit artifact: "what did Claude actually do
during this ingest?" answerable later by reading `_ingest-log/<ts>.yaml`.

### 8. Clean up

```bash
rm -f "$PLAN"
```

### 9. Report

Show the user a one-paragraph confirmation:

> Ingested `<transcript basename>` into the cp tenant.
> Touched <N> projects (<comma-separated codes>).
> Plan saved to `<log path>` for audit.
> Run `git status` to see the diff; commit when ready.

**Do NOT commit/push automatically** in v0.8.6 — the user reviews + commits
themselves. v0.8.7's auto-ingest path will commit with the `[fathom-ingest]`
prefix.

## What good looks like

- The audit step **always** runs before any writes. Audio gaps, missing
  speakers, no mentioned codes — all surfaced before the user commits to
  proceeding.
- Each plan entry has a clear bracket-formatted bullet trail back to the
  transcript. The "text" field is concrete, not generic.
- Cross-cutting decisions are flagged explicitly — they're what light up
  weekly-cp.md's decisions-strip.
- Stakeholders are captured opportunistically — every new client-side
  person mentioned with a role gets one entry, even if no other content
  about them is in the transcript.
- The plan is saved to `_ingest-log/` regardless of success or failure.
  Audit trail trumps cleanup.

## Failure modes

- **Audit shows audio gap > 5 min.** Strongly prefer asking the user to
  confirm scope manually rather than guessing. Half a meeting is missing;
  the deepening will be a partial reconstruction. Flag this explicitly.
- **No mentioned_codes AND Claude can't classify.** Probably a transcript
  with no project content (e.g. a casual call). Tell the user "this
  transcript doesn't touch any cp-tracked projects — skipping."
- **Plan validation fails.** `cp ingest --dry-run` prints the validation
  error. Fix the plan and re-dry-run. Don't proceed to execute.
- **Sprint file missing for a referenced project code.** Plan executor
  errors with `sprint file missing for <code>`. Means the project isn't
  active this sprint per MC-2. Either the classification is wrong
  (project not really mentioned, or different code) or the project needs
  to be re-activated in MC-2 + a `cp sync` run.

## What this command doesn't do

- Doesn't read from Fathom directly — that's `/cp-ingest-fathom` in v0.8.7.
- Doesn't commit changes — user reviews + commits manually.
- Doesn't write to project `cp.md` durable sections directly. Those get
  populated automatically on next `cp sync` from the strip regions
  (which read from the sprint files this command wrote to).
- Doesn't update `weekly-cp.md` directly. Same flow as above — write
  bracket-formatted decisions into sprint files with `cross_cutting: true`,
  let sync project them into the weekly-cp.md decisions-strip.
