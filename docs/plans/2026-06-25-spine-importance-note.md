# Spine Importance + Context Note — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a first-class `important` flag and a persistent `note` ("why this matters") to spine elements, settable via a new MCP tool and surfaced in the spine read path (sorted important-first).

**Architecture:** Two new columns on `spine_substance` (mc-2 migration). The cp-engine authored-element row builders carry `important`/`note` forward across versions (decision A). A new `set_spine_element` MCP tool does a targeted partial update of the live row. `list_spine` / `pull_spine` return the new fields and sort important-first. This plan covers **item 1 of the cp-enhancements design** — the keystone. It is cp-engine-only except for the one mc-2 migration (tracked here as the first task, applied via the supabase MCP).

**Tech stack:** Python 3.12, `uv`, pytest (fake-supabase-client pattern, see `tests/test_spine_read_mc2.py`), FastMCP, MC-2 Supabase (`spine_substance` table).

**Design doc:** `cp` tenant `docs/plans/2026-06-25-cp-enhancements-design.md` (§1).

---

## Context the executor needs

- **Run tests:** `uv run --dev python -m pytest -q` from the worktree root. A single test: `uv run --dev python -m pytest tests/test_X.py::test_name -v`.
- **The row builder is the carry-forward seam.** `src/cp_engine/authored_element.py:_row` (line 68) builds every spine_substance row. Adding `important`/`note` params here, and threading them through `build_version_rows` (line 107) from `base = prior_versions[0]`, is the entire carry-forward mechanism (decision A).
- **No `*` selects.** Project rule + enforced by tests (`assert "*" not in captured["select"]`). When you add columns to a SELECT, add them to the explicit column constant.
- **MCP tools never throw.** Every `@mcp.tool` wraps its body in `try/except Exception` and returns a structured `{"error": ...}` / note. Match the existing pattern (`mcp_server.py:368`).
- **Column constants:** `_SPINE_LIST_COLUMNS` (project_sources.py:222), `_SPINE_PULL_COLUMNS` (225). Both need `important, note` added.
- **mc-2 parity note:** the design flags that `authored_element.py` is mirrored into mc-2 and kept in parity by a golden-vector test. This plan changes the cp-engine copy; a follow-up (out of scope here) re-syncs the mc-2 mirror. Leave a `# PARITY:` comment where you touch `_row`.

---

## Task 1: mc-2 migration — add `important` + `note` columns

**Files:**
- Create (mc-2 repo): `backend/migrations/0NN_spine_importance_note.sql` (use the next free number — check `ls backend/migrations | tail`).

**Step 1: Write the migration**

```sql
-- 0NN_spine_importance_note.sql
-- Item 1 of cp-enhancements: first-class importance + a persistent
-- "why this matters" note on spine elements. `note` is distinct from the
-- per-version `version_note` (a "what changed" memo) — `note` is an
-- element-level standing annotation, carried forward across versions.
ALTER TABLE spine_substance
  ADD COLUMN IF NOT EXISTS important boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS note text;
```

**Step 2: Apply** via the supabase MCP `apply_migration` against the MC-2 project (`mgheymslksfyhuvhmvmj`), or note for the human to run if MCP write isn't available. Verify with `list_tables` that `spine_substance` now shows both columns.

**Step 3: Commit** (in mc-2 repo, separate from cp-engine work):
```bash
git add backend/migrations/0NN_spine_importance_note.sql
git commit -m "feat(spine): add important + note columns to spine_substance"
```

> The remaining tasks are all in the **cp-engine worktree**.

---

## Task 2: Row builder carries `important` + `note` (carry-forward, decision A)

**Files:**
- Modify: `src/cp_engine/authored_element.py:68` (`_row`), `:93` (`build_create_rows`), `:107` (`build_version_rows`)
- Test: `tests/test_authored_element_importance.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_authored_element_importance.py
from cp_engine.authored_element import build_create_rows, build_version_rows


def test_create_defaults_important_false_note_none():
    rows = build_create_rows(
        project_id="pid", project_code="p", label="Fred lens",
        type_="note", body="b", serves=[], now_iso="2026-06-25T00:00:00+00:00",
    )
    assert rows[0]["important"] is False
    assert rows[0]["note"] is None


def test_create_accepts_important_and_note():
    rows = build_create_rows(
        project_id="pid", project_code="p", label="Fred lens",
        type_="note", body="b", serves=[], now_iso="2026-06-25T00:00:00+00:00",
        important=True, note="the fork in the engagement",
    )
    assert rows[0]["important"] is True
    assert rows[0]["note"] == "the fork in the engagement"


def test_version_carries_forward_important_and_note_from_prior():
    prior = [{
        "version_label": "v1", "framing": "Fred lens", "layer": "Note",
        "serves": [], "important": True, "note": "the fork",
    }]
    rows = build_version_rows(
        project_id="pid", project_code="p", est_item_id="_authored/fred-lens",
        prior_versions=prior, body="v2 body", version_note="sharpened",
        now_iso="2026-06-26T00:00:00+00:00",
    )
    assert rows[0]["version_label"] == "v2"
    assert rows[0]["important"] is True          # carried forward
    assert rows[0]["note"] == "the fork"         # carried forward
```

