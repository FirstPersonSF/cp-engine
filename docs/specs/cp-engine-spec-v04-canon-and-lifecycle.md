# cp-engine spec v04 — Canon & element lifecycle

**Status:** draft v01 · 2026-08-03
**Origin:** Drew/Tony brainstorm 2026-08-03 (whiteboard: elements funneling
into a per-project "1P Truth" box; red DELIVERY line with retrospective
loop-back). Companion to #112 (spine self-curation).

## Problem

Cards accrete but never expire, connect, or defer to each other:

1. **No half-life.** Every element has equal standing forever. Research that
   fed a shipped deliverable (e.g. a Perspective & Possibilities report) keeps
   surfacing as if it were current, though it's only relevant in retrospect.
2. **No "current truth" surface.** There is no small, curated set that says
   *this is where the project stands*. Retrieval treats a thin 8-week-old
   client remark and last week's partner decision as peers (ranked by
   similarity + recency only).
3. **No authority ordering.** A remembered stakeholder signal can effectively
   override a live partner directive because nothing ranks who's speaking.

The ibx-5192 audit (57% thin stubs) showed this is a *lifecycle-discipline*
gap, not a storage gap: `spine_relations` (mig 117) already provides the
graph; nothing in the workflow ever demands edges. A graph DB (Neo4j etc.)
was considered and rejected — it would add a third store to sync without
creating the missing lifecycle events.

## Design

Three mechanisms, ordered by cost. All edges reuse `spine_relations`
(element-lineage endpoints, `est_item_id`-keyed). One migration extends the
`kind` CHECK with two values: `canon_of`, `absorbed_by`.

### 1. Authority precedence (behavioral now; schema later)

Precedence, highest first:

1. **Partner directive** — what a partner asks for in the live session
2. **Project canon** (§2)
3. **Delivered artifacts** — shipped deliverables and their live versions
4. **Stakeholder signals** — remembered client statements
5. **Working notes / ephemera** — sprint-file material, day-scale thinking

Rule: **stakeholder memory advises; it never vetoes.** On conflict, execute
the partner's direction and surface the conflict ("Heads up — Janet said X
on 5/27; proceeding as you asked"). Ships as a session-protocol section in
`templates/CLAUDE.md.j2`.

Follow-on (issue): an actor-class field on element provenance
(`partner | client | vendor | inferred`) so `semantic_search` can rank by
authority and phase, not just similarity + recency. `origin`
(`distilled|authored`, mig 074) tracks write-path, not speaker — it cannot
carry this.

### 2. Per-project canon, anchored on the standing brief

The canon is NOT a new container. Anchor = the standing **Inputs & Briefing**
element (`_authored/inputs-briefing`), which is already versioned, live-status,
and source-attached. The brief holds the narrative truth; the canon set holds
the pinned strategic elements linked to it:

- **Membership edge:** `canon_of` (member element → brief element),
  status `active`.
- **Small by construction:** target ≤7 active members. `cp spine-lint` warns
  beyond that (warn-only, like all lint).
- **Promotion is deliberate and displacing:** new hosted verb
  `promote_to_canon(code, item_id, replaces_item_id?)` — writes the
  `canon_of` edge; when `replaces_item_id` is given, also writes
  `supersedes` (new → old) and retires the old member's `canon_of` edge.
  Runs on the hosted server so the write carries user identity.
- **Read side:** `get_project_state` and the `cp brief` context pack list
  canon members under the brief. When retrieval returns an element that
  predates the newest canon change touching its territory, annotate it
  ("predates current positioning — canon delta: …").

Per-project only for now. A firm-level 1P canon that project canons inherit
from is explicitly deferred (decision 2026-08-03).

### 3. Seal-on-delivery (the red line)

A shipped deliverable is a **compression event**: it absorbs the elements it
was synthesized from.

- **Edge:** `absorbed_by` (source element → deliverable element), status
  `active`. Element state stays edge-derived — no new status column, and the
  version vocabulary stays `live | superseded` ONLY (the sap-5171 lesson).
- **Verb:** `seal_to_deliverable(code, deliverable_item_id,
  absorbed_item_ids[])` — batch-writes the edges, journals ONE step
  ("sealed N elements into <deliverable>").
- **Retrieval default: live canon-eligible view.** Elements with an active
  `absorbed_by` edge are excluded from `semantic_search` /
  `list_spine_elements` defaults. `include_absorbed=true` (retrospective
  mode) includes them, each annotated with what absorbed them.
- Absorbed ≠ archived: `archived` hides an element entirely; absorbed keeps
  it one hop behind its deliverable for retrospectives and "why did we
  decide this."
- Ephemera (day-scale thinking) should not become spine elements at all —
  that's sprint-file territory, which already expires by week.

## Rollout

1. mc-2 migration `125_spine_relations_lifecycle_kinds.sql` — extend `kind`
   CHECK with `canon_of`, `absorbed_by`.
2. Authority-rule section in `CLAUDE.md.j2` + release (this ships first; no
   schema dependency).
3. Hosted verbs `promote_to_canon`, `seal_to_deliverable`; read-side filters
   in `semantic_search` / `list_spine_elements` / `get_project_state`.
4. Lint: canon >7 members; absorbed element still bound `serves` an open
   work item.
5. Pilot: seed one project's canon (ibx-5153 or ibx-5192) and run a
   retroactive seal on a project whose P&P report already shipped — the
   honest test of whether supersession finally "feels right."

## Open questions

- Actor-class backfill: derivable from provenance/meeting attendees, or
  hand-tagged during the pilot?
- Does sealing propose itself? (Auto-ingest could detect "deliverable
  shipped" and propose a seal set for review, mirroring the relations
  proposal inbox.)
- Firm-level 1P canon: revisit after the per-project pilot.
