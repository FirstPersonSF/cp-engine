---
Project: cp-engine — Learning from projects (archive rethink)
Provenance: Version 01 | 2026-11-11
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

`category` (Production / Strategy) stays as it is. It is a finance grouping,
not a learning one.

## Design

### The lesson: one item, tagged

A lesson is one short statement, not a document. Each one carries:

| Field | Meaning |
|---|---|
| `text` | The lesson, 1–3 sentences, phrased as what to do or watch for. |
| `scope` | `person` (a stakeholder) · `client` (a company) · `type` (one or more project types) · `practice` (all our work). |
| `company_id` / `stakeholder` | Set for `client` / `person` scope. |
| `project_types` | Set for `type` scope; optional tags on the others. |
| `when` | The moment it bites, from a short starter vocabulary: kickoff, estimating, hiring freelancers, travel, shoot, client review round, legal review, final delivery, invoicing. Free text is allowed. |
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

MC-2's Archive (and Close) runs in this order:

1. **Final ingest:** pull in the project's remaining Dropbox files and
   meetings *before* the folder moves. Ingest also has to keep working
   against `archive_path` after the move.
2. **Draft the close-out report:** written automatically from the
   `wrap_report` facts, stored as a spine element, with the owner to confirm
   it. This uses the same "drafted by cp" pattern as Exec Summaries.
3. **Draft lessons:** each axis of the report becomes candidate lessons,
   scoped and tagged. The owner confirms, edits or drops them.
4. **Close the tools:** as today (#208/#212).

The project leaves the active views (master-cp, agendas, planning). Its
sources, meetings, spine, report and lessons stay readable and searchable.
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
| 0 | Project types | `projects.project_types text[]` with a check constraint; a picker in MC-2; types drafted for all 70 projects for Drew to confirm |
| 1 | Lesson store | `lessons` table, RLS, grants, embedding, `mc2_db` helpers |
| 2 | Capture | `log_lesson`, `list_lessons`, `confirm_lesson`, `edit_lesson`, `retire_lesson` (hosted + `cxp`); `remember this` trigger in the CLAUDE.md template; meeting-ingest candidate queue |
| 3 | Surfacing | Lookouts block in `get_project_state` / `cxp brief`; `when` matching in `/cp-prep`; proposal / preflight / RFP read type and practice lessons; client-facing guardrail and tests |
| 4 | Archive close | MC-2 close runs final ingest → report draft → lesson drafts → close tools; ingest reads `archive_path`; delete `archive_project_assets` |
| 5 | Backfill | 38 projects: ingest archive folders, set types, draft reports and lessons; seed from stakeholder dossiers |
| 6 | Lessons page | MC-2 page: review queue, filter by client / type / `when` |

Build order: phases 0–3 first (usable the same day lessons start being
recorded), then 4–6.

## Open questions

- **`when` vocabulary:** is the starter list right? It is meant to grow.
- **Reviewer:** the project owner by default, with Drew for `practice`
  lessons. Confirm.
- **Close vs Archive:** should a Closed project also run steps 1–3, or only
  Archive? Proposed: Close runs 1–3, and Archive only closes the tools.
- **Lessons that go stale:** retire by hand only, or prompt a review after
  12 months?

## Tracker

Progress is tracked in the "Learning Tracker" artifact. One row per task;
statuses: todo · doing · done · blocked.