**Step 2: Run — expect FAIL** (`TypeError: unexpected keyword 'important'`):
`uv run --dev python -m pytest tests/test_authored_element_importance.py -v`

**Step 3: Implement.** In `_row`, add params + emit columns:
```python
def _row(*, project_id, project_code, est_item_id, label, type_, body, serves,
         version_label, version_date, status, version_note=None, sources=None,
         important=False, note=None):  # PARITY: mirror in mc-2 authored_element
    return {
        ...                      # (existing keys unchanged)
        "rel_path": None,
        "important": bool(important),
        "note": note,
    }
```
In `build_create_rows`, add `important=False, note=None` params and pass them into `_row`.
In `build_version_rows`, carry forward from `base`:
```python
    base = prior_versions[0] if prior_versions else {}
    return [_row(
        ...,
        version_note=version_note,
        important=base.get("important", False),   # carry forward (decision A)
        note=base.get("note"),
    )]
```

**Step 4: Run — expect PASS.**

**Step 5: Commit:**
```bash
git add tests/test_authored_element_importance.py src/cp_engine/authored_element.py
git commit -m "feat(spine): carry important+note through authored row builders"
```

---

## Task 3: `add_spine_version` reads prior `important`/`note`

**Files:**
- Modify: `src/cp_engine/mcp_server.py:386` (the `_SEL` constant in `add_spine_version`)
- Test: `tests/test_spine_set_element.py` (create; shared with Task 5 — start it here)

**Why:** `build_version_rows` carries forward from `prior_versions[0]`, but `add_spine_version` fetches prior rows with an explicit `_SEL` that does **not** include the new columns — so carry-forward would silently reset them. Add `important, note` to `_SEL`.

**Step 1: Write the failing test** (asserts the select string includes the columns):

```python
# tests/test_spine_set_element.py
import cp_engine.mcp_server as m


def test_add_version_sel_includes_important_and_note():
    src = m.add_spine_version.__doc__  # sanity: tool exists
    assert src is not None
    # The _SEL constant is module-local to the function; assert via source.
    import inspect
    body = inspect.getsource(m.add_spine_version)
    assert "important" in body and "note" in body
```

**Step 2: Run — expect FAIL.**

**Step 3: Implement** — extend `_SEL`:
```python
    _SEL = ("id, est_item_id, est_item_kind, phase, binding, layer, placement, "
            "serves, version_label, version_date, status, framing, body, sources, "
            "origin, important, note")
```

**Step 4: Run — expect PASS.**

**Step 5: Commit:**
```bash
git add tests/test_spine_set_element.py src/cp_engine/mcp_server.py
git commit -m "feat(spine): add_spine_version carries important+note forward"
```

---

## Task 4: `list_spine` / `pull_spine` return new fields, sorted important-first

**Files:**
- Modify: `src/cp_engine/project_sources.py` — `_SPINE_LIST_COLUMNS` (222), `_SPINE_PULL_COLUMNS` (225), `list_spine` (231), `_spine_element` (317)
- Test: `tests/test_spine_importance_read.py` (create)

**Step 1: Write the failing test** (uses the fake-client pattern from `test_spine_read_mc2.py`):

```python
# tests/test_spine_importance_read.py
from cp_engine.project_sources import list_spine, pull_spine


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


def _row(eid, important):
    return {"est_item_id": eid, "framing": eid, "layer": "Note",
            "binding": "unbound", "status": "live", "serves": [], "body": "b",
            "important": important, "note": f"why {eid}"}


def test_list_spine_returns_important_and_sorts_important_first():
    captured = {}
    rows = [_row("a", False), _row("b", True), _row("c", False)]
    out = list_spine(_client(rows, captured), "pid")
    assert "*" not in captured["select"]
    assert "important" in captured["select"]
    assert out[0]["est_item_id"] == "b"          # important sorts first
    assert out[0]["important"] is True
    assert all("important" in r for r in out)


def test_pull_spine_returns_important_and_note():
    captured = {}
    rows = [_row("a", True)]
    el = pull_spine(_client(rows, captured), "pid", "a")
    assert el["important"] is True
    assert el["note"] == "why a"
```

**Step 2: Run — expect FAIL** (`KeyError`/sort wrong, and `important` not in select).

**Step 3: Implement:**
- `_SPINE_LIST_COLUMNS = "est_item_id, framing, layer, binding, status, serves, body, important, note"`
- Add `important, note` to `_SPINE_PULL_COLUMNS`.
- In `list_spine`, add `"important": bool(row.get("important")), "note": row.get("note")` to each `out` dict, then **stable-sort important-first** before returning:
  ```python
  out.sort(key=lambda r: not r["important"])   # True(1) before False — important first
  return out
  ```
  (Stable sort preserves the existing `layer` ordering within each group.)
- In `_spine_element`, add `"important": bool(row.get("important")), "note": row.get("note")`.

