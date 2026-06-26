# Spine "Done" (Derived Read-Through) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Surface a derived `done` status on spine elements by reading through to the Gantt schedule bar's completion (`estimator.schedule_items.done`) — no new DB state, never hand-stored.

**Architecture:** For each live spine element bound to a work-item, `done` is derived from whether ANY of that work-item's schedule bars is marked done (reusing the existing `execution_status.derive_status` Rule-1 semantics). The project's bars are batch-fetched ONCE per `list_spine_elements` call (no N+1), keyed `work_item_id → done`, and joined to elements by `est_item_id`. Three states: `true` (bound, a bar is done), `false` (bound, no bar done), `null` (unbound/orphaned — no real work-item). Item 2 of the cp-enhancements design; cp-engine-only.

**Tech stack:** Python 3.12, `uv`, pytest (fake-supabase-client pattern), FastMCP, cross-schema Supabase reads (`client.schema("estimator")`).

**Design doc:** cp tenant `docs/plans/2026-06-25-cp-enhancements-design.md` §2.

---

## Decisions pinned (some SUPERSEDE the design doc)

- **ANY bar done ⇒ done** (not `bool_and`/all-bars). This SUPERSEDES the design doc's "all bars done" pick — made before we found the shared `execution_status.derive_status`, whose Rule 1 (`any(bar.done)`) is the app-wide meaning of "done" (the Gantt execution-status badge). Reusing it keeps ONE consistent definition of done; diverging would let an item read done on the Gantt but not in the spine.
- **Only Rule 1 is needed.** `done` depends solely on the `done` flag — not on `start_date`/substance/recency/today. So we do NOT assemble full `derive_status` inputs; we apply its Rule-1 logic (`any bar.done`) directly. (We deliberately do NOT surface the richer active/next/flag/done? status here — that's a separate, larger feature. Item 2 is strictly the boolean `done`.)
- **`null` for unbound** (not `false`): an element with no real `est_item_id` in the estimate (binding `unbound`/`orphaned`, or an `_authored/...` id) has no work-item, so done is n/a → `null`. The dashboard renders "—", not an empty checkbox.

## Grounding (verified against current code, 2026-06-25)

- `ScheduleItem` dataclass already carries `work_item_id`, `work_item_kind`, `done` — `estimate.py:125-141`; `_SCHEDULE_COLUMNS` already selects them (migration 069) — `estimate.py:155-158`.
- `fetch_estimate(client, mc_project_id) -> Estimate | None` — `estimate.py:161`. `Estimate` carries `id` (the estimator project id) and `start_date`/items.
- `fetch_schedule(client, estimate_id) -> list[ScheduleItem]` — `estimate.py:241`. Filters by `estimate_id` (= `estimator.projects.id`), NOT `mc_project_id`. So: `est = fetch_estimate(client, pid)`, then `fetch_schedule(client, est.id)`.
- Join validity: `spine_substance.est_item_id` (text, holds the stringified work-item uuid for live elements) == `schedule_items.work_item_id` (uuid) — direct match for bound elements; `_authored/...` ids never match (correctly → null). Verified.
- `list_spine(client, project_id)` — `project_sources.py:231`; `_spine_element` (pull) — same file. Both already return `important`/`note` (item 1). `list_spine` already has `project_id` (the mc_project_id) in hand — enough to call `fetch_estimate`.
- `list_spine_elements` / `pull_spine_element` MCP tools — `mcp_server.py:279` / `:303`; both resolve `(client, pid, cid)` via `_resolve`.
- Reuse: `execution_status.derive_status` Rule 1 — `execution_status.py:73-75` (`if any(_bar_field(b, "done") for b in bars): return Status("done", ...)`).

---

## Context the executor needs

- **Run tests (uv quirk):** `uv pip install -e . --force-reinstall -q` ONCE, then `uv run --no-sync --dev python -m pytest ...`. Never bare `pytest`. Revert `uv.lock` before committing (`git checkout HEAD -- uv.lock`).
- **No `SELECT *`** — enforced by tests. We add no new columns/queries; `fetch_schedule` already selects what we need.
- **MCP tools never throw** — wrap in try/except returning a structured note (match siblings). A failure deriving `done` must degrade to `null`/omission, NEVER break the listing.
- **No N+1:** fetch the project's bars ONCE per `list_spine` call, build a `work_item_id → done` map, then look up per element. Do not call `fetch_schedule` per element.

---

## Task 1: A pure `done_map` builder (bars → {work_item_id: done})

**Files:**
- Create: `src/cp_engine/spine_done.py`
- Test: `tests/test_spine_done.py`

**Why pure/separate:** isolates the ANY-bar-done logic from any DB/client, so it's unit-testable without mocking and documents the Rule-1 reuse in one place.

**Step 1: Write the failing test**

```python
# tests/test_spine_done.py
from cp_engine.spine_done import build_done_map, derive_done


def _bar(work_item_id, done):
    # mimics estimate.ScheduleItem's relevant fields (dataclass OR dict both ok)
    return {"work_item_id": work_item_id, "done": done}


def test_build_done_map_any_bar_done_wins():
    bars = [_bar("w1", False), _bar("w1", True), _bar("w2", False)]
    m = build_done_map(bars)
    assert m["w1"] is True      # any bar done → True (Rule 1 semantics)
    assert m["w2"] is False     # bound, no bar done → False


def test_build_done_map_ignores_bars_without_work_item():
    bars = [_bar(None, True), _bar("w1", False)]   # schedule-native bar (no work item)
    m = build_done_map(bars)
    assert None not in m
    assert m["w1"] is False


def test_derive_done_three_states():
    done_map = {"w1": True, "w2": False}
    # bound + a bar done → True
    assert derive_done("w1", done_map) is True
    # bound + no bar done → False
    assert derive_done("w2", done_map) is False
    # not in the estimate (unbound/orphaned/_authored) → None (n/a)
    assert derive_done("_authored/x", done_map) is None
    assert derive_done(None, done_map) is None
```

**Step 2: Run — expect FAIL** (module missing).

**Step 3: Implement**

```python
# src/cp_engine/spine_done.py
"""Derived spine `done` — reads through to the Gantt bar's completion.

`done` is NEVER stored on the spine; it mirrors `estimator.schedule_items.done`
via the work-item link. ANY bar marked done ⇒ done (the app-wide Rule-1
semantics from `execution_status.derive_status`), so the spine's `done` matches
the Gantt execution-status badge exactly. Three states:
  True  — bound to a work-item that has a done bar
  False — bound, but no bar is done
  None  — not bound to a real work-item (n/a)
"""
from __future__ import annotations


def _field(bar, key, default=None):
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def build_done_map(bars) -> dict:
    """Map work_item_id → done(bool), folding multiple bars with ANY-done wins.
    Bars without a work_item_id (schedule-native: milestones, holidays) are
    ignored — they bind to no spine element."""
    out: dict = {}
    for b in bars or []:
        wid = _field(b, "work_item_id")
        if wid is None:
            continue
        out[wid] = bool(out.get(wid, False) or _field(b, "done", False))
    return out


def derive_done(est_item_id, done_map: dict):
    """True/False if `est_item_id` is a real work-item in the schedule map;
    None when it isn't bound to one (unbound/orphaned/_authored → n/a)."""
    if not est_item_id or est_item_id not in done_map:
        return None
    return done_map[est_item_id]
```

**Step 4: Run — expect PASS.**

**Step 5: Commit**
```bash
git checkout HEAD -- uv.lock
git add src/cp_engine/spine_done.py tests/test_spine_done.py
git commit -m "feat(spine): pure done-derivation (any-bar-done, three-state)"
```

---

## Task 2: A client-level `fetch_project_done_map` (estimate → bars → map)

**Files:**
- Modify: `src/cp_engine/spine_done.py`
- Test: `tests/test_spine_done.py`

**Why:** wraps the two existing fetches (`fetch_estimate` → `fetch_schedule`) into one "give me the project's done-map" call, returning `{}` when there's no estimate (so unbound projects degrade to all-`null`). Keeps the DB plumbing out of `list_spine`.

**Step 1: Write the failing test** (inject the two fetchers so no client is needed):

```python
# (append to tests/test_spine_done.py)
from cp_engine import spine_done


def test_fetch_project_done_map_uses_estimate_then_schedule(monkeypatch):
    class _Est:  # mimics estimate.Estimate enough
        id = "EST1"
    captured = {}
    monkeypatch.setattr(spine_done, "fetch_estimate", lambda c, pid: (_Est() if pid == "pid" else None))
    def _fake_schedule(c, estimate_id):
        captured["estimate_id"] = estimate_id
        return [{"work_item_id": "w1", "done": True}]
    monkeypatch.setattr(spine_done, "fetch_schedule", _fake_schedule)
    m = spine_done.fetch_project_done_map(object(), "pid")
    assert captured["estimate_id"] == "EST1"   # filtered by ESTIMATE id, not pid
    assert m == {"w1": True}


def test_fetch_project_done_map_no_estimate_returns_empty(monkeypatch):
    monkeypatch.setattr(spine_done, "fetch_estimate", lambda c, pid: None)
    # fetch_schedule must NOT be called when there's no estimate
    monkeypatch.setattr(spine_done, "fetch_schedule",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")))
    assert spine_done.fetch_project_done_map(object(), "pid") == {}
```

**Step 2: Run — expect FAIL.**

**Step 3: Implement** — add to `spine_done.py`:

```python
from cp_engine.estimate import fetch_estimate, fetch_schedule


def fetch_project_done_map(client, mc_project_id) -> dict:
    """The project's work_item_id → done map, or {} when it has no estimate.
    Two reads against the `estimator` schema (estimate, then its schedule)."""
    est = fetch_estimate(client, mc_project_id)
    if est is None:
        return {}
    return build_done_map(fetch_schedule(client, est.id))
```

(Import at module top is fine; the monkeypatch targets `spine_done.fetch_estimate`/`fetch_schedule`, which the `from ... import` binds into this module's namespace.)

**Step 4: Run — expect PASS.**

**Step 5: Commit**
```bash
git checkout HEAD -- uv.lock
git add src/cp_engine/spine_done.py tests/test_spine_done.py
git commit -m "feat(spine): fetch_project_done_map (estimate→schedule→done-map)"
```

---

## Task 3: `list_spine` surfaces `done` (batch, no N+1)

**Files:**
- Modify: `src/cp_engine/project_sources.py` — `list_spine` (~231)
- Test: `tests/test_spine_done_read.py` (create)

**Step 1: Write the failing test** (fake client returns spine rows; inject the done-map so no estimator client is needed):

```python
# tests/test_spine_done_read.py
import cp_engine.project_sources as ps


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


def _row(eid, important=False):
    return {"est_item_id": eid, "framing": eid, "layer": "Note",
            "binding": "live", "status": "live", "serves": [], "body": "b",
            "important": important, "note": None}


def test_list_spine_adds_done_from_map(monkeypatch):
    # w1 bound+done → True; w2 bound+not-done → False; _authored → None (n/a)
    monkeypatch.setattr(ps, "fetch_project_done_map",
                        lambda client, pid: {"w1": True, "w2": False})
    captured = {}
    rows = [_row("w1"), _row("w2"), _row("_authored/x")]
    out = ps.list_spine(_client(rows, captured), "pid")
    by = {r["est_item_id"]: r for r in out}
    assert by["w1"]["done"] is True
    assert by["w2"]["done"] is False
    assert by["_authored/x"]["done"] is None   # unbound → n/a


def test_list_spine_done_degrades_to_none_on_estimate_error(monkeypatch):
    # If the done-map fetch fails, listing must still work — done omitted/None,
    # never an exception.
    def _boom(client, pid): raise RuntimeError("estimator down")
    monkeypatch.setattr(ps, "fetch_project_done_map", _boom)
    captured = {}
    out = ps.list_spine(_client([_row("w1")], captured), "pid")
    assert out[0].get("done") is None    # graceful: None, not a crash
```

**Step 2: Run — expect FAIL** (`done` not present; second test may error).

**Step 3: Implement** in `project_sources.py`:
- Import at top: `from cp_engine.spine_done import fetch_project_done_map, derive_done`.
- In `list_spine`, AFTER fetching `rows` and BEFORE building `out`, fetch the map ONCE, fail-soft:
  ```python
  try:
      done_map = fetch_project_done_map(client, project_id)
  except Exception:  # noqa: BLE001 — done is best-effort; never break the listing
      done_map = {}
  ```
  Then in each `out.append({...})` add: `"done": derive_done(row.get("est_item_id"), done_map)`.
- Keep the existing important-first sort unchanged (done does not affect sort).

NOTE: when `done_map` is `{}` (no estimate OR error), `derive_done` returns `None` for every element — exactly the desired "n/a" degradation.

**Step 4: Run — expect PASS.** Then existing spine read tests:
`uv run --no-sync --dev python -m pytest tests/test_spine_importance_read.py tests/test_project_sources.py tests/test_spine_done_read.py -q`

**Step 5: Commit**
```bash
git checkout HEAD -- uv.lock
git add src/cp_engine/project_sources.py tests/test_spine_done_read.py
git commit -m "feat(spine): list_spine surfaces derived done (batch fetch, fail-soft)"
```

---

## Task 4: `pull_spine` / `_spine_element` surfaces `done`

**Files:**
- Modify: `src/cp_engine/project_sources.py` — `pull_spine` (~266) + `_spine_element` (~325)
- Test: `tests/test_spine_done_read.py`

**Wrinkle:** `_spine_element` is a pure row-shaper with no client; `done` needs the map. So derive `done` in `pull_spine` (which HAS the client) and pass it into `_spine_element`, OR set it on the returned dict in `pull_spine` after `_spine_element` returns. SIMPLER: set it after — keeps `_spine_element` pure.

**Step 1: Write the failing test:**

```python
# (append to tests/test_spine_done_read.py)
def test_pull_spine_includes_done(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map",
                        lambda client, pid: {"w1": True})
    captured = {}
    el = ps.pull_spine(_client([_row("w1")], captured), "pid", "w1")
    assert el["done"] is True


def test_pull_spine_done_none_for_unbound(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda client, pid: {})
    captured = {}
    el = ps.pull_spine(_client([_row("_authored/x")], captured), "pid", "_authored/x")
    assert el["done"] is None
```

**Step 2: Run — expect FAIL.**

**Step 3: Implement:** in `pull_spine`, on the SUCCESS path (after `_match_one_live` yields the row and `_spine_element` shapes it), fetch the map fail-soft (same try/except as Task 3) and set `result["done"] = derive_done(row.get("est_item_id"), done_map)` before returning. Do NOT add `done` to the failure (`error`) returns. Leave `_spine_element` pure (no `done` key added there).

**Step 4: Run — expect PASS**, plus full read suite.

**Step 5: Commit**
```bash
git checkout HEAD -- uv.lock
git add src/cp_engine/project_sources.py tests/test_spine_done_read.py
git commit -m "feat(spine): pull_spine surfaces derived done"
```

---

## Task 5: Full suite + docstrings + review

**Step 1:** Update the `list_spine`/`pull_spine` docstrings' `Returns` clauses to mention `done` (true/false/null = n/a). One line each.
**Step 2:** Full suite — `uv pip install -e . --force-reinstall -q` then `uv run --no-sync --dev python -m pytest -q`. Expect all green (the lone `test_cli_parse_sprint` env-quirk passes after the force-reinstall).
**Step 3:** `git checkout HEAD -- uv.lock`; commit any docstring change.
**Step 4:** REQUIRED SUB-SKILL: superpowers:requesting-code-review.

---

## Out of scope (tracked, not built here)

- **Richer execution status** (active/next/flag/done?) on spine elements — item 2 is strictly the boolean `done`. The full `derive_status` is a separate, larger feature.
- **Dashboard ✓/—/☐ rendering** of `done` on the `/spine` card (mc-2 frontend) — pairs with the item-1 UI work.
- **MCP tool exposure beyond list/pull** — `done` is read-only (set it on the Gantt). No new set-tool.
- The design doc's `bool_and` (all-bars) semantics — SUPERSEDED here by any-bar-done; update the design doc's §2 note when convenient.
