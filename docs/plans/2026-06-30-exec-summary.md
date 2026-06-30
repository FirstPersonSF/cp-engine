# Sprint-Planning Rethink: Exec Summary Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the stale, one-line `quick-resume` region in each project `cp.md` with a structured, model-authored `exec-summary` region, and rewire `cp prep-planning` to emit a bundle of those summaries for in-session synthesis — so sprint planning reads fresh, consistent, full-fidelity project state instead of scraping a stale `**Current work:**` line.

**Architecture:** The engine owns *scaffold + read + bundle* (region markers, migration cutover, the prep-planning read-path, the deterministic metrics); the **model** owns *all prose* (authoring the Exec Summary at wrap-up by editing between the markers, and synthesizing `_planning.md` in-session from the bundle). Auto-ingest stops writing project `cp.md` state entirely — per-meeting truth already lands in the sprint file. The cutover is automatic: `cp sync` converts each project's `quick-resume` region to `exec-summary` in place, seeding the new region from the old content plus a dated "migrated" Update.

**Tech Stack:** Python 3.14, Click CLI, Jinja2 templates, pytest (`dev` extra), `uv` for env management. Region splicing via `render.splice_managed_region`. The cp tenant skill side is markdown in `plugin/commands/`.

**Resolved design decisions (from brainstorm, 2026-06-30):**
- **Write path:** model edits markers directly (Edit tool); engine never authors Exec Summary prose.
- **Auto-ingest:** drops its `cp.md` state write entirely; Exec Summary is model-only.
- **Migration:** `cp sync` auto-converts `quick-resume` → `exec-summary` in place, seeding from old content + a dated "migrated" Update.
- **Roll-up:** `cp prep-planning` emits a structured bundle (full Exec Summary per project + existing metrics); model synthesizes + writes `_planning.md` in-session.

---

## Region shape (the contract every task builds toward)

```markdown
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** 2026-06-30
**Objective:** <one line — what this project delivers>
**Status:** <current state in a phrase>

**Where it stands:**
- <2-4 dense bullets of current reality>

**Next up:**
- <concrete near-term moves>

**Blockers:**
- <what's stuck / needed, with who>

**Updates:**
- 2026-06-30 — <what changed, dated>
<!-- cp-engine:end exec-summary -->
```

Notes that constrain the build:
- `**Last session:**` stays inside the region (it lived inside `quick-resume`; `capture_session.py` rewrites it). Keep it as the first field so the existing `Last session` regex still matches.
- The `· updated <date>` header is part of the `## Exec Summary` heading line — the model stamps it; the engine only seeds it at migration.
- Markers are `<!-- cp-engine:start exec-summary -->` / `<!-- cp-engine:end exec-summary -->`.

---

## Pre-flight (do once before Task 1)

All commands run from the worktree:
`cd /Users/drewf/Documents/Python/cp-engine/.worktrees/exec-summary`
Run tests with: `uv run pytest ...`

**Step 0.1:** Confirm baseline green for the modules in scope.
Run: `uv run pytest tests/test_prep_planning.py tests/test_agenda.py tests/test_ingest.py tests/test_sync.py tests/test_summary.py -q`
Expected: all pass (this is the regression floor; re-run the full relevant set after each task).

---

## Task 1: Exec Summary region constants + a shared region-name module

**Why first:** every other task references the marker strings. Today the same two marker literals are copy-duplicated in `sync.py` and `ingest.py` (a known drift hazard — see the item-1 review finding about parallel literals). Define them once.

**Files:**
- Modify: `src/cp_engine/render.py` (add public constants near `splice_managed_region`, ~line 966)
- Test: `tests/test_render.py`

**Step 1.1: Write the failing test**

```python
# tests/test_render.py
from cp_engine.render import (
    EXEC_SUMMARY_REGION,
    EXEC_SUMMARY_START,
    EXEC_SUMMARY_END,
)

def test_exec_summary_marker_constants():
    assert EXEC_SUMMARY_REGION == "exec-summary"
    assert EXEC_SUMMARY_START == "<!-- cp-engine:start exec-summary -->"
    assert EXEC_SUMMARY_END == "<!-- cp-engine:end exec-summary -->"
```

**Step 1.2: Run test to verify it fails**

Run: `uv run pytest tests/test_render.py::test_exec_summary_marker_constants -v`
Expected: FAIL with ImportError.