**Step 4: Run — expect PASS.** Then run the existing spine suite to confirm no regression:
`uv run --dev python -m pytest tests/test_spine_read_mc2.py tests/test_spine_substance_sync.py -q`

**Step 5: Commit:**
```bash
git add tests/test_spine_importance_read.py src/cp_engine/project_sources.py
git commit -m "feat(spine): surface important+note in list/pull, sort important-first"
```

---

## Task 5: New `set_spine_element` MCP tool (partial update of live row)

**Files:**
- Modify: `src/cp_engine/mcp_server.py` (add the tool after `add_spine_version`, ~line 412)
- Test: `tests/test_spine_set_element.py` (extend from Task 3)

**Contract:** `set_spine_element(project_code, key, important=None, note=None) -> dict`. `None` args are no-ops (partial update). Resolves `key` to the live element via the same exact-id / framing-substring discipline as `pull_spine`, then targeted-updates only the provided fields on the live row. Returns `{est_item_id, important, note}` or a structured note/error. Never throws.

**Step 1: Write the failing test:**

```python
# (append to tests/test_spine_set_element.py)
def test_set_spine_element_partial_update(monkeypatch):
    captured = {"updates": []}

    class _T:
        def __init__(self, n): captured["table"] = n
        def select(self, c): return self
        def eq(self, c, v): captured.setdefault("eqs", []).append((c, v)); return self
        def update(self, d): captured["updates"].append(d); return self
        def execute(self):
            return type("R", (), {"data": [
                {"est_item_id": "_authored/x", "framing": "X", "status": "live",
                 "important": False, "note": None, "id": "p/_authored/x/v1"}
            ]})()

    class _C:
        def table(self, n): return _T(n)

    monkeypatch.setattr("cp_engine.mcp_server._resolve",
                        lambda code: (_C(), "pid", "cid"))
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", important=True)
    # only `important` was written (note left untouched — partial update)
    assert any(u == {"important": True} for u in captured["updates"])
    assert res["important"] is True
```

**Step 2: Run — expect FAIL** (`AttributeError: module has no set_spine_element`).

**Step 3: Implement** (match the never-throw + resolve pattern of sibling tools):

```python
@mcp.tool()
def set_spine_element(project_code: str, key: str,
                      important: bool | None = None,
                      note: str | None = None) -> dict:
    """Set the `important` flag and/or standing `note` on a live spine element.

    `key` is an est_item_id (exact) or a case-insensitive title (`framing`)
    substring, resolved to ONE element (same discipline as pull_spine_element).
    Args left as None are not touched (partial update). Marking an element
    important surfaces it first in list_spine_elements and (item 3) promotes its
    source transcript to RAG. Returns {est_item_id, important, note}.
    """
    try:
        resolved = _resolve(project_code)
        if resolved is None:
            return {"error": f"project {project_code!r} not found"}
        client, pid, _cid = resolved
        # Resolve to one live row (reuse pull_spine's matching via a light read).
        from cp_engine.project_sources import _resolve_live_element  # see note
        row = _resolve_live_element(client, pid, key)
        if row is None or "note" in row.get("_status", {}):
            return {"note": f"no single live element matching '{key}'"}
        patch = {}
        if important is not None:
            patch["important"] = bool(important)
        if note is not None:
            patch["note"] = note
        if patch:
            client.table("spine_substance").update(patch).eq("id", row["id"]).execute()
        return {
            "est_item_id": row["est_item_id"],
            "important": patch.get("important", row.get("important")),
            "note": patch.get("note", row.get("note")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to set element '{key}' in {project_code!r}: {exc}"}
```

**Helper** — add a small `_resolve_live_element(client, project_id, key) -> dict | None` to `project_sources.py` that returns the single live row (with `id`) using the exact-id / single-distinct-framing logic already in `pull_spine` (refactor the matching out of `pull_spine` so both share it — DRY). Returns `None` on no-match or ambiguity.

**Step 4: Run — expect PASS.** Run full spine suite + the set test:
`uv run --dev python -m pytest tests/test_spine_set_element.py tests/test_spine_importance_read.py -v`

**Step 5: Commit:**
```bash
git add tests/test_spine_set_element.py src/cp_engine/mcp_server.py src/cp_engine/project_sources.py
git commit -m "feat(spine): set_spine_element MCP tool for important+note"
```

---

## Task 6: Full suite + lint, then request review

**Step 1:** `uv run --dev python -m pytest -q` — expect all green (baseline was ~1,500 passing).
**Step 2:** Run the project linter if configured (`ruff check src tests` — check `pyproject.toml`).
**Step 3:** REQUIRED SUB-SKILL: superpowers:requesting-code-review before any merge.

---

## Out of scope (tracked, not built here)

- **Dashboard ★ toggle + note field** (mc-2 frontend) — item 1's UI half; separate mc-2 PR.
- **mc-2 `authored_element` mirror parity** — re-sync the mirror + golden-vector test after this lands.
- **Items 2–5** (done-derived, promote-to-RAG, folder UI, ingest caching) — separate plans.
