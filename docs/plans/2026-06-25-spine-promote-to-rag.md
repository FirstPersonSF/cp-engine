# Promote Spine Transcript to RAG — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When a spine element is marked important, promote its source transcript into the RAG store (`rag_assets`) so it's retrievable via `pull_project_source`. Promotion is a standalone, idempotent operation; the importance flip calls it; a failed embed is retried by calling it again.

**Architecture:** A standalone `promote_spine_transcript(code, key)` MCP tool resolves the element's transcript file (via `rel_path`), embeds it through a thin single-file wrapper over the existing ingest pipeline, and writes a `rag_assets` row stamped `source_provider='spine-promote'`, `source_file_id=<est_item_id>`. Idempotency is **check-before-write** on `(project_id, source_file_id, source_provider)` — no migration. `set_spine_element` calls the tool on an `important: false→true` transition; importance is always set regardless of promotion outcome (promotion result returned alongside). Item 3 of cp-enhancements; **cp-engine-only**, **engagements only** (initiative promotion deferred with a clear note). Inline/synchronous embed (~20s) is accepted for this MVP.

**Tech stack:** Python 3.12, `uv`, pytest (inject a fake pipeline — the real `ingest` package is NOT importable in this venv, so tests MUST NOT import it), FastMCP, Supabase.

**Design doc:** cp tenant `docs/plans/2026-06-25-cp-enhancements-design.md` §3 (this plan refines it: check-before-write not upsert; engagements-only; standalone-tool-as-retry-door).

---

## Decisions pinned (some refine/supersede the design doc)

- **Standalone idempotent tool, importance flip calls it.** `promote_spine_transcript(code, key)` is the single promotion path AND the retry door. `set_spine_element` calls it on `false→true`. (Design doc framed promotion only as a set-path side-effect — this makes it first-class so retry = re-call.)
- **Importance always set; promotion non-fatal.** A failed embed (missing file, Voyage error) still sets `important=true` and returns a `promotion: {ok: false, reason}` field. Importance is independent of embed success (matches item 1).
- **Check-before-write idempotency** on `(owner, source_file_id=est_item_id, source_provider='spine-promote')` — NO migration, cp-engine-only. The TOCTOU race is irrelevant in this human-triggered/explicit-retry workflow.
- **Engagements only.** `_resolve_project_id` resolves only the `projects` table and `_owner_filter`'s initiative path is inert, so an initiative element would write a FK-violating row. Promotion of an initiative element returns `{note: "initiative promotion not yet supported"}` — no bad row. (Design doc claimed initiative support; this defers it cleanly.)
- **Inline synchronous embed** (~20s block) — accepted MVP per the architecture decision.

## Grounding (verified against current code, 2026-06-25)

- The ingest pipeline accepts a LOCAL file path directly: `IngestPipeline.ingest_file(file_path, title=, url=)` — no download needed. Built via `_build_pipeline(project_id, supabase_url, supabase_key)` (asset_ingest.py:707), which is "kept tiny + separate so tests can inject a pipeline." **The real `ingest` package is NOT importable in this venv** (`ModuleNotFoundError: No module named 'ingest'`), so the wrapper must accept an injected `pipeline_factory`/`pipeline` and tests must use a fake.
- `_stamp_scope` (asset_ingest.py:~1078) updates `rag_assets` by `(owner_col, file_path, status='active')`; `_owner_filter(folders)` (asset_ingest.py:~1117) returns `("project_id"|"initiative_id", id)` — initiative branch INERT.
- `rag_assets`: no unique index on `source_file_id`; dedup today is `(project_id, file_path) WHERE status='active'`. Columns: project_id/initiative_id (CHECK num_nonnulls==1), title, file_hash, status, scope, file_path, source_provider, source_file_id, source_path.
- Spine element `rel_path` (relative to project dir) is in `spine_substance` but NOT in `_SPINE_RESOLVE_COLUMNS` (project_sources.py:236) — must be added so `resolve_live_element` (project_sources.py:270) returns it.
- `set_spine_element` (mcp_server.py:417-452): has `client, pid` via `_resolve`; `row.get("important")` is the PRIOR value (the transition signal). `_resolve` returns `(client, project_id, company_id)`; company_id is None for initiatives.
- `_tenant_root()` (mcp_server.py:33) gives the tenant root for building the absolute transcript path.
- Pipeline-injection test pattern: `tests/test_asset_ingest_run.py`.

---

## Context the executor needs

