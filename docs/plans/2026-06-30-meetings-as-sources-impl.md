# Meetings-as-Sources Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every tagged Fathom meeting a queryable, work-item-scoped source — resolve its project, embed its summary into RAG always, promote its full transcript to RAG on demand, and keep all of it correct across retags.

**Architecture:** Extend the existing mc-2 `fathom_meetings` table (it already holds all tagged meetings with summary + transcript jsonb) rather than create a new table. Add resolution/link columns + RAG-bridge timestamps. cp-engine owns the resolve/embed/promote/cascade logic and a migration; mc-2 adds a backend proxy + meeting-list UI. Engagements only in v1; meeting rows may carry `initiative_id` but promote stays engagement-gated.

**Tech stack:** Python 3.14 + uv (cp-engine), pytest, Supabase/PostgREST, the document-ingest pipeline (Voyage embedder), FastAPI webhook; mc-2 FastAPI backend + React/TS frontend.

**Companion design:** `cp/docs/plans/2026-06-30-meetings-as-sources-design.md` (v02).

**Worktrees:**
- cp-engine: `.worktrees/meetings-as-sources` (branch `feature/meetings-as-sources`, off v0.40.2)
- mc-2: `.worktrees/meetings-as-sources` (branch `feature/meetings-as-sources`)

**Key facts confirmed against live data:**
- `fathom_meetings` columns: `id` (uuid PK), `recording_id` (bigint, durable Fathom id), `title`, `meeting_date`, `duration_minutes`, `participants` (jsonb), `summary` (text), `transcript` (jsonb), `action_items`, `project_tags` (text[]), `account_company_id`, `sprint_planning_scope`, `fathom_url`, `share_url`, `meeting_type`, `manually_assigned`, `processed`.
- ibx-5167's 24 meetings are tagged `project_tags = ["IBX 5167 DDI Platform Video"]` (a display string = the project's full_job_name form). `account_company_id` is null.
- `_resolve_project_id(client, code)` in `mcp_server.py` already reverses the `full_job_name` slug form → `projects.id`. The project for these is `28b3e25e-319c-4e54-b17b-853694b8d754`.
- `rag_assets`: scope via `project_id`; `asset_chunks` derive scope from parent (no own `project_id`). Meeting rows use `source_provider='fathom'`, `source_file_id=<recording_id>`, `meta->>'kind'` ∈ {`meeting_summary`,`meeting_transcript`}.
- The v0.40.2 ingest path (`ingest_single_file` + `_configure_pipeline_once` + `_load_ingest_creds`) is the embed primitive to reuse.
- Latest mc-2 migration is `083`. Next is `084`.

---

## PHASE 1 — cp-engine backend (+ migration)

### Task 1: Migration — extend `fathom_meetings` with link + bridge columns

**Files:**
- Create: `mc-2 worktree: backend/migrations/084_fathom_meetings_linkage.sql`

**Step 1:** Write the migration:

```sql
-- 084_fathom_meetings_linkage.sql
-- Link Fathom meetings to a project/work-item and track RAG-bridge state.
alter table public.fathom_meetings
  add column if not exists project_id            uuid references public.projects(id) on delete set null,
  add column if not exists initiative_id         uuid references public.initiatives(id) on delete set null,
  add column if not exists work_item_id          uuid,  -- estimator.schedule_items.id; soft ref (cross-schema)
  add column if not exists work_item_confidence  real,
  add column if not exists summary_embedded_at   timestamptz,
  add column if not exists transcript_promoted_at timestamptz;

create index if not exists idx_fathom_meetings_project_id on public.fathom_meetings(project_id);
create index if not exists idx_fathom_meetings_recording_id on public.fathom_meetings(recording_id);
```

