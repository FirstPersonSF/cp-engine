---
allowed-tools: Bash(cp:*), Bash(cat:*), Bash(ls:*), Bash(mkdir:*), Bash(date:*), Bash(test:*), Bash(jq:*), Bash(echo:*), Read, Write
description: Author a close-out wrap report — the learning artifact for a finished engagement.
---

# /cp-wrap

Author the **wrap report** for a finished (or finishing) engagement: the
durable record of what the work taught the firm. The engine emits a
**bundle** of measured facts (`cp wrap <code> --bundle`) and **you
synthesize** the report against a fixed nine-section contract, then render
it as a Word document a human will actually read.

**This is not `cp close`.** `cp close` is the internal hygiene ritual —
spine tidying, stub retires, terminal Exec Summary. `/cp-wrap` produces the
artifact you read a year later before pitching the same client again. Run
`/cp-wrap` FIRST, so the close-out's terminal Exec Summary can quote it.

**Arguments:**
- `/cp-wrap <code>` → wrap report for that engagement or initiative.

---

## Why this exists

ibx-5192's retrospective was written by hand on 2026-08-14. It concluded
**"actual hours: not captured"** — while `sprint_allocations` held 270+
hours across five people the whole time. It also missed the fact that 66%
of all meeting time fell in the final two weeks, which is the single
number that explains the engagement.

A hand-written retro records what you *remember*. The bundle supplies what
is *true*. Neither is sufficient alone — the facts don't interpret
themselves, and memory doesn't count hours.

---

## What you do

### 1. Confirm the tenant root

```bash
test -f "$(pwd)/.cp-engine.toml"
```

Not there → stop: "Run /cp-wrap from the cp tenant root."

### 2. Get the bundle

```bash
BUNDLE=$(cp wrap "$CODE" --bundle)
echo "$BUNDLE" | jq .
```

Fields you will use:

| Field | What it is |
|---|---|
| `duration_weeks`, `start_date` | span; end falls back to the last meeting (MC-2 has no `end_date`) |
| `budget`, `target_profit_pct`, `budget_per_hour` | the commercial frame |
| `effort.by_person`, `effort.total_hours`, `effort.verified` | **allocated** hours per person |
| `meetings.tail_share`, `.heaviest_days`, `.count` | cadence — and WHERE it fell |
| `deliverables` | live Deliverables-layer spine elements |
| `feedback_artifacts` | worklists in `feedback-on-deck/` — a count that means something |
| `open_commitments` | what is still owed at close |
| `not_assessable_from_data` | the blanks you must NOT fill |

### 3. Read the project's own record

The bundle is facts, not narrative. Before writing, read:

- the project's `cp.md` **Exec Summary + Updates history** — the running
  account of what happened, week by week
- the **final sprint file** — what was live at the end
- any **retrospective or punchlist** already in the working dir
- recent `meetings/` artifacts if the ending is unclear

**Read the artifact over the transcript.** Where a delivered file and
someone's account of it disagree, the file wins. (That rule was itself an
ibx-5192 finding.)

### 4. Synthesize the report — the nine-section contract

Write `<workdir>/wrap-report-<code>-<date>-v01.md`. Same shape every time,
so two wrap reports are comparable:

1. **Outcome** — what shipped, who approved it, in their words if you have
   them. One paragraph. Lead with the verdict, not the chronology.
2. **Shape of the engagement** — the facts table, straight from the
   bundle. Duration, budget, hours by person, meetings, rounds,
   deliverables. Numbers only.
3. **What actually happened** — the honest narrative. Include the parts
   that were uncomfortable; a retro that only records wins is worth
   nothing.
4. **What worked, and is worth repeating** — 3–6 items, each concrete
   enough to do again. Name the specific artifact or decision, not the
   virtue.
5. **What cost us, and what to change** — 3–6 items, each with a *Change:*
   line. A cost without a change is a complaint.
