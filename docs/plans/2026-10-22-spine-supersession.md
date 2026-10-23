---
Project: cp-engine
Provenance: Version 01 | 2026-10-22
Filename: 2026-10-22-spine-supersession.md
Author: Claude
---

# Spine supersession: the "this was replaced" signal, and sources per version (#352 items 1 and 3)

**Status:** item 1 read side BUILT on `w3/352-supersession`; (c) item 3 (`replaces_key` on the create verbs) and (d) Option A (sources per version) BUILT on the same branch — (d) needs mc-2 migration `202_spine_source_live_only.sql` (branch `w3/352-sources`; applied to MC-2 2026-10-22) before the hosted server ships. (c) items 1, 2 and 4 remain design only. Item 2 (Feedback layer) is being built separately in `cp-engine-w3f`.

## Test cases

- **ibx-5153, 08-26.** A fresh agent drafted against `Narrative Messaging Architecture` (July, v1), which Janet's 08-24 redirect had withdrawn.
- **ibx-5153, 10-19.** The weekly drafter rewrote Status from the 10-16 Janet call (the reset, $49.5K). That made the same-day talking-points v02 and branch-2 assessment v2 partly obsolete, and nothing pointed to them.

## (a) Signals that already exist (live MC-2, read-only, 2026-10-22)

| Signal | What it says | Live count | Surfaced at list/pull before this branch? |
|---|---|---|---|
| `supersedes` edge (new → old) | X replaces Y | **5 active**, all with a live source and a live target. They hit **4 distinct elements** (one has two successors) | **No.** Only `list_spine_relations` when called explicitly, plus `spine_lint`'s stale-canon check |
| `contradicts` edge | X challenges Y's claim | **6 active**, all live. In all 6, `from` is the newer challenger | No |
| `promote_to_canon(replaces_key=)` | writes `supersedes` and drops the old member's canon edge | 21 `canon_of` edges | canon yes, the displacement no |
| `absorbed_by` (seal) | Y was compressed into deliverable D | 25 active | yes: hidden by default, `absorbed_by` |
| version rows (`spine_substance.status`) | v2 replaced v1 **of the same element** | — | yes (only the live row is read) |

So versions and seals handle replacement inside one element and compression into a deliverable. Neither handles replacement across elements: a *different* element overtaking this one. The `supersedes` edge does, but no reader showed it. In 2 of the 5 cases the successor is itself sealed, so it is hidden from the default listing.

**Neither test case has an edge today.** Narrative Messaging Architecture has no incoming `supersedes`. Talking-points v02 and branch-2 v2 have none either. A read-side signal alone would not have caught either case, so (c) is the half that closes them.

## (b) Read-side signal: what was built, and the heuristic that was not

**Built:** `superseded_by: {key, framing, date}` on `list_spine_elements` rows (hosted, and the engine's `list_spine` when lifecycle edges are read). It is set for an element with an incoming **active** `supersedes` edge whose source is a **live** element. `date` is when the edge was recorded. When several successors exist, the listing shows the newest, and `superseded_by_all` on pull names them all. When the successor is sealed, the pointer also carries `absorbed_by`, so it leads somewhere readable even under the default `include_absorbed=false`. `pull_spine_element` / `pull_spine` lead with `warning: "Superseded by '<framing>' (<key>) on <date> — read that first."`. Active `contradicts` edges add `contradicted_by: [...]` to the target (pull adds each edge's `note`). Listings count superseded rows as `superseded_count` (hosted) or `superseded` in the trailing note row (engine).

**It is a hint, not a hard flag.** Nothing is hidden or refused. A superseded element is often still the record of what was said; it is just not current truth. This follows "stakeholder memory advises, never vetoes" and the absorbed/archived precedent (annotate, never filter). The same edge read feeds it as `absorbed_by`/`canon` (`seal_sweep.read_lifecycle_edges`, now with an optional `challenges` out-list), so it adds no extra query to a listing and has one failure mode. A failed read says so: `annotations_error` on lists, `supersession_error` on pull.

**Heuristic: "newer live element in the same layer, bound to the same work item." Measured, NOT built.**

| Measure | Value |
|---|---|
| live elements / bound (`serves` non-empty) | 443 / 178 |
| bound elements the heuristic would flag | **75 (42%)**, 146 pairs |
| pairs in `SourceMaterial` / `Research` (siblings feeding one item, not replacements) | 113 of 146 (≈0% precision) |
| other layers: hand-judged plausible replacements | ~7 of 33 (≈20%): feedback rounds r2→r4→final, a v1→v2 comment deck, one Activity, branch-2 → Janet 10-16 call |
| recall on the 5 real `supersedes` edges | **0 / 5**. 3 cross layers (ClientFeedback→SourceMaterial, ClientFeedback→Activity), 2 have an unbound successor |
| test case 1 (NMA) | **missed.** NMA is the *newer* element in its pairs |