- **Test recipe (uv quirk):** `uv pip install -e . --force-reinstall -q` ONCE, then `uv run --no-sync --dev python -m pytest ...`. Never bare `pytest`. Revert `uv.lock` before committing.
- **Never import the real `ingest` package in tests** — it isn't installed here. Inject fakes.
- **No `SELECT *`.** **MCP tools never throw** — structured `{error}`/`{note}`.
- **Engagements only** — detect & defer initiatives, never write a row with a non-projects id in `project_id`.

---

## Task 1: `resolve_live_element` returns `rel_path`

**Files:** Modify `src/cp_engine/project_sources.py` (`_SPINE_RESOLVE_COLUMNS` ~236). Test: `tests/test_spine_resolve_relpath.py` (create).

**Step 1: Failing test**
```python
# tests/test_spine_resolve_relpath.py
from cp_engine.project_sources import resolve_live_element


def _client(rows, captured):
    class _T:
        def __init__(self, n): captured.setdefault("table", n)
        def select(self, c): captured["select"] = c; return self
        def eq(self, c, v): captured.setdefault("eqs", []).append((c, v)); return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": rows})()
    class _C:
        def table(self, n): return _T(n)
    return _C()


def test_resolve_live_element_includes_rel_path():
    captured = {}
    rows = [{"id": "p/w1/v1", "est_item_id": "w1", "framing": "W1",
             "status": "live", "important": False, "note": None,
             "rel_path": "meetings/2026-06-20-kickoff.txt"}]
    el = resolve_live_element(_client(rows, captured), "pid", "w1")
    assert "rel_path" in captured["select"]      # selected, not *
    assert "*" not in captured["select"]
    assert el["rel_path"] == "meetings/2026-06-20-kickoff.txt"
```

**Step 2: Run — FAIL** (`rel_path` not selected/returned).

**Step 3:** Add `rel_path` to `_SPINE_RESOLVE_COLUMNS` (currently `"id, est_item_id, framing, status, important, note"` → append `, rel_path`). `resolve_live_element` returns the raw row, so `rel_path` flows through automatically once selected.