6. **Client relationship** — communication pattern, decision-making, who
   could actually END a round (often not the person giving feedback),
   relationship strength, maintenance needed. **This is the axis that
   compounds across engagements and the one a hand-written retro skips.**
7. **Commercial** — budget, target margin, allocated hours, budget÷hours.
   Say plainly whether the project made money or whether you cannot tell.
8. **Not assessed** — every field in `not_assessable_from_data`, named as
   an open question. **Never guess these.**
9. **Open at close** — a table: item, owner, note. What survives the
   engagement.

### 5. Render it as Word

Markdown is the source of record; the `.docx` is what people read.

```bash
cp wrap "$CODE" --facts-docx "<workdir>/wrap-report-<code>-<date>-v01.docx"
```

That writes the **facts half**. Then extend it with your authored sections
using `cp_engine.wrap_docx` (`WrapSection(heading, body=..., table=...,
blanks=[...])`), preserving section order from the contract.

Then push it where humans look:

```
push_to_dropbox(project_code, local_path)
```

The default destination is `03 Assets/06 Spine/` — do not pass a bare
`dest_name`, which would drop it at the project root.

### 6. Record it in the spine

Create ONE `retrospective`-layer element (`create_spine_element`) carrying
the *transferable finding* — not the whole report. The document is the
detail; the element is what a future project should inherit. Mark it
`important=true`.

### 7. Don't commit for the user

Report the paths. Committing is their call.

---

## Rules that matter

**Never fill a `not_assessable_from_data` field.** Budget outcomes,
licensing clearances, work-page candidacy, ratings — these are judgement
and commercial fact. A model guessing them is worse than a visible blank,
because a blank prompts a human and a guess does not.

**Call allocated hours "allocated".** They are MC-2 planning rows, not
timesheets. Presenting an allocation as an actual overstates precision on
the one number a scope conversation turns on.

**If `effort.verified` is false, say hours are unread — do not state a
margin.** An unread table must never render as "this project used no
hours."

**Quote the client verbatim where you have it.** "The decks are now
complete, make sense and look amazing" carries more than any paraphrase.

**Distribution beats totals.** "39 meetings" is inert; "66% of meeting time
in the final two weeks" is a finding. Always check `tail_share` and
`heaviest_days` and say what they mean.

**Version, don't overwrite.** A wrap report that gets corrected becomes
`-v02`, and the reasoning behind the v01 error stays legible. Never "final"
in a filename.

---

## What good looks like

- **A stranger could read it.** Someone who wasn't on the project
  understands what happened, what it cost, and what to do differently.
- **Every number is measured or explicitly blank.** No plausible-looking
  estimates.
- **The client section is specific** — named patterns and named people, not
  "communication was good."
- **Costs carry changes.** Each one is actionable next engagement.
- **The uncomfortable parts survive.** If mid-project sentiment was bad and
  the ending was good, both are recorded — that contrast is often the most
  useful thing in the document.
- **Nine sections, in order, every time.**

## Failure modes

- **`cp wrap` refuses: MC-2 unreachable.** Deliberate. The bundle is mostly
  MC-2 facts; one built from the disk mirror would understate effort, which
  is the exact failure this exists to prevent. Restore connectivity.
- **`effort.total_hours` is 0 with `verified: true`.** The project genuinely
  has no allocation rows. Say so — don't infer hours from meeting time.
- **`open_commitments` looks empty and you doubt it.** Cross-check with
  `cp commitments-sweep <code>`. (That verb was blind for every engagement
  until 2026-08-14 — a wrong empty is a known failure shape here.)
- **The `.docx` lands at the Dropbox project root.** `push_to_dropbox` was
  called with a bare `dest_name`, or `cp mcp` is serving stale bytecode —
  check its version warning and restart `/mcp`.

## What this command doesn't do

- Doesn't run the close-out ritual — that's `cp close`, and it comes after.
- Doesn't mutate the spine beyond the one retrospective element.
- Doesn't resolve commitments or archive anything.
- Doesn't decide whether the project was a success. It assembles the
  evidence; the rating is a human field.
