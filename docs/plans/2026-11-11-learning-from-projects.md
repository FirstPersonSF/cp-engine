---
Project: cp-engine — Learning from projects (archive rethink)
Provenance: Version 03 | 2026-11-11
Filename: docs/plans/2026-11-11-learning-from-projects.md
Author: Claude (with Drew)
---

# Learning from projects

## Why

Drew, 2026-11-11: *"I'd really like to continuously learn from our projects
and have all of the context available."* Learning means:

- **Search past work** when scoping new work, and remember the details.
- **A written lesson per project**: what worked, what took longer than
  estimated, what we'd price differently.
- **Patterns** across projects, by **client** and by **project type**.
- **Client knowledge**: personality, what tends to happen, what to look out
  for (the Janet lookouts, generalised to every client).
- **Small operational lessons** that are hard to keep in mind but matter.
  For example: *get travelling freelance directors to submit expenses early,
  or they arrive after the final invoice.*
- **Making cp and MC-2 better over time**: where we miss things, where
  clients cause problems.

**Archive still closes the tools.** Otherwise the active list becomes
unmanageable. What changes is that closing a project also *distils* it, and
everything it held stays reachable.

## Where things stand (2026-11-11)

| Area | Today |
|---|---|
| MC-2 archive (#208/#212) | Closes the tools: moves Dropbox to `archive_path` and archives Slack, ClickUp, Miro, Drive and Frame.io. Writes `projects.archive_receipt`; `archive_reconcile.py` runs daily. |
| Context of archived projects | The 30 Archived projects hold 18 active sources, 19 meetings and 22 live spine rows in total. Most were archived before ingest existed, so their substance sits unindexed in Dropbox archive folders. The 8 Closed projects hold 108 sources, 63 meetings and 113 spine rows. |
| Search | The `match_chunks*` RPCs filter `status='active'` only. They ignore project status, so past work is already searchable once it has been ingested. |
| Close-out report | `wrap_report.py` gathers the facts, and `/cp-wrap` writes four learning axes: project, client, vendors, scope+budget. It has been run once (IBX 5192, 2026-08-14) across 38 closed or archived projects. |
| Client lookouts | 71 account-scoped stakeholder dossiers, readable from every project of the company. Janet Noe's is one of them. There is no **company-level** lookout, only per-person ones. |
| Project type | `projects.category` is Production (15), Strategy (10) or null (45). There is no campaign/video/etc. type to learn under. |
| Operational lessons | Nowhere. `improvements.md` holds friction with *cp*, not lessons about the *work*. |
| Dead code | `asset_ingest.archive_project_assets` sets `scope='archived'` on a closing project's sources. Nothing calls it, and it contradicts this plan. |

## Project types

Multi-valued: a project can carry more than one (for example a Campaign that
is mostly Video).

| Type | Type |
|---|---|
| Narrative Strategy | Brand |
| Campaign Architecture | Messaging |
| Message Architecture | Research |
| Campaign | Event |
| Video | Web and Digital |
| Product | Experience |
| Design (added 2026-11-11) | |

`category` (Production / Strategy) stays as it is. It is a finance grouping,
not a learning one.

## Built to grow

Drew, 2026-11-11: *"This is going to continuously grow and change."* So:

- **Project types and moments are rows in MC-2 tables, not check
  constraints or code enums.** Adding one is an insert from MC-2 or a verb,
  with no migration and no release.
- **A lesson can carry any number of types and moments.** Adding a new moment
  later never invalidates an old lesson.
- **Free text is accepted** when no listed moment fits. Free-text moments are
  surfaced for promotion into the list in the review queue.
- **Lessons are reviewed every 12 months.** A confirmed lesson older than 12
  months comes back to its owner: keep, edit, or retire.

## Moments

The moments when a lesson bites. This is the starter list, and it will grow.

| Moment | Example lesson shape |
|---|---|
| Kickoff | What to settle before work starts |
| Estimating | What took longer than estimated; what to price differently |
| Process order | Which steps can't be skipped; if one is, what approval replaces it, and when |
| Booking resources | Anticipate who and what is needed, and book it early |
| Hiring freelancers | Agree the hours before they start |
| Contractor hours | Track progress against the agreed hours during the work |
| Scope change | When to tell the client they are out of scope, and when we charge |
| Travel | Expenses in early when people travel |
| Shoot | Production-day lookouts |
| Client review round | Who can actually close a round |
| Legal review | What adds a round, and how long it takes |
| Workload | Someone's plate is too full; what to move |
| Final delivery | What to lock before handing over |
| Invoicing | What has to be in before the final invoice |

## Freelancers

Freelancers are already `entities` rows (`is_external`) with weekly
`sprint_allocations` per project: 426 rows across 12 people. Production
companies are `vendors` (4 rows, with `watch_outs`). So the record of who
worked on what already exists. What's missing:

- **Evaluation:** a `freelancer_reviews` table (entity, project, role, rating
  1–5, recommendation level, strengths, watch-outs, reviewer, date). One review
  per freelancer per project, written at Close. The owner is prompted for it
  alongside the lessons.
- **Agreed hours:** the hours agreed with a contractor for a project, recorded
  before they start, so planned and actual hours can be compared during the
  work. Actual hours come from invoices for contractors who invoice, and from
  Gusto timesheets for those paid through Gusto. `entities.payment_method`
  should say which, but on 2026-11-11 it was empty for 58 of 60 active
  external people (2 were `ach`), and the enum may need a Gusto value. Fill
  it in during phase 5. Today's allocations are planned hours only.
- **Recommendation:** "who should we use for X?" ranks freelancers by
  capability, past project types, ratings and recency, with the evidence
  shown. It is internal only, like client lookouts.

## Workload lookahead

cp's sprint prep has started noticing overloaded people. Make it deliberate.
Per person, look ahead 2–4 weeks across allocations, open commitments by due
date, deliverable dates and meetings, and flag anyone over capacity with a
suggestion of what could move. The `workload` moment ties lessons to it ("the
last time Drew carried three launches at once…").

## Design

### The lesson: one item, tagged

A lesson is one short statement, not a document. Each one carries:

| Field | Meaning |
|---|---|
| `text` | The lesson, 1–3 sentences, phrased as what to do or watch for. |
| `scope` | `person` (a stakeholder) · `client` (a company) · `type` (one or more project types) · `practice` (all our work). |
| `company_id` / `stakeholder` | Set for `client` / `person` scope. |
| `project_types` | Set for `type` scope; optional tags on the others. |
| `when` | One or more moments when it bites, from a list that grows (see "Moments"). |
| `source` | Where it came from: project, meeting, session, wrap report. Every lesson cites its evidence. |
| `status` | `candidate` → `confirmed` → `retired`. Only confirmed lessons are shown. |
| `owner` / `confirmed_by` | Who reviews it, and who confirmed it. |
| `internal_only` | Always true for `person` and `client` scope. |

**Storage: a new MC-2 `lessons` table, not spine elements.** The spine is
per-project distilled memory, with versions and relations. Lessons cross
projects, are small, and are queried by filter (company, type, `when`). They
get their own table with RLS, grants (the mig 127/160 lesson: every new write
needs its grant migration) and a Voyage embedding for search. A person-scoped
lesson *links* to the stakeholder dossier rather than editing it. The dossier
stays the long-form card; lessons are the lookouts drawn from it.

### Recording lessons

1. **During a session:** the trigger phrase `remember this: <lesson>` maps to
   a new hosted verb, `log_lesson`. The model proposes scope, types and
   `when`, and Drew corrects them in one line. It is filed `confirmed`
   because a person said it.
2. **From meetings:** meeting ingest flags *possible* lessons ("next time…",
   "we should have…", "watch out for…") into a candidate queue, the same way
   it handles `alias-candidates.md`. Nothing is auto-confirmed.
3. **At close-out:** the wrap report's four axes are split into candidate
   lessons (see Archive).
4. **Review:** `cxp lessons` and hosted `list_lessons(status='candidate')`;
   `confirm_lesson`, `edit_lesson`, `retire_lesson`. MC-2 gets a Lessons page
   later (phase 6).

### Showing lessons where they help

| Moment | What shows |
|---|---|
| New project setup / opening a project (`get_project_state`, `cxp brief`) | **Lookouts** block: the client's people and company lessons, plus lessons for the project's types. |
| Proposal, preflight, estimate, RFP | Type and practice lessons about scope and budget. Client lookouts inform the author, but **never enter the document**. |
| Sprint prep (`/cp-prep`) | Lessons whose `when` matches what is coming up (an invoice due, a shoot, a review round). |
| Search (`semantic_search`) | Past projects' sources are included. Lessons are searchable on their own. |

**Guardrail:** `person` and `client` lessons never appear in client-facing or
partner-facing output: proposals, RFPs, decks, the Canonic site. This is
enforced in code (an output filter, with tests on the far side of the
boundary), not just in prompt instructions.

### Archive becomes a four-step close

Decided 2026-11-11: **Close distils, Archive closes the tools.**

**Close** (status → Closed):

1. **Final ingest:** pull in the project's remaining Dropbox files and
   meetings.
2. **Draft the close-out report:** written automatically from the
   `wrap_report` facts, stored as a spine element, with the owner to confirm
   it. This uses the same "drafted by cp" pattern as Exec Summaries.
3. **Draft lessons:** each axis of the report becomes candidate lessons,
   scoped and tagged. The **project owner** confirms, edits or drops them.
4. **Review freelancers:** one review per freelancer who worked on the
   project.

**Archive** (status → Archived): close the tools, as today (#208/#212).
Ingest has to keep reading the moved folder (`archive_path`), so a later
backfill still works.

**An archived project leaves context.** It drops out of active views
(already true) **and out of default search**. Today the `match_chunks*` RPCs
ignore project status, so that has to change. An archived project's
sources and meetings are searched only when asked explicitly ("look in
archived projects", an `include_archived` flag). **Its confirmed lessons
and freelancer reviews stay live.** They are the distilled part, and they
are what keeps it useful.

Delete `archive_project_assets` and its CLI command.

### Backfill

Run it over the 38 Closed and Archived projects:

- Ingest each project's Dropbox archive folder.
- Set the project's types.
- Where there is enough material, draft a report and candidate lessons.
- Seed `person` lessons from the existing account-scoped dossiers. Janet first.

## Phases

| # | Phase | Ships |
|---|---|---|
| 0 | Types + moments | `project_types` and `lesson_moments` lookup tables; `projects.project_types`; a picker in MC-2; types drafted for all 70 projects for Drew to confirm |
| 1 | Lesson store | `lessons` table, RLS, grants, embedding, `mc2_db` helpers, `review_due` (12 months) |
| 2 | Capture | `log_lesson`, `list_lessons`, `confirm_lesson`, `edit_lesson`, `retire_lesson` (hosted + `cxp`); `remember this` trigger in the CLAUDE.md template; meeting-ingest candidate queue |
| 3 | Surfacing | Lookouts block in `get_project_state` / `cxp brief`; moment matching in `/cp-prep`; proposal / preflight / RFP read type and practice lessons; client-facing guardrail and tests; 12-month review prompts |
| 4 | Close + Archive | Close: final ingest → report draft → lesson drafts → freelancer reviews. Archive: close tools; ingest reads `archive_path`; archived projects out of default search; delete `archive_project_assets` |
| 5 | Freelancers | `freelancer_reviews`; agreed hours per contractor per project; planned vs actual tracking; "who should we use" recommendation |
| 6 | Workload lookahead | Per-person 2–4 week load from allocations, commitments, deliverables and meetings; flags in sprint prep |
| 7 | Backfill | 38 projects: ingest archive folders, set types, draft reports and lessons; seed from stakeholder dossiers; reviews for past freelancers |
| 8 | Lessons page | MC-2 page: review queue, filter by client / type / moment, add types and moments |

Build order: phases 0–3 first (usable the same day lessons start being
recorded), then 4 onwards.

## Decisions (2026-11-11)

- **Project types:** Drew's 12, plus Design (added the same day by insert). The list grows.
- **Moments:** the starter list is good, plus booking resources, contractor
  hours, scope change, process order and workload. The list grows.
- **Reviewer:** the project owner, by default.
- **Close vs Archive:** Close runs the report, lessons and freelancer
  reviews. Archive only closes the tools.
- **Archived context:** archived projects are out of default search, and
  searched only on request. Their lessons stay live.
- **Stale lessons:** reviewed every 12 months.

- **Actual hours:** they come from the contractor's invoice, or from
  timesheets for anyone paid through Gusto. Which one depends on how each
  contractor is paid.
- **Practice-wide lessons:** no second reviewer. The project owner confirms
  them, and readers decide whether to act on them.

## Open questions

None yet.

## Tracker

Progress is tracked in the "Learning Tracker" artifact. One row per task;
statuses: todo · doing · done · blocked.