**Step 4: Run — PASS**, plus `tests/test_spine_set_element.py` (resolve_live_element's other consumer) still green.

**Step 5: Commit**
```bash
git checkout HEAD -- uv.lock
git add src/cp_engine/project_sources.py tests/test_spine_resolve_relpath.py
git commit -m "feat(spine): resolve_live_element returns rel_path (for promotion)"
```

---

## Task 2: `ingest_single_file` wrapper (injectable, no real ingest)

**Files:** Create `src/cp_engine/spine_promote.py`. Test: `tests/test_spine_promote_ingest.py`.

**Contract:** `ingest_single_file(file_path, project_id, title, *, supabase_url, supabase_key, pipeline_factory=_build_pipeline) -> dict`. Builds (or is injected) a pipeline, calls `pipeline.ingest_file(file_path, title=title, url=None)`, returns its result. The `pipeline_factory` seam mirrors `_build_pipeline` so tests inject a fake — NEVER import real `ingest`.

**Step 1: Failing test**
```python
# tests/test_spine_promote_ingest.py
from cp_engine.spine_promote import ingest_single_file


class _FakePipeline:
    def __init__(self): self.calls = []
    def ingest_file(self, file_path, title=None, url=None):
        self.calls.append((file_path, title, url))
        return {"asset_id": "a1", "status": "new"}


def test_ingest_single_file_calls_pipeline_ingest_file():
    fake = _FakePipeline()
    out = ingest_single_file(
        "/tmp/t.txt", "pid", "My Transcript",
        supabase_url="u", supabase_key="k",
        pipeline_factory=lambda project_id, supabase_url, supabase_key: fake,
    )
    assert fake.calls == [("/tmp/t.txt", "My Transcript", None)]
    assert out["asset_id"] == "a1"
```

**Step 2: Run — FAIL** (module/func missing).

**Step 3:** Implement `src/cp_engine/spine_promote.py`:
```python
"""Promote a spine element's source transcript into the RAG store.

A spine element marked important gets its underlying transcript embedded into
`rag_assets` so it's retrievable via pull_project_source. Idempotent on
(owner, source_file_id=est_item_id): re-promoting updates in place.
"""
from __future__ import annotations


def _default_pipeline_factory(project_id, supabase_url, supabase_key):
    # Lazy: the real `ingest` package is optional and may be absent (e.g. tests).
    from cp_engine.asset_ingest import _build_pipeline
    return _build_pipeline(project_id, supabase_url, supabase_key)


def ingest_single_file(file_path, project_id, title, *,
                       supabase_url, supabase_key,
                       pipeline_factory=_default_pipeline_factory) -> dict:
    """Embed ONE local file into rag_assets for `project_id`. Returns the
    pipeline's result dict. `pipeline_factory` is injectable for tests."""
    pipeline = pipeline_factory(project_id, supabase_url, supabase_key)
    return pipeline.ingest_file(file_path, title=title, url=None)
```

**Step 4: Run — PASS** + full suite.

**Step 5: Commit** (`feat(spine): ingest_single_file wrapper over the ingest pipeline`).

---

## Task 3: the stamp/idempotency write (check-before-write)

**Files:** Modify `src/cp_engine/spine_promote.py`. Test: `tests/test_spine_promote_stamp.py`.

**Contract:** `stamp_promoted_asset(client, *, project_id, est_item_id, title, file_path) -> dict`. After `ingest_single_file` has created/updated the row (keyed by file_path), stamp `source_provider='spine-promote'`, `source_file_id=est_item_id` onto it, AND enforce idempotency: if a prior `spine-promote` row for `(project_id, est_item_id)` exists, ensure we don't leave duplicates. Practically: locate the just-ingested active row by `(project_id, file_path, status='active')` and update its source_* fields. (The `(project_id, file_path)` dedup already collapses re-ingests of the same stable path, and our stable synthetic path keyed on est_item_id makes re-promotion land on the same row — so the file_path-targeted stamp IS the idempotency.)

**Stable synthetic path:** promotion writes the transcript to a deterministic temp path derived from est_item_id (mirroring `_stable_dir_for`), so re-promotion reuses the same `file_path` → the pipeline's dedup skips/versions instead of duplicating. The stamp then re-points source_* (idempotent).

**Step 1: Failing test** (fake client capturing update calls):
```python
# tests/test_spine_promote_stamp.py
from cp_engine.spine_promote import stamp_promoted_asset


def test_stamp_sets_source_provider_and_file_id():
    captured = {"updates": []}
    class _T:
        def __init__(self, n): pass
        def update(self, d): captured["updates"].append(d); return self
        def eq(self, c, v): captured.setdefault("eqs", []).append((c, v)); return self
        def execute(self): return type("R", (), {"data": [{"id": "a1"}]})()
    class _C:
        def table(self, n): return _T(n)
    out = stamp_promoted_asset(_C(), project_id="pid", est_item_id="w1",
                               title="T", file_path="/tmp/w1/t.txt")
    u = captured["updates"][0]
    assert u["source_provider"] == "spine-promote"
    assert u["source_file_id"] == "w1"
    # located by project_id + file_path + active
    assert ("project_id", "pid") in captured["eqs"]
    assert ("file_path", "/tmp/w1/t.txt") in captured["eqs"]
```

**Step 2: Run — FAIL.**

**Step 3:** Implement `stamp_promoted_asset` — an `update({source_provider, source_file_id, source_path:None, scope:'project'}).eq("project_id", project_id).eq("file_path", file_path).eq("status","active")`. (Mirrors `_stamp_scope` but with spine-promote provenance. No `SELECT *`.)

**Step 4: Run — PASS** + full suite.

**Step 5: Commit** (`feat(spine): stamp promoted asset with spine-promote provenance`).

---

## Task 4: `promote_transcript` orchestration (resolve → file → embed → stamp), engagement-only

**Files:** Modify `src/cp_engine/spine_promote.py`. Test: `tests/test_spine_promote_orchestrate.py`.

**Contract:** `promote_transcript(client, tenant_root, project_code, project_id, company_id, element_row, *, supabase_url, supabase_key, ingest=ingest_single_file) -> dict`. Returns `{"ok": True, "asset": ...}` or `{"ok": False, "reason": ...}`. Steps:
1. **Engagement-only guard:** if `company_id is None` (the `_resolve` signal that folders didn't resolve → initiative/no-projects-row), return `{"ok": False, "reason": "initiative promotion not yet supported"}`. (company_id is None for initiatives per `_resolve`.)
2. Resolve transcript path: `rel_path = element_row.get("rel_path")`; if falsy → `{"ok": False, "reason": "element has no source file (rel_path)"}`. Build `path = tenant_root / <project dir> / rel_path`. If not exists → `{"ok": False, "reason": "transcript file not found: <path>"}`.
3. Copy/ensure the file at a STABLE path keyed on est_item_id (so re-promotion dedups). Title = element framing or filename.
4. `ingest(path, project_id, title, supabase_url=, supabase_key=)`.
5. `stamp_promoted_asset(client, project_id=, est_item_id=, title=, file_path=)`.
6. Return `{"ok": True, "asset_id": ..., "title": ...}`.
Never raises — wrap and return `{"ok": False, "reason": str(exc)}`.

**Step 1:** Failing tests with everything injected (fake client, fake `ingest`, a tmp file the test writes, and the engagement-only + missing-file paths). Assert: initiative (company_id=None) → ok False + the defer reason WITHOUT calling ingest; missing rel_path → ok False; happy path → ingest called + stamp called + ok True.

**Step 2–4:** implement to green; full suite.

**Step 5: Commit** (`feat(spine): promote_transcript orchestration (engagement-only, fail-soft)`).

---

## Task 5: `promote_spine_transcript` MCP tool

**Files:** Modify `src/cp_engine/mcp_server.py`. Test: `tests/test_spine_promote_tool.py`.

**Contract:** `@mcp.tool() promote_spine_transcript(project_code, key) -> dict`. Resolve via `_resolve` + `resolve_live_element` (now returns rel_path), call `promote_transcript`, return its result shaped as `{est_item_id, promotion: {ok, ...}}` or structured `{note}`/`{error}`. Never throws. Idempotent (re-call = re-stamp same row). This IS the retry door.

**Step 1:** Failing test — monkeypatch `_resolve` to return a fake `(client, pid, company_id)` and monkeypatch `promote_transcript` to assert it's called with the resolved row; assert the tool's return shape; assert initiative (company_id None) surfaces the defer note; assert no-match key → `{note}`.

**Step 2–4:** implement to green; full suite. (Mirror sibling tools' `_resolve` + never-throw idiom.)

**Step 5: Commit** (`feat(spine): promote_spine_transcript MCP tool (idempotent retry door)`).

---

## Task 6: wire `set_spine_element` to call it on `false→true`

**Files:** Modify `src/cp_engine/mcp_server.py` (`set_spine_element` 417-452). Test: `tests/test_spine_set_promote_wiring.py`.

**Contract:** when `important` goes `false→true` (prior `row.get("important")` falsy AND new `important is True`), call `promote_transcript` AFTER the importance update, and include its result as `"promotion"` in the return. Importance is ALWAYS set first (independent of promotion). NOT triggered when important is already true, or set to False/None.

**Step 1: Failing tests** (monkeypatch `promote_transcript`):
- `important False→True`: importance updated, `promote_transcript` called, return has `important: True` AND `promotion: {...}`.
- `important already True` (prior row important=True): `promote_transcript` NOT called (no redundant re-embed on a no-op).
- `important=None` / `important=False`: `promote_transcript` NOT called.
- promotion fails: `important: True` still returned, `promotion: {ok: False, reason}` present (non-fatal).

**Step 2:** Run — FAIL.

**Step 3:** In `set_spine_element`, after the `patch` update, add:
```python
promotion = None
prior_important = bool(row.get("important"))
if important is True and not prior_important:
    promotion = promote_transcript(client, _tenant_root(), project_code, pid,
                                   _cid, row, supabase_url=..., supabase_key=...)
result = {"est_item_id": ..., "important": ..., "note": ...}
if promotion is not None:
    result["promotion"] = promotion
return result
```
(Pull `supabase_url`/`supabase_key` from the same config `_resolve`/MC2Backend uses — read how `_resolve` builds the client; reuse that config. If they're not readily available at the tool layer, have `promote_transcript` accept the `client` and derive what it needs, or thread the config through — keep it consistent with how asset_ingest gets creds.)

**Step 4:** Run — PASS + FULL suite green.

**Step 5: Commit** (`feat(spine): set_spine_element promotes transcript on important false→true`).

---

## Task 7: docstrings + full suite + review

- Update `set_spine_element` docstring: marking important promotes its transcript (engagement-only; failure non-fatal, see `promotion` in return). Document `promote_spine_transcript` as the standalone/retry tool.
- Full suite green.
- REQUIRED SUB-SKILL: superpowers:requesting-code-review.

---

## Out of scope (tracked, not built here)

- **Initiative promotion** — deferred (returns a clear note). Needs an initiative-aware resolve + wiring `_owner_filter`'s inert `is_initiative` path.
- **Async/webhook promotion** — MVP is inline/synchronous (~20s). A background-queue path is the production follow-up.
- **Un-promote on important→false** — design says leave the asset; no retraction.
- **A unique index on (project_id, source_file_id)** — check-before-write suffices for this workflow; the index is a future hardening if promotion ever becomes concurrent.
- **Dashboard surfacing of promotion status** — mc-2 frontend, separate.