**Step 1.3: Add the constants**

In `src/cp_engine/render.py`, just above `def splice_managed_region`:

```python
EXEC_SUMMARY_REGION = "exec-summary"
EXEC_SUMMARY_START = f"<!-- cp-engine:start {EXEC_SUMMARY_REGION} -->"
EXEC_SUMMARY_END = f"<!-- cp-engine:end {EXEC_SUMMARY_REGION} -->"
```

**Step 1.4: Run test to verify it passes**

Run: `uv run pytest tests/test_render.py::test_exec_summary_marker_constants -v`
Expected: PASS.

**Step 1.5: Commit**

```bash
git add src/cp_engine/render.py tests/test_render.py
git commit -m "feat(exec-summary): add exec-summary region marker constants"
```

---

## Task 2: New-project template scaffolds the Exec Summary region

**Files:**
- Modify: `src/cp_engine/templates/project-cp.md.j2:102-109`
- Test: `tests/test_render.py` (or wherever template rendering is tested — search `project-cp.md.j2` in tests first)

**Step 2.1: Locate the template test.**
Run: `grep -rln "project-cp.md.j2\|render_project_cp\|Quick Resume" tests/`
If a render test exists, extend it; otherwise add one that renders the project template and asserts on the region.

**Step 2.2: Write the failing test**

```python
def test_project_template_scaffolds_exec_summary_region():
    body = render_project_cp(...)  # use the existing render entrypoint the other tests use
    assert "<!-- cp-engine:start exec-summary -->" in body
    assert "## Exec Summary" in body
    assert "**Objective:**" in body
    assert "**Where it stands:**" in body
    assert "**Updates:**" in body
    assert "<!-- cp-engine:start quick-resume -->" not in body
```

**Step 2.3: Run test to verify it fails**

Expected: FAIL (template still emits quick-resume).

**Step 2.4: Replace the template block**

Replace lines 102-109 of `src/cp_engine/templates/project-cp.md.j2`:

```jinja
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated {{ today }}

**Last session:** _<date>_
**Objective:** _<one line — what this project delivers>_
**Status:** _<current state in a phrase>_

**Where it stands:**
- _<2-4 dense bullets of current reality>_

**Next up:**
- _<concrete near-term moves, dated where possible>_

**Blockers:**
- _<what's stuck / needed, with who — or "None">_

**Updates:**
- _<dated — first wrap up authors this>_
<!-- cp-engine:end exec-summary -->
```

Check whether the template has a `today` variable in scope (grep the template + its render call). If not, either pass it through or drop the `· updated {{ today }}` suffix from the scaffold (the model stamps it on first wrap-up anyway). Prefer passing `today` if it's a one-line addition to the render context; otherwise omit the suffix and leave just `## Exec Summary`.

**Step 2.5: Run test to verify it passes; run full template test file.**

Run: `uv run pytest tests/test_render.py -q`
Expected: PASS.

**Step 2.6: Commit**

```bash
git add src/cp_engine/templates/project-cp.md.j2 tests/test_render.py
git commit -m "feat(exec-summary): scaffold exec-summary region in new project cp.md"
```

---

## Task 3: Migration — `cp sync` converts `quick-resume` → `exec-summary` in place

**This is the riskiest task.** It rewrites existing project files. It must be: idempotent, content-preserving, and a no-op once converted.

**Files:**
- Modify: `src/cp_engine/sync.py` — rename/replace `_ensure_quick_resume_markers` (line 1238) with `_migrate_quick_resume_to_exec_summary`; update its call site at `sync.py:360`.
- Test: `tests/test_sync.py`

**Behavior spec:**
1. Body already has `<!-- cp-engine:start exec-summary -->` → return unchanged (idempotent).
2. Body has the `quick-resume` region (markers present) → convert in place:
   - Extract the old field values: `**Last session:**`, `**Current work:**`, `**Next up:**`, `**Blockers:**` (any may be the `_<placeholder>_` form).
   - Build the new region body:
     - `## Exec Summary  ·  updated <today>`
     - `**Last session:**` — carried verbatim.
     - `**Objective:**` / `**Status:**` — seed as placeholder `_<...>_` (no source in old region; model fills at first wrap-up).
     - `**Where it stands:**` — seed with the old `Current work` value as a single bullet (drop if placeholder).
     - `**Next up:**` — seed with old `Next up` value as a bullet (drop if placeholder).
     - `**Blockers:**` — seed with old `Blockers` value as a bullet (drop if placeholder).
     - `**Updates:**` — one line: `- <today> — migrated from Quick Resume`.
   - Replace the whole region (markers included) with the new `exec-summary` markers + body.