**Step 2:** Confirm against schema conventions (nullable, `on delete set null` so a project delete doesn't drop meeting history). `work_item_id` is a soft ref (estimator schedule item lives in another schema; FK omitted deliberately — note it in a comment).

**Step 3:** Apply to a Supabase branch (NOT prod yet) via mcp `apply_migration`, or document for the co-merge deploy. Verify columns exist.

**Step 4:** Commit (mc-2 worktree).

```bash
git add backend/migrations/084_fathom_meetings_linkage.sql
git commit -m "migration 084: fathom_meetings project/work-item linkage + RAG bridge cols"
```

---

### Task 2: `resolve_meeting_project` — map `project_tags` → project_id

**Files:**
- Create: `src/cp_engine/meetings.py`
- Test: `tests/test_meetings_resolve.py`

**Step 1: Write the failing test.**

```python
# tests/test_meetings_resolve.py
from cp_engine.meetings import resolve_meeting_project

class _FakeResolver:
    def __init__(self, mapping): self.mapping = mapping
    def __call__(self, client, code): return self.mapping.get(code)

def test_resolves_first_matching_tag():
    resolve = _FakeResolver({"IBX 5167 DDI Platform Video": "pid-5167"})
    pid, tag = resolve_meeting_project(object(), ["IBX 5167 DDI Platform Video"], resolver=resolve)
    assert pid == "pid-5167"
    assert tag == "IBX 5167 DDI Platform Video"

def test_untagged_returns_none():
    resolve = _FakeResolver({})
    pid, tag = resolve_meeting_project(object(), ["untagged"], resolver=resolve)
    assert pid is None

def test_empty_tags_returns_none():
    pid, tag = resolve_meeting_project(object(), [], resolver=lambda c, code: None)
    assert pid is None
```

**Step 2:** Run → FAIL (module missing).

**Step 3: Implement minimally.**

```python
# src/cp_engine/meetings.py
"""Meeting → project/RAG bridging. v1 substrate is the existing
`fathom_meetings` table; this module owns resolution, summary embedding,
transcript promotion, and the retag re-scope cascade."""
from __future__ import annotations

def _default_resolver(client, code):
    from cp_engine.mcp_server import _resolve_project_id
    return _resolve_project_id(client, code)

_UNTAGGED = {"untagged", ""}

def resolve_meeting_project(client, project_tags, *, resolver=_default_resolver):
    """Resolve a meeting's project_tags to (project_id, matched_tag).

    Tags are human display strings (full_job_name form). Returns the first tag
    that resolves to a real project, or (None, None) when none do / untagged.
    """
    for tag in project_tags or []:
        if not tag or tag.strip().lower() in _UNTAGGED:
            continue
        pid = resolver(client, tag)
        if pid:
            return pid, tag
    return None, None
```

**Step 4:** Run → PASS.

**Step 5:** Commit.

---

### Task 3: `embed_meeting_summary` — summary → rag_assets (`kind=meeting_summary`)

**Files:**
- Modify: `src/cp_engine/meetings.py`
- Test: `tests/test_meetings_embed_summary.py`

**Step 1: Failing test** — inject the ingest seam (like `spine_promote` tests do); assert it embeds the summary text under a stable file_path keyed on `recording_id` and stamps `meta.kind='meeting_summary'`, `source_provider='fathom'`, `source_file_id=<recording_id>`. Assert idempotency: a second call with `summary_embedded_at` set is a no-op.

**Step 2:** Run → FAIL.

**Step 3: Implement** `embed_meeting_summary(client, meeting_row, project_id, *, ingest=..., stamp=...)`:
- Skip if `summary` empty or `summary_embedded_at` already set (unless `force`).
- Write summary text to a stable temp path keyed on `recording_id` (mirror `spine_promote`'s stable-path discipline).
- `ingest(...)` then stamp the row: `source_provider='fathom'`, `source_file_id=str(recording_id)`, `meta={'kind':'meeting_summary'}`, `scope='project'`.
- Set `fathom_meetings.summary_embedded_at`.
- Return `{ok, asset_id}`; never raise (wrap).

**Step 4:** Run → PASS.

**Step 5:** Commit.

---

### Task 4: `promote_meeting_transcript` — transcript jsonb → rag_assets (`kind=meeting_transcript`)

**Files:**
- Modify: `src/cp_engine/meetings.py`
- Test: `tests/test_meetings_promote_transcript.py`

**Step 1: Failing test** — given a meeting row with a `transcript` jsonb, assert promote flattens it to text, ingests via the injected seam to a stable path keyed on `recording_id`, stamps `meta.kind='meeting_transcript'`, sets `transcript_promoted_at`, and is engagement-gated (returns `ok:false reason` when `project_id` resolves to an initiative / no company). Assert idempotent re-promote lands the same row.

**Step 2:** Run → FAIL.

**Step 3: Implement** `promote_meeting_transcript(...)`:
- Reuse the v0.40.2 ingest primitive (`ingest_single_file`) — but source the text from `fathom_meetings.transcript` (flatten jsonb segments to text) rather than a tenant-tree file. Write to stable temp path keyed on `recording_id`.
- Engagement guard: require a company-bearing project (mirror `promote_transcript` CONTRACT A).
- Stamp `meta.kind='meeting_transcript'`; set `transcript_promoted_at`.
- Verify exactly one row stamped (mirror CONTRACT B). Never raise.

**Step 4:** Run → PASS.

**Step 5:** Commit.

---

### Task 5: `link_meeting` — orchestrate resolve + summary embed on a single meeting

**Files:**
- Modify: `src/cp_engine/meetings.py`
- Test: `tests/test_meetings_link.py`

**Step 1: Failing test** — `link_meeting(client, meeting_row)`:
- resolves project_id from tags;
- writes `fathom_meetings.project_id` (+ `work_item_id`/`confidence` if a high-confidence guess is supplied via injected `assigner`, else null);
- calls `embed_meeting_summary`;
- on a retag (project_id changed vs. the stored one) triggers the cascade (Task 6).
Assert: tagged meeting → project_id set + summary embed called; untagged → project_id null + NO embed.

**Step 2-4:** RED → implement → GREEN. Keep the work-item assigner injected (real high-confidence logic is Task 7).

**Step 5:** Commit.

---

### Task 6: `rescope_meeting` — retag re-scope cascade (no re-embed)

**Files:**
- Modify: `src/cp_engine/meetings.py`
- Test: `tests/test_meetings_rescope.py`

**Step 1: Failing test** — given a meeting whose stored `project_id` differs from the newly-resolved one:
- `UPDATE fathom_meetings SET project_id=<new>` (and clear `work_item_id` — a new project means the old work item is invalid);
- `UPDATE rag_assets SET project_id=<new>, company_id=<new co> WHERE source_provider='fathom' AND source_file_id=<recording_id>` (covers BOTH summary + transcript rows; chunks follow by asset_id — NO re-embed);
- idempotent (running twice with same target is a no-op);
- old scope has zero `fathom`-sourced rag_assets rows afterward (no ghosts).
Assert via a fake client that records the UPDATEs.

**Step 2-4:** RED → implement → GREEN.

**Step 5:** Commit.

---

### Task 7: High-confidence work-item auto-assign

**Files:**
- Modify: `src/cp_engine/meetings.py`
- Test: `tests/test_meetings_workitem_assign.py`

**Step 1: Failing test** — `assign_work_item(meeting_row, project_id, *, classifier=...)` returns `(work_item_id, confidence)` and only sets the id when confidence ≥ threshold (default constant, e.g. `0.75`); below → `(None, confidence)`. The classifier (Claude routing reuse) is injected; test asserts threshold gating only, not the LLM.

**Step 2-4:** RED → implement → GREEN. Wire into `link_meeting`'s assigner seam.

**Step 5:** Commit. (Note in code: actual confidence source is the existing auto-ingest routing signal; threshold is configurable.)

---

### Task 8: Flow change — webhook also links the meeting

**Files:**
- Modify: `webhook/main.py` (in/after `_run_auto_ingest`, the single/account paths)
- Test: `tests/test_webhook_meeting_link.py`

**Step 1: Failing test** — simulate an auto-ingest invocation for a tagged meeting; assert that AFTER the existing sprint-file ingest, `link_meeting` is invoked for that meeting (additive — sprint write still happens; meeting-link failure is non-fatal and logged, never blocks the primary ingest, matching the existing `except` discipline at main.py:148).

**Step 2-4:** RED → implement (call `link_meeting` in a try/except that logs and continues) → GREEN.

**Step 5:** Commit.

---

### Task 9: Backfill command — `cp meetings-backfill [<code>]`

**Files:**
- Modify: `src/cp_engine/cli.py` (new command), `src/cp_engine/meetings.py` (batch driver)
- Test: `tests/test_meetings_backfill.py`

**Step 1: Failing test** — `backfill_meetings(client, code=None, *, link=...)` lists `fathom_meetings` (optionally filtered to one project's tags), runs `link_meeting` over each, returns a summary `{linked, skipped, embedded}`. Idempotent: rows already `summary_embedded_at` are skipped. No Fathom API — reads existing rows only.

**Step 2-4:** RED → implement → GREEN. Add `cp meetings-backfill` CLI wrapper (single-project or `--all`), echoing the summary + any unresolved-tag rows (log what was skipped — no silent caps).

**Step 5:** Commit.

---

### Task 10: MCP read tool — `list_project_meetings(code)`

**Files:**
- Modify: `src/cp_engine/mcp_server.py`
- Test: `tests/test_mcp_list_meetings.py`

**Step 1: Failing test** — `list_project_meetings("ibx-5167-ddi-platform-video")` returns rows for that project_id: `{recording_id, title, meeting_date, work_item_id, summary_embedded, transcript_promoted, fathom_url}`. Degrades to `{note}` on unresolvable code (mirror existing tool discipline).

**Step 2-4:** RED → implement (resolve code→project_id, select the meeting columns — explicit columns, never `*`, never the heavy `transcript` jsonb in the list) → GREEN.

**Step 5:** Commit.

---

### Task 11: Phase-1 whole-feature review + release

**Step 1:** Run the full cp-engine suite: `uv run pytest -q`. All green.
**Step 2:** Use superpowers:requesting-code-review for the cross-task seams (resolve↔embed↔rescope; the webhook non-fatal hook; idempotency of backfill + rescope).
**Step 3:** Address findings (regression test each).
**Step 4:** CHANGELOG `## v0.41.0` section; release via `scripts/release.py 0.41.0`; reinstall binary.

---

## PHASE 2 — mc-2 (backend proxy + UI)

### Task 12: Backend — meetings list endpoint

**Files:**
- Modify: `backend/src/routers/` (new `meetings.py` router or extend spine router)
- Test: backend test alongside

**Step 1: Failing test** — `GET /api/projects/{code}/meetings` resolves code→project_id (reuse the existing resolver), returns the meeting list (explicit columns, no transcript blob). RED → implement → GREEN. Source `.env` for backend tests.

**Step 5:** Commit.

---

### Task 13: Backend — assign work-item + promote-transcript proxies

**Files:**
- Modify: backend meetings router; reuse the signed `post_to_webhook` client.
- Test: backend tests.

**Step 1: Failing tests** —
- `PATCH /api/meetings/{recording_id}` sets `work_item_id` (manual reclassify; re-scopes nothing in RAG — just the meeting row).
- `POST /api/meetings/{recording_id}/promote-transcript` → signs + forwards to a new cp-engine webhook endpoint (mirror the spine promote-transcript proxy at `spine_verification.py:1205`), run-tracked.

RED → implement → GREEN.

**Step 5:** Commit.

---

### Task 14: cp-engine webhook — `/api/meetings/promote-transcript`

**Files:**
- Modify (cp-engine worktree): `webhook/main.py`
- Test: `tests/test_webhook_meeting_promote.py`

**Step 1: Failing test** — signed POST resolves the meeting + project, runs `promote_meeting_transcript` off the event loop (asyncio.to_thread), records outcome. Reuses the v0.40.2 ingest path. No tenant clone needed (transcript comes from the DB column, not a tenant file) — simpler than the spine promote path. RED → implement → GREEN.

**Step 5:** Commit. (This ships in the Phase-1 cp-engine release if sequenced before 11; otherwise a v0.41.1.)

---

### Task 15: Frontend — meeting list on the project view

**Files:**
- Modify (mc-2 worktree): `frontend/src/components/` (new meetings list; surface under the project/spine view)
- Test: frontend test alongside

**Step 1: Failing test** — renders the meeting list (title, date, summary, work-item badge or "needs assignment", Fathom link), a ★ promote control with status (⏳/✓/✗ + Retry), and a work-item reassign affordance. RED → implement → GREEN (mirror the spine card's promote-badge polling, including the item-6 P0 lesson: seed optimistic state but don't let a null refetch clobber it).

**Step 5:** Commit.

---

### Task 16: Phase-2 review + co-merge + deploy

**Step 1:** Full mc-2 backend + frontend suites green.
**Step 2:** superpowers:requesting-code-review across the cross-surface seams (recording_id threading card→proxy→webhook→run row→badge; reassign re-scopes nothing in RAG).
**Step 3:** Co-merge cp-engine + mc-2 PRs as a pair; apply migration 084; deploy webhook before/with mc-2 frontend (lagging webhook degrades the ★→promote leg gracefully).
**Step 4:** Live verify on ibx-5167: backfill → 24 meetings visible → ★ one rich meeting → confirm `meeting_transcript` chunks in rag_assets + retrievable via pull_project_source. Retag a meeting in Fathom → confirm re-scope cascade moved it (no ghost in old project).

---

## Cross-cutting notes
- **DRY:** reuse `_resolve_project_id`, `ingest_single_file`/`_configure_pipeline_once`/`_load_ingest_creds` (v0.40.2), the signed `post_to_webhook` client, `spine_promote_runs`-style run tracking.
- **YAGNI:** no new meetings table; no Fathom list API; initiatives deferred (column present, promote gated).
- **Never `SELECT *`** on `fathom_meetings` — it has a `transcript` jsonb that is large; list endpoints select explicit columns and never the transcript.
- **Idempotency is load-bearing** (Fathom retries + backfill re-runs): resolve/embed/rescope must all be safe to run repeatedly.
- **Retag trigger gap** (design doc): the same-project-retag webhook trigger fix is a prerequisite for the cascade to fire — fold into Task 8 or note as a deploy-time dependency on fathom-meeting-sync.
