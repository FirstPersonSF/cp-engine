# Ingest Caching — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make repeated `cp ingest-assets` runs fast by (a) skipping download + embed of UNCHANGED files using a per-file content change-token captured from the listing (before download), and (b) caching the folder listing in-process for back-to-back scans.

**Architecture:** Each listed `FileRef` carries a `change_token` = the provider's content hash (Drive `md5Checksum`, Dropbox `content_hash`), captured from the listing at zero extra API cost. At ingest, the token is stamped into `rag_assets.meta.change_token`. On a later run, before downloading a file, we look up the active rag_asset for `(project_id, source_file_id)`; if its stored `meta.change_token` equals the freshly-listed token, we SKIP (no download, no embed). The folder tree-walk is memoized in-process with a TTL (clock injected for tests). Item 5 of cp-enhancements; **cp-engine-only, NO migration** (`rag_assets.meta` jsonb already exists).

**Tech stack:** Python 3.12, `uv`, pytest (fake connectors/clients — the existing asset_ingest test patterns), Supabase (`rag_assets.meta` jsonb read/write).

**Design doc:** cp tenant `docs/plans/2026-06-25-cp-enhancements-design.md` §5 (this plan refines it: token in `rag_assets.meta`, NOT a new `ingest_scan_cache` table — the asset row can't drift from its own token, and no migration is needed; token is the provider CONTENT HASH, not a timestamp).

---

## Decisions pinned (refine the design doc)

- **Token = provider content hash** (Drive `md5Checksum`, Dropbox `content_hash`), NOT a modified-timestamp. Changes only on real content change; a touch/re-share/clock-skew won't force re-embed. Both available in the listing at zero extra API cost.
- **Stored in `rag_assets.meta.change_token`** (existing jsonb, nullable) — NOT a new `ingest_scan_cache` table. No migration; cp-engine-only; the token lives on the asset it describes so it can't drift.
- **Skip key:** `(project_id, source_file_id)` active row whose `meta.change_token` == the listed token → skip download + embed. The existing post-download SHA `file_hash` cross-path dedup STAYS as the content backstop (catches same-bytes-new-path).
- **In-process listing cache:** TTL-memoize the tree-walk per `(provider, folder_id)`; clock injected for testability. Process-local — dies with the process, never stale across runs.
- **`--no-cache` / `--force` bypass** for a full re-scan.

## Grounding (verified against current code + live DB, 2026-06-25)

- `FileRef` (asset_ingest.py:82-100) has source/id/name/mime_type/size/modified/path/folder_path — NO change_token yet.
- Drive listing `_list_drive` (288) requests `fields="files(id,name,mimeType,size,modifiedTime)"` (line 340) — `md5Checksum` is a standard field, addable to the mask at zero cost. Drive `modified` set at line 282.
- Dropbox listing `_list_dropbox` (364) builds FileRef at 394-401 from the entry; `content_hash` is on the entry object (`getattr(entry, "content_hash", None)`), not captured today.
- The ingest LOOP (asset_ingest.py:909+): for each file_ref → shortcut check → `_stable_dir_for` → `download_file` (924) → `_content_hash` + `_existing_dup_at_other_path` cross-path dedup (942) → `pipeline.ingest_file` (961) → `_stamp_scope` (984). **The pre-download skip goes right after the shortcut check, before `download_file`.**
- `rag_assets` (live): has `meta jsonb` (nullable), `source_provider`, `source_file_id`, `file_hash`, `status`, `project_id`/`initiative_id`. CONFIRMED via live query.
- `_stamp_scope` (~1078) UPDATEs rag_assets source_* by `(owner, file_path, status='active')` after ingest — the natural place to ALSO write `meta.change_token`.

---

## Context the executor needs

- **Test recipe (uv quirk):** `uv pip install -e . --force-reinstall -q` ONCE, then `uv run --no-sync --dev python -m pytest ...`. Never bare `pytest`. Revert `uv.lock` before committing.
- **No `SELECT *`.** Reads of rag_assets must select explicit columns (incl. `meta`).
- **Zero extra API calls** — the change token must come from the EXISTING listing response (Drive fields mask / Dropbox entry attr), never a per-file metadata fetch.
- **Backward-compatible:** a rag_asset with no `meta.change_token` (everything pre-item-5) must NOT match the skip (treat missing token as "unknown → don't skip → ingest normally"). First run after deploy re-ingests once, then subsequent runs skip.
- **Fail-open:** any error in the skip-check must fall through to normal ingest (never skip on a failed lookup, never crash the run). Mirror the existing dedup's collect-and-continue.

---

## Task 1: `FileRef.change_token` + capture it in both listings

**Files:** Modify `src/cp_engine/asset_ingest.py` (`FileRef`, `_list_drive` fields mask + line 282 area, `_list_dropbox` 394-401). Test: `tests/test_asset_ingest_change_token.py`.

**Step 1: Failing test** — feed fake Drive + Dropbox listing responses, assert the built FileRefs carry `change_token` from md5Checksum / content_hash:

```python
# tests/test_asset_ingest_change_token.py
from cp_engine.asset_ingest import _list_drive, _list_dropbox


class _DriveConn:
    # minimal stand-in for whatever _list_drive calls; returns one file with md5
    def __init__(self, files): self._files = files
    # adapt to the real connector interface _list_drive uses (read the function)


def test_drive_fileref_carries_md5_as_change_token(...):
    # list a drive folder whose file has md5Checksum="abc123"
    refs = _list_drive(conn, "root")
    assert refs[0].change_token == "abc123"


def test_dropbox_fileref_carries_content_hash_as_change_token(...):
    refs = _list_dropbox(conn, "/folder")
    assert refs[0].change_token == "deadbeef"
```
(READ `_list_drive`/`_list_dropbox` first to build the right fakes — mirror existing tests in `tests/test_asset_ingest_listing.py` if present.)

**Step 2: Run — FAIL** (no change_token field).

**Step 3: Implement:**
- Add `change_token: str | None = None` to `FileRef` (after `folder_path`, defaulted so other construction sites are unaffected).
- Drive: add `md5Checksum` to the `fields=` mask (line 340) → `fields="files(id,name,mimeType,size,modifiedTime,md5Checksum)"`; set `change_token=item.get("md5Checksum")` where the Drive FileRef is built (near line 282).
- Dropbox: set `change_token=getattr(entry, "content_hash", None)` in the FileRef built at 394-401.

**Step 4: Run — PASS** + full suite (existing listing tests must still pass — the new field is defaulted/optional).

**Step 5: Commit** (`feat(ingest): capture provider content-hash as FileRef.change_token`).

---

## Task 2: stamp `meta.change_token` at ingest

**Files:** Modify `src/cp_engine/asset_ingest.py` (`_stamp_scope` ~1078, and its call site at ~984 to pass the token). Test: extend `tests/test_asset_ingest_*` or a new `tests/test_asset_ingest_change_token_stamp.py`.

**Goal:** when stamping an ingested asset, also write `meta.change_token = file_ref.change_token` (merge into existing meta, don't clobber other meta keys).

**Subtlety — meta merge:** `meta` is jsonb and the pipeline may have written embedding/chunk keys into it. The stamp must MERGE (read-modify-write or a jsonb merge), not overwrite `meta` wholesale. Simplest correct approach: read the row's current `meta` in the same locate query the stamp already does, merge `{**(meta or {}), "change_token": token}`, write it back. (If `_stamp_scope` doesn't currently read meta, add `meta` to its select.)

**Step 1: Failing test** — fake client; after `_stamp_scope` with a file_ref carrying change_token="abc", assert the update payload's `meta` contains `change_token="abc"` AND preserves a pre-existing meta key.

**Step 2–4:** implement the merge; run + full suite.

**Step 5: Commit** (`feat(ingest): stamp meta.change_token on ingested assets`).

---

## Task 3: the pre-download skip check (pure helper)

**Files:** Modify `src/cp_engine/asset_ingest.py` (add a pure-ish `_unchanged_since_last_ingest(client, project_id, file_ref) -> bool`). Test: `tests/test_asset_ingest_skip.py`.

**Contract:** returns True ONLY when an ACTIVE rag_asset exists for `(project_id, source_file_id=file_ref.id)` whose `meta.change_token` equals `file_ref.change_token` AND `file_ref.change_token` is not None. Otherwise False (missing row, missing/mismatched token, None token → don't skip). Fail-open: any exception → False (ingest normally).

```python
def _unchanged_since_last_ingest(client, project_id, file_ref) -> bool:
    token = file_ref.change_token
    if not token:
        return False                      # no token → can't prove unchanged
    try:
        rows = (client.table("rag_assets")
                .select("meta")            # explicit; never *
                .eq("project_id", project_id)
                .eq("source_file_id", file_ref.id)
                .eq("status", "active")
                .limit(1).execute().data or [])
        if not rows:
            return False
        stored = (rows[0].get("meta") or {}).get("change_token")
        return stored is not None and stored == token
    except Exception:                      # fail-open: never skip on error
        return False
```

**Step 1: Failing tests** (fake client): match → True; no row → False; token mismatch → False; None token → False; stored-token-missing → False; client raises → False (fail-open).

**Step 2–4:** implement; run + full suite.

**Step 5: Commit** (`feat(ingest): _unchanged_since_last_ingest pre-download skip check`).

---

## Task 4: wire the skip into the ingest loop + `--no-cache`

**Files:** Modify `src/cp_engine/asset_ingest.py` (the loop ~909, `ingest_project_assets` signature for a `use_cache` flag) + `src/cp_engine/asset_ingest_cli.py` (the `--no-cache`/`--force` flag). Test: `tests/test_asset_ingest_skip_loop.py`.

**Behavior:** in the loop, right AFTER the shortcut-extension check and BEFORE `download_file`:
```python
if use_cache and _unchanged_since_last_ingest(client, folders.project_id, file_ref):
    result.skipped_unchanged += 1     # new IngestRunResult counter
    continue
```
- Add `skipped_unchanged: int = 0` to `IngestRunResult`.
- `ingest_project_assets(..., use_cache: bool = True)`; thread through. CLI `--no-cache` (or `--force`) sets `use_cache=False` for a full re-scan.
- When skipped: NO download, NO hash, NO embed — the whole point.

**Step 1: Failing tests:** loop with a file whose token matches an active row → skipped_unchanged incremented, download NOT called (inject a fake download that records calls / raises if called). Changed token → NOT skipped (download called). `use_cache=False` → never skipped even on match.

**Step 2–4:** implement; run + full suite.

**Step 5: Commit** (`feat(ingest): skip unchanged files pre-download; --no-cache bypass`).

---

## Task 5: in-process folder-listing cache (TTL, injected clock)

**Files:** Modify `src/cp_engine/asset_ingest.py` (wrap the tree-walk seam — `list_files` ~453 or the `_list_drive`/`_list_dropbox` calls). Test: `tests/test_asset_ingest_listing_cache.py`.

**Contract:** memoize the listing per `(source_provider, folder_id)` for a TTL (default 600s). Process-local dict. Clock injected (`now: Callable[[], float] = time.monotonic`) so tests control expiry — do NOT call an arg-less clock that the harness forbids; accept a `now` parameter / module-level injectable.

**Design:** a small `_ListingCache` (or module dict) keyed by `(provider, folder_id)` → `(monotonic_timestamp, refs)`. On lookup: if present and `now() - ts < ttl` → return cached refs; else recompute via the real lister and store. Provide a `clear()` for tests and a way to disable (ttl=0 → never cache). Wire it at the seam where `_list_drive`/`_list_dropbox` are invoked, gated by the same `use_cache` flag.

**Step 1: Failing tests:** two back-to-back lookups within TTL → underlying lister called ONCE (memoized); after advancing the injected clock past TTL → called again; ttl=0 / use_cache=False → called every time; different folder_id → separate entries.

**Step 2–4:** implement with injected clock; run + full suite.

**Step 5: Commit** (`feat(ingest): in-process TTL cache for folder listings`).

---

## Task 6: docstrings + full suite + whole-item review

- Document the caching behavior on `ingest_project_assets` (skip semantics, `--no-cache`, the token source, first-run-after-deploy re-ingests once).
- Full suite green.
- Whole-item review: trace the end-to-end — first run ingests + stamps tokens; second run lists (cached) + skips unchanged; an edited file (new content hash) re-ingests; `--no-cache` forces full. Confirm fail-open everywhere and that the existing SHA cross-path dedup still works alongside.
- REQUIRED SUB-SKILL: superpowers:requesting-code-review.

---

## Out of scope (tracked, not built here)

- **A dedicated `ingest_scan_cache` table** — superseded by `rag_assets.meta.change_token` (no migration, no drift).
- **Drive `version` / Dropbox `rev` as the token** — content hash chosen (skips on content, not revision churn). Either is available if hash ever proves insufficient.
- **Cross-process / persistent listing cache** — the listing cache is deliberately in-process/TTL (a scan is a single process); persistent listing caching is unnecessary.
- **Invalidation beyond token mismatch + archived check** — token mismatch and the `status='active'` filter cover edits and archival; no explicit purge needed.