3. Body has a *pre-marker, hand-written* `## Quick Resume` (no markers — the pre-v0.11 shape `_ensure_quick_resume_markers` handled) → wrap-and-convert in the same pass: treat its content as the seed.
4. Body has neither → return unchanged (defensive).

**Step 3.1: Write the failing tests** (write all cases up front; they share a fixture)

```python
# tests/test_sync.py
from cp_engine.sync import _migrate_quick_resume_to_exec_summary

TODAY = "2026-06-30"

QR_FILLED = """---
Project: ggl-5168
---

<!-- cp-engine:start quick-resume -->
## Quick Resume

**Last session:** 2026-05-21
**Current work:** Finalizing the activation playbook draft
**Next up:** Send WAVs to Drew & Marcello
**Blockers:** Waiting on Shane's mix
<!-- cp-engine:end quick-resume -->

## Project Notes
existing notes here
"""

def test_migrate_filled_region_seeds_fields_and_preserves_outside():
    out = _migrate_quick_resume_to_exec_summary(QR_FILLED, today=TODAY)
    assert "<!-- cp-engine:start exec-summary -->" in out
    assert "<!-- cp-engine:start quick-resume -->" not in out
    assert "## Exec Summary  ·  updated 2026-06-30" in out
    assert "**Last session:** 2026-05-21" in out          # carried verbatim
    assert "Finalizing the activation playbook draft" in out  # → Where it stands
    assert "Send WAVs to Drew & Marcello" in out           # → Next up
    assert "Waiting on Shane's mix" in out                 # → Blockers
    assert "- 2026-06-30 — migrated from Quick Resume" in out
    assert "## Project Notes" in out                       # outside preserved
    assert "existing notes here" in out

def test_migrate_is_idempotent():
    once = _migrate_quick_resume_to_exec_summary(QR_FILLED, today=TODAY)
    twice = _migrate_quick_resume_to_exec_summary(once, today="2026-07-07")
    assert once == twice                                   # second run is a no-op

def test_migrate_drops_placeholder_seeds():
    qr_empty = QR_FILLED.replace(
        "**Current work:** Finalizing the activation playbook draft",
        "**Current work:** _<what's in flight right now>_",
    )
    out = _migrate_quick_resume_to_exec_summary(qr_empty, today=TODAY)
    # placeholder must NOT become a literal bullet
    assert "_<what's in flight right now>_" not in out
    assert "**Where it stands:**" in out                   # field still present

def test_migrate_no_region_is_noop():
    body = "---\nProject: x\n---\n\n## Project Notes\nhi\n"
    assert _migrate_quick_resume_to_exec_summary(body, today=TODAY) == body
```

**Step 3.2: Run to verify they fail.**
Run: `uv run pytest tests/test_sync.py -k migrate -v`
Expected: FAIL (function not defined).

**Step 3.3: Implement `_migrate_quick_resume_to_exec_summary`.**

Model it on the existing `_ensure_quick_resume_markers` structure (sync.py:1238) for the no-marker wrap case, and on `render.splice_managed_region` semantics for the marker case. Pull old values with the same per-label regex shape `ingest.py` uses (`rf"^{re.escape(label)}\s*(.*)$"`, MULTILINE). Treat a value containing `_<` as a placeholder → omit that seed bullet. Use the `EXEC_SUMMARY_START/END` constants from Task 1.

Pass `today` as a parameter (caller supplies it — keeps the function pure/testable; do NOT call `date.today()` inside).

**Step 3.4: Run to verify they pass.**
Run: `uv run pytest tests/test_sync.py -k migrate -v`
Expected: PASS.

**Step 3.5: Wire the call site.**
At `sync.py:360`, replace the `_ensure_quick_resume_markers(...)` call with `_migrate_quick_resume_to_exec_summary(..., today=<the today value sync already has>)`. Confirm sync has a `today`/date in scope at that point; if it threads `today` elsewhere, reuse it.