At about 5% precision overall and zero recall, it would mark 42% of bound elements and teach readers to ignore the marker. Do not build it. The one place it is close to right is successive feedback rounds on a deliverable. That case is better served by (c) than by a guess.

## (c) Write side: who proposes `supersedes` / `contradicts`

Both write as **`proposed`** edges (the `informs`-on-route precedent, #174: 19 proposed today). A human confirms them in the Suggestions inbox. A drafter's edge is never active. Proposals:

1. **The weekly Exec Summary drafter** (10-19 case). When it rewrites Status, it reads the live elements bound to the work the Status names (same project, versions in the last ~21 days, layers Synthesis/Deliverables/Decisions/Output). It names each one the new Status contradicts and proposes `contradicts` (new state element → old), with a one-line note quoting the clashing claim. This needs an element to stand for "the new Status". Use the meeting-ingest element for the call the Status came from (the 10-16 Janet call), not the Exec Summary itself, because the summary is not a spine element.
2. **A partner directive** (08-26 case). When a partner-actor or `ClientFeedback` element is captured with redirect language ("drop", "instead", "no longer", "withdraw"), the capture asks which live elements it withdraws and proposes `supersedes`. The partner names them, so the agent does not guess. Under authority precedence a partner directive outranks canon, so a confirmed edge here may also run `promote_to_canon(replaces_key=)`.
3. **`add_spine_document` / `create_spine_element` with `replaces_key`** (cheap, explicit). This is the same argument `promote_to_canon` already takes. It writes an **active** edge, because the author asserted it.
4. **`spine_lint`** reports a live element bound to a work item whose deliverable shipped a newer version, as a *question* ("still current?"). It is not an edge.

## (d) Item 3: sources per version. Recommendation: per-version source, no schema change

Today `add_element_source` writes the link onto **every** version row. Attaching a new input to a deliverable whose current version shipped earlier therefore claims the shipped version drew on it.

- **Option A, per-version (recommended).** `spine_substance.sources` is already a per-row jsonb array, and `add_spine_version` already copies the live row's `sources` forward. So the fix is behavioral: `add_element_source` writes to the **live row only**, and superseded rows stay as they were (history becomes truthful). For the "arrived after this version shipped" case, the typed link gains a jsonb field, `{"type":"rag_asset", "id", "title", "for_next": true}`. The verb sets it when the live version is a shipped deliverable (seal-sweep's "shipped" test), or when the caller passes `for_next_version=true`. `add_spine_version` clears the flag on the copy, so the input becomes a normal source of the version it actually fed. **Schema:** none (jsonb field). **Code:** the `_modify_element_sources` row filter, a flag-clear in `add_spine_version`, and readers (`pull`, `seal_sweep`, `spine_lint`) that treat `for_next` as "not yet consumed". `remove_element_source` keeps its every-version behavior (a wrong attach is wrong everywhere).
- **Option B, an "input to next version" edge.** A new relation kind, or `informs` from a source stub to the element, tagged `next`. This needs an edge-kind CHECK migration plus grants (the #127/#160 lesson). It also models a rag_asset as a spine endpoint, which the stub layer only partly does today. Not recommended. It duplicates what the per-row array already expresses.

## (e) Built vs deferred

- **Built** (`w3/352-supersession`): read-side `superseded_by` / `superseded_by_all` / `contradicted_by` / `warning` on hosted `list_spine_elements` + `pull_spine_element` and on the engine's `list_spine` + `pull_spine`; a shared pure `seal_sweep.challenge_marks` and `supersession_warning`; cp-tools docs. No schema change, no new tool, no MC-2 writes. Control tests: `tests/test_spine_supersession_352.py`, `prototypes/hosted-mcp/test_supersession_352.py`.
- **Built, second pass** (`w3/352-supersession`): (c) 3 — `replaces_key` on hosted `create_spine_element` / `add_spine_document`, resolved before the insert, edge via `_insert_lifecycle_edge` (no engine create verb exists to mirror). (d) Option A — `add_element_source` live row only + `for_next` (hosted via `p_live_only`; engine `modify_element_sources` directly), `add_spine_version` clears it. The auto default is `seal_sweep.version_shipped`: the live version is a deliverable AND an active `absorbed_by` edge into it was recorded on/after its `version_date` (it was sealed, so it shipped). The looser "deliverable with a version label" (`is_shipped_deliverable`, still what `build_rounds` uses) flagged nearly every attach to an in-progress v1, so it is not used for the default. Pull adds `sources_for_next_version`, `seal_sweep` rounds split `sources` / `sources_for_next_version`, `spine_lint` counts only consumed sources. `add_element_provenance` keeps the every-version write. Control tests: `tests/test_sources_for_next_352.py`, `prototypes/hosted-mcp/test_supersession_write_352.py`.
- **Deferred:** the heuristic (measured, rejected); (c) 1, 2 and 4; a mc-2 UI badge; backfilling edges for the two ibx-5153 test cases (a human call, and this branch writes nothing).
