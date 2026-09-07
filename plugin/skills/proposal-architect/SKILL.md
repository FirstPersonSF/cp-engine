---
name: proposal-architect
description: Write a First Person Engagement Summary from a cp project — running the readiness preflight first, selecting only approved services from the live Service Library in MC-2 (never an uploaded file), and persisting the result as a versioned spine element. Use when the user says "write an engagement summary", "draft a proposal for <code>", "build the engagement summary", or asks to turn a brief into a client-ready scope document.
---

# Write an Engagement Summary

An Engagement Summary is what the client signs against. It states what
they need, what we will do, what they receive, and what it costs — and it
becomes the orienter for every downstream artifact on the engagement.

Two jobs at once:

- **Service Librarian** — select only approved Activities and Outputs
  from the live library, preserving Reference IDs and official
  definitions exactly.
- **Proposal Architect** — turn real project scope into a clear,
  persuasive document in the standard structure.

## The hard rules

**1. Run `cxp preflight <code>` first.** If it says NOT READY, do not
draft. It has three gates and each catches a different failure:

| gate | means |
|---|---|
| unauthored scaffold | the project exists but nobody has written it up |
| `shape_warning` | wrong KIND of project for this artifact |
| `funding_warning` | right shape, unfunded scope |

**2. The Service Library lives in MC-2. Call `list_services`.** Never use
an uploaded spreadsheet, a Google Doc, or an Airtable link — all three
are retired. A Reference ID from a stale source is indistinguishable from
a correct one once it reaches a client.

**3. Verify every ID with `get_service` before citing it.** A fabricated
`[A.###]` looks exactly like a real one in a finished document. This is
the only place the difference can be caught.

**4. When something material is missing, ASK.** An invented phase,
deliverable or date is a promise nobody at First Person made. If you draft
anyway at the user's request, mark each gap inline as `[ASSUMPTION: …]`.

---

## Step 1 — Preflight

```
cxp preflight <code>
```

`found` is your scope. **Deliverables come from CP, never from
invention.** Read `conflicts` carefully — a schedule disagreement is not a
blocker, but the summary must take a position and you should say which.

## Step 2 — Establish the engagement type

**Before selecting a single service.** This decides which half of the
library is even in play, and getting it wrong produces a document that is
well-written and about the wrong job.

- **Creative / production** — the client owns the strategy: their own
  brief, campaign platform, messaging. They hire us to MAKE something.
  Draw from Creative development, Visual design, Content production,
  Experience design. **Story-strategy Activities are out of scope.**
- **Strategy** — we do the upstream thinking: narrative, category, brand,
  research.
- **Combined** — genuinely both, usually phased. Real, but least common.

**Default to the narrower reading.** If the client has a marketing team,
an existing campaign, or an agency of record, they own the strategy.
Proposing to do it for them reads as not listening, or as padding.

A client whose inputs are *unsettled* is a client doing their own
strategic work — note the dependency, do not absorb it.

## Step 3 — Confirm understanding

Play back **client need → purpose/scope → phases → expected outcomes**
and get a yes. A rough brief and a finished proposal are far apart, and
that distance is where invented scope enters.

## Step 4 — Select services

```
list_services(kind="output", category="Content production")
list_services(search="storyboard")
get_service("A.053")
```

For each: verify the ID, the official name, the definition, genuine fit,
and any `guidance` field. **Read `guidance`** — it is where two
similar-looking items are told apart.

**Deprecated items are excluded by default and you should leave it that
way.** `include_deprecated=True` exists only to explain an ID found in an
older proposal.

If work has no matching item, say so and offer: (a) the nearest real
item, naming what the fit loses, or (b) a proposed new library entry for
approval. Never write your own.

## Step 5 — Draft

Seven sections, mirroring the Engagement Summary template:

1. **`<ProjectName>`** — summary, then "The approach will address
   `<Client>`'s desire to:" and exactly **three** outcomes. Under 250
   words. Total Cost / Total Time table.
2. **Approach** — one block per phase:
   `## Phase N: <Name>` · summary (25–125 words, explain WHY these
   activities solve the problem) · **Key Activities** · **Deliverables**
   *(Outcomes belong in Section 1, not per phase.)*
3. **Timetable & Review Windows** — milestones, schedule, review cadence
   (standard: 2 cycles, feedback within 1 business day)
4. **Dependencies, Risks and Exclusions** — 4.1 Alignment ·
   4.2 Client Provisions · 4.3 Contract & Payment · 4.4 Partner
   Collaboration · 4.5 Exclusions
5. **Payment Schedule** — standard 50/50, Net 30
6. **Licensing Information** — pick the "no licensing" or "some
   licensing" block, never both
7. **Additional Expenses Disclosure**

Leave any figure you were not given as its placeholder (`$000,000`,
`00 weeks`). **A plausible invented number is the most dangerous thing
this document can contain.**

### Activities vs Outputs vs Outcomes

| | | |
|---|---|---|
| **Activity** | what we DO | "Stakeholder Interviews" |
| **Output** | what they RECEIVE | "Research Findings Report" |
| **Outcome** | the client-side END-STATE | "Leadership aligned on one narrative before launch" |

An Outcome is never a deliverable restated. Keeping these apart is most
of what makes a summary read as a proposal rather than an invoice.

### Write deliverables at the specificity of real ones

The official definition is generic on purpose — reusable across clients.
Quote it, then add what makes it *this* engagement's version: counts,
formats, dimensions, named audiences, review gates.

> **Final Video Package** — Complete set of finished video executions:
> one (1) :30, two to three (2–3) :15s, and two to three (2–3) :06s —
> each delivered in 16:9, 9:16, and 1:1 aspect ratios, with high-quality
> thumbnail stills, voiceover-led audio masters, and all source files
> packaged for delivery.

Name what a deliverable *decides* where it has that role ("the go/no-go
gate before principal photography"). Carry unknowns explicitly — "final
specs to be confirmed by the booth vendor" — never resolve them silently.

## Step 6 — Self-check before delivering

State each result; do not just assert the document is fine.

- [ ] Every `[A.xxx]`/`[O.xxx]` verified with `get_service`, definitions unaltered
- [ ] No deprecated item cited
- [ ] Every phase has Summary / Key Activities / Deliverables
- [ ] Exactly three Outcomes, in Section 1, and none is a restated deliverable
- [ ] No `[ASSUMPTION: …]` left unmarked
- [ ] Section 1 under 250 words
- [ ] Engagement type right — no Story-strategy Activities in a
      creative/production job unless the client asked
- [ ] Nothing invented: no phase, deliverable, date or figure absent from
      the brief or supplied by the user

## Step 7 — Persist

The summary is **a versioned spine element, not a file**. On one real
engagement the delivery date changed three times in a session; in a loose
document that is four edits each time.

Create on `cp-hosted`:

```
create_spine_element(
  project_code = "<code>",
  layer        = "Brief",
  framing      = "Engagement Summary — <project>",
  body         = "<the summary>",
  important    = true,
  sources      = [ …the sprint file, any ingested client brief… ]
)
```

Every revision after that is `add_spine_version`. Dates then live in one
place and the trail is diffable. Write a shareable file too if asked —
but the element is the source of truth and the file is an export of it.

## What this skill will not do

**Invent a Reference ID, a name, or a definition.** If it is not in
`list_services`, it does not exist yet.

**Rewrite an official definition.** Tailor with a separate "Suggested
reframing:" line beneath it.

**Send anything.** Drafting and sending are different acts, and the second
is the user's.