**Step 3.6: Run the full sync test file.**
Run: `uv run pytest tests/test_sync.py -q`
Expected: PASS. Fix any test that asserted on the old `quick-resume` cutover behavior (update it to the new region — that's expected churn, not a regression).

**Step 3.7: Commit**

```bash
git add src/cp_engine/sync.py tests/test_sync.py
git commit -m "feat(exec-summary): migrate quick-resume region to exec-summary on sync"
```

---

## Task 4: Stop auto-ingest from writing project cp.md state

Per the resolved design (Option A): auto-ingest no longer writes `Current work / Next up / Blockers` into `cp.md`. Per-meeting truth still flows into the sprint file (unchanged).

**Files:**
- Modify: `src/cp_engine/ingest.py` — remove the `_QUICK_RESUME_VERBS` write branch (call sites at `ingest.py:349` and `ingest.py:506`), and delete the now-dead `_write_quick_resume_verb` (line 1087) + `_QUICK_RESUME_VERB_TO_LABEL` / `_QUICK_RESUME_VERBS` constants (lines 235-242) **only if nothing else references them** (grep first).
- Modify: `src/cp_engine/plan_from_transcript.py` — the LLM prompt (~line 357) that instructs the model to emit `current_work/next_up/blockers` verbs for the project cp.md. Remove those verb instructions so the model stops producing writes with no destination. **Leave the sprint-file verbs intact.**
- Test: `tests/test_ingest.py`, `tests/test_plan_from_transcript.py`

**Step 4.1: Grep for all references first.**
Run: `grep -rn "_QUICK_RESUME_VERBS\|_write_quick_resume_verb\|_QUICK_RESUME_VERB_TO_LABEL\|current_work\|next_up\|blockers" src/ tests/`
Map every consumer before deleting. The `plan_from_transcript` prompt + the schema/allowed-verbs list are the producers; `ingest` is the consumer.

**Step 4.2: Write the failing test** — assert auto-ingest no longer touches cp.md state.

```python
def test_auto_ingest_does_not_write_cp_md_state(tmp_path, ...):
    # Given a project cp.md with an exec-summary region and a parsed plan that
    # (under old behavior) carried current_work/next_up/blockers verbs,
    # when ingest runs, the cp.md exec-summary region is unchanged.
    ...
    assert cp_md_after == cp_md_before
```

Also add/adjust a `test_plan_from_transcript` assertion that the produced verb set for a project no longer includes `current_work/next_up/blockers` (or that the prompt no longer lists them — match the existing test style there).

**Step 4.3: Run to verify it fails.**
Expected: FAIL (current code still writes).

**Step 4.4: Remove the write branch + dead code + prompt verbs.**
- Delete the `if normalized in _QUICK_RESUME_VERBS:` branches that call `_write_quick_resume_verb`.
- Delete `_write_quick_resume_verb`, `_QUICK_RESUME_VERB_TO_LABEL`, `_QUICK_RESUME_VERBS`, `_QUICK_RESUME_REGION_START/END` **if** the Task 4.1 grep shows no remaining consumers.
- Trim the `plan_from_transcript` prompt's project-state verb instructions.

**Step 4.5: Run to verify it passes; run both test files.**
Run: `uv run pytest tests/test_ingest.py tests/test_plan_from_transcript.py -q`
Expected: PASS. Update any test that asserted the old write happened (expected churn).

**Step 4.6: Commit**

```bash
git add src/cp_engine/ingest.py src/cp_engine/plan_from_transcript.py tests/
git commit -m "feat(exec-summary): auto-ingest no longer writes project cp.md state (sprint file only)"
```

---

## Task 5: Read-path — `prep_planning` reads the full Exec Summary region

Replace the one-line `_extract_current_work` (prep_planning.py:500) with a full-region extractor, and carry the structured summary on `ProjectPlanningBlock`.

**Files:**
- Modify: `src/cp_engine/prep_planning.py` — add `_extract_exec_summary` (replaces/augments `_extract_current_work`); add `exec_summary: str | None` field to `ProjectPlanningBlock` (dataclass ~line 117); populate it in `build_project_block` (~line 829).
- Test: `tests/test_prep_planning.py`

**Step 5.1: Write the failing test**

```python
def test_extract_exec_summary_returns_full_region():
    from cp_engine.prep_planning import _extract_exec_summary
    body = (
        "<!-- cp-engine:start exec-summary -->\n"
        "## Exec Summary  ·  updated 2026-06-30\n\n"
        "**Objective:** Ship the activation playbook\n"
        "**Status:** In review\n\n"
        "**Where it stands:**\n- Draft complete\n\n"
        "**Next up:**\n- Send WAVs\n\n"
        "**Blockers:**\n- Waiting on Shane\n\n"
        "**Updates:**\n- 2026-06-30 — draft done\n"
        "<!-- cp-engine:end exec-summary -->\n"
    )
    out = _extract_exec_summary(body)
    assert "Objective:" in out and "Ship the activation playbook" in out
    assert "Where it stands:" in out and "Blockers:" in out
    assert "<!-- cp-engine" not in out          # markers stripped
    assert out.strip().startswith("## Exec Summary") or out.strip().startswith("**Objective")

def test_extract_exec_summary_missing_returns_none():
    from cp_engine.prep_planning import _extract_exec_summary
    assert _extract_exec_summary("## Project Notes\nhi\n") is None

def test_extract_exec_summary_all_placeholder_returns_none():
    # a freshly-scaffolded, never-authored region is "no real content"
    from cp_engine.prep_planning import _extract_exec_summary
    body = (
        "<!-- cp-engine:start exec-summary -->\n## Exec Summary\n"
        "**Objective:** _<one line>_\n**Status:** _<phrase>_\n"
        "<!-- cp-engine:end exec-summary -->\n"
    )
    assert _extract_exec_summary(body) is None
```

**Step 5.2: Run to verify it fails.** Expected: FAIL.

**Step 5.3: Implement `_extract_exec_summary`.**
Extract the body between `EXEC_SUMMARY_START`/`END`, strip the marker lines, return the inner text. Return `None` if markers absent OR if the region is entirely placeholders (every non-empty field value matches `_<...>_` and there are no real bullets). Keep the existing `_<` placeholder test (`prep_planning.py:507`).

**Step 5.4: Run to verify it passes.** Expected: PASS.

**Step 5.5: Thread it through the block.**
- Add `exec_summary: str | None = None` to `ProjectPlanningBlock`.
- In `build_project_block` (~line 829), replace the `_extract_current_work(...)` call with `_extract_exec_summary(...)`, storing to `exec_summary`. Keep `quick_resume_line` temporarily ONLY if other code still reads it; otherwise rename cleanly. (Grep `quick_resume_line` usages first — `_render_project_block` at line 1379 reads it; that's handled in Task 6.)

**Step 5.6: Run the prep_planning test file.**
Run: `uv run pytest tests/test_prep_planning.py tests/test_prep_planning_cross_cutting.py -q`
Expected: PASS (some assertions will need updating to the new field — expected churn).

**Step 5.7: Commit**

```bash
git add src/cp_engine/prep_planning.py tests/test_prep_planning.py
git commit -m "feat(exec-summary): prep-planning reads full exec-summary region per project"
```

---

## Task 6: Bundle output — `cp prep-planning --bundle` emits structured per-project summaries + metrics

The roll-up changes from "engine renders the final 426-line doc" to "engine emits a bundle; model synthesizes in-session." Add a `--bundle` mode that prints (or writes) the structured input the model needs. The existing human-readable `_render_project_block` stays for backward-compat / non-model use, now reading `exec_summary` instead of the one-line `quick_resume_line`.

**Files:**
- Modify: `src/cp_engine/prep_planning.py` — `_render_project_block` (line 1356/1379) reads `block.exec_summary`; add a `render_planning_bundle(blocks, metrics, today) -> str` that emits, per project: code+name, the full Exec Summary, urgent flags, milestones/commitments, and the tenant-level metrics (capacity binding, hours, cross-cutting decisions).
- Modify: `src/cp_engine/cli.py` — `prep_planning_cmd` (line 2397): add `--bundle` flag that calls `render_planning_bundle` instead of `render_planning_doc`.
- Test: `tests/test_prep_planning.py`

**Step 6.1: Write the failing test for the bundle renderer.**

```python
def test_render_planning_bundle_includes_each_exec_summary_and_metrics():
    blocks = [make_block(code="ggl-5168", exec_summary="## Exec Summary\n**Objective:** X\n**Blockers:**\n- Waiting on Shane"),
              make_block(code="ibx-5167", exec_summary="## Exec Summary\n**Objective:** Y\n**Blockers:**\n- None")]
    out = render_planning_bundle(blocks, metrics=make_metrics(), today="2026-06-30")
    assert "ggl-5168" in out and "ibx-5167" in out
    assert "Waiting on Shane" in out          # full summary, not one line
    assert "**Objective:** X" in out
    # deterministic metrics still present
    assert "capacity" in out.lower() or "Brandon" in out   # whatever metrics render
```

**Step 6.2: Run to verify it fails.** Expected: FAIL.

**Step 6.3: Implement `render_planning_bundle`** — assemble per-project sections (code+name header, the verbatim `exec_summary`, urgent flags, forward calendar, commitments) + a tenant metrics header reusing the existing metric computations. Update `_render_project_block` to print `block.exec_summary` (multi-line) where it printed `**Where:** {quick_resume_line}`.

**Step 6.4: Run to verify it passes.** Expected: PASS.

**Step 6.5: Add the `--bundle` CLI flag.**
In `cli.py:prep_planning_cmd`, add `@click.option("--bundle", is_flag=True, ...)`. When set, call `render_planning_bundle(...)` and print to stdout (the skill captures it). Keep existing behavior when unset.

**Step 6.6: Run the full prep_planning suite + a manual smoke.**
Run: `uv run pytest tests/test_prep_planning.py tests/test_prep_planning_cross_cutting.py -q`
Expected: PASS.

**Step 6.7: Commit**

```bash
git add src/cp_engine/prep_planning.py src/cp_engine/cli.py tests/test_prep_planning.py
git commit -m "feat(exec-summary): cp prep-planning --bundle emits per-project summaries + metrics"
```

---

## Task 7: Update the other two region consumers (`summary.py`, `agenda.py`)

These read the old region and will silently return nothing post-migration if untouched.

**Files:**
- Modify: `src/cp_engine/summary.py` — `_extract_current_work_first_paragraph` (line 112) + the `## Quick Resume` "Current work:" reader (lines 53, 83-87) feed the master-cp rollup line. Point them at the Exec Summary region: prefer the `**Status:**` line (a phrase) or the first `Where it stands` bullet as the one-line master-cp summary.
- Modify: `src/cp_engine/agenda.py` — `extract_quick_resume` (lines 197-207) feeds `quick_resume_excerpt` into the weekly-cp agenda. Point it at the Exec Summary region (return the `Where it stands` block or the whole region).
- Test: `tests/test_summary.py`, `tests/test_agenda.py`

**Step 7.1: Grep + read each consumer to choose the replacement line precisely.**
Run: `grep -rn "Quick Resume\|Current work\|quick_resume_excerpt\|first_paragraph" src/cp_engine/summary.py src/cp_engine/agenda.py`

**Step 7.2: Write failing tests** — each asserts the consumer now extracts from an `exec-summary` region (filled) and returns `None`/placeholder-safe on an unauthored one. Mirror the existing test shapes in `test_summary.py` / `test_agenda.py`.

**Step 7.3: Run to verify they fail.**

**Step 7.4: Implement** — swap the region heading/markers each looks for to the Exec Summary region; pick the single most useful line for the master-cp summary (`Status:` recommended — it's the designed one-phrase field).

**Step 7.5: Run both test files.**
Run: `uv run pytest tests/test_summary.py tests/test_agenda.py -q`
Expected: PASS.

**Step 7.6: Commit**

```bash
git add src/cp_engine/summary.py src/cp_engine/agenda.py tests/test_summary.py tests/test_agenda.py
git commit -m "feat(exec-summary): point master-cp + weekly-cp readers at exec-summary region"
```

---

## Task 8: Wrap-up + prep skill wiring (cp-engine plugin commands)

The model-authored half lives in markdown skills, shipped in `plugin/commands/`. Two surfaces:
1. **Wrap-up authoring instructions** — tell the model, at `wrap up`, to author/merge the Exec Summary for each touched project by editing between the `exec-summary` markers: read prior region + this session's sprint-file/spine/meeting changes, rewrite the six fields, append ONE dated Update, roll off Updates older than ~4 weeks, stamp `· updated <today>`.
2. **`cp-prep` skill** — call `cp prep-planning --bundle`, hand the bundle to the model, have it synthesize the Focus list / decisions+blockers / cross-cutting patterns / per-owner commitments and write `_planning.md` in-session.

**Files:**
- Modify: `plugin/commands/cp-prep.md` — replace "engine writes the doc" framing with "engine emits `--bundle`; you synthesize + write `_planning.md`."
- Find + modify the wrap-up skill. Run: `grep -rln "wrap up\|wrap-up\|Exec Summary\|Quick Resume" plugin/commands/ plugin/skills/ 2>/dev/null`. The cp tenant `CLAUDE.md` (generated from `templates/CLAUDE.md.j2`) documents `wrap up`; the authoring instructions belong in the skill that the tenant uses. If wrap-up authoring is documented in `templates/CLAUDE.md.j2`, update there (it regenerates the tenant `CLAUDE.md`).
- Modify: `src/cp_engine/templates/CLAUDE.md.j2` — update the "Quick Resume engine-managed" / wrap-up sections to describe the Exec Summary region + the model-authors-at-wrap-up contract.

**Step 8.1:** Map the exact skill/template files (the grep above). These are prose, not unit-tested — review by reading.

**Step 8.2:** Write the wrap-up authoring instructions (the read-last + merge-this + append-dated-update + rolloff procedure, verbatim from design Part 2). Be explicit that the model edits between the markers with the Edit tool and that the engine does NOT author this.

**Step 8.3:** Rewrite `cp-prep.md` for the `--bundle` → in-session-synthesis flow (design Part 3: Focus list, decisions & blockers, cross-cutting patterns, per-owner commitments; generated in-session so it's interrogable live).

**Step 8.4:** Update `templates/CLAUDE.md.j2` region docs.

**Step 8.5: Commit**

```bash
git add plugin/commands/ src/cp_engine/templates/CLAUDE.md.j2
git commit -m "docs(exec-summary): wrap-up authoring + prep-planning bundle synthesis instructions"
```

---

## Task 9: Full suite + changelog + version bump

**Step 9.1:** Run the entire test suite.
Run: `uv run pytest -q`
Expected: all pass. The summary said the suite is ~1600 tests; investigate any failure (likely a stale assertion on the old region — update it, don't paper over a real break).

**Step 9.2:** Use `superpowers:requesting-code-review` on the full diff before finishing.

**Step 9.3:** Update `CHANGELOG.md` / release notes describing the Exec Summary cutover, the migration-on-sync, the auto-ingest behavior change, and `--bundle`.

**Step 9.4:** Do NOT hand-bump the version. The release is cut via `scripts/release.py` at integration time (per the project's release discipline — hand-bumping leaves `plugin.json` stuck and downgrades the CLI on SessionStart). Note in the PR that this needs a `scripts/release.py` minor bump.

**Step 9.5: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(exec-summary): changelog for exec-summary cutover"
```

---

## Integration notes (for the human, post-merge)

- **Migration is automatic** on the next `cp sync` after the release lands — every active project's `cp.md` converts from `quick-resume` to `exec-summary`, seeded from old content + a dated "migrated" Update. First wrap-up per project then authors the real summary.
- **No mc-2 change** — this is cp-engine-only (no migration, no webhook change). The webhook *behavior* changes (it stops writing cp.md state) but that's via the engine code it runs, not a webhook deploy.
- **Release** via `scripts/release.py` (minor bump). Re-pin the tenant after.
- **Deferred / not in scope:** any dashboard surfacing of the Exec Summary; initiative-specific Exec Summary tuning (initiatives get the same region via their template — verify `initiative-cp.md.j2` either shares the region or is given one in Task 2 if it lacks it).

---

## Risks & test focus

- **Task 3 (migration) is the sharp edge** — it rewrites real files. The idempotency + placeholder-drop + outside-preserved tests are the safety net. Manually eyeball a real converted `cp.md` (e.g. dry-run sync against a copy of ibx-5167) before trusting it tenant-wide.
- **Producer/consumer seam (Task 4 + 5)** — auto-ingest stops producing the verbs while prep stops reading the line; grep both ends so no dangling reference survives (the kind of cross-seam bug the item-1/item-5 reviews caught).
- **The "all placeholder = None" rule** (Task 5/7) is what keeps a freshly-scaffolded but never-authored region from polluting the rollup with `_<...>_` noise — test it explicitly.
