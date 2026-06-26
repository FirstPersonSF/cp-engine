# Folder Management — Targeted Scan — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a user scan ONE configured ingest folder instead of all of them — an `only_folder` that narrows (never widens) the existing per-project allowlist, threaded end-to-end: cp-engine `ingest_project_assets` + `cp ingest-assets --folder` + the ingest webhook → mc-2 backend proxy → a folder dropdown on the existing "Ingest assets" button.

**Architecture:** `only_folder` is applied as an ADDITIONAL narrowing on top of `projects.asset_ingest_folders`: the effective allowlist for a scan is `only_folder ∩ configured-allowlist` (if `only_folder` isn't in the allowlist → scans nothing; preserves the scope guard). cp-engine changes land on PR #17 (branch `feature/spine-importance-note`); mc-2 changes pair with PR #102 (branch `feature/spine-importance-note` in mc-2). Item 4 of cp-enhancements.

**Tech stack:** Python 3.12 / `uv` / pytest (cp-engine + webhook), FastAPI (webhook + mc-2 backend proxy), React/TS (mc-2 frontend), Click (CLI).

**Design doc:** cp tenant `docs/plans/2026-06-25-cp-enhancements-design.md` §4 (this plan refines it: the folder-list EDITOR already exists — `AssetIngestFolders.tsx` — so item 4 is ONLY the targeted scan; UX = a folder dropdown on the existing Ingest button; semantics = narrow-only).

---

## Decisions pinned

- **Targeted scan only.** The folder-list add/remove editor (`AssetIngestFolders.tsx`) and the `PATCH /api/jobs/{id}` persistence of `asset_ingest_folders` ALREADY EXIST and work. Item 4 does NOT rebuild them.
- **UX:** a folder dropdown on the existing "Ingest assets" button — pick a folder → scan only it; blank → scan all (today's behavior). Additive to the existing button.
- **Narrow-only semantics:** `only_folder` further restricts the configured allowlist; it can NEVER reach a folder the project isn't configured to ingest. If `only_folder` matches nothing in the allowlist → scans nothing (safe). Preserves the scope guard.
- **Spans both repos:** cp-engine (core + CLI + webhook) on PR #17; mc-2 (backend proxy + frontend dropdown) on PR #102.

## Grounding (verified against the worktree + mc-2, 2026-06-25)

- `ingest_project_assets(project_code, *, mc_project_id=, client=, drive_connector=, dropbox_connector=, tmp_root=, pipeline=, pipeline_factory=, supabase_url=, supabase_key=, use_cache=True)` — asset_ingest.py:907 (worktree; item-5's `use_cache` present). Calls `list_files(..., allowlist=folders.asset_ingest_folders, ..., use_cache=use_cache)` at :1020.
- `list_files(folders, drive_connector=, dropbox_connector=, allowlist=(), *, use_cache=True)` — :530. Applies `_matches_allowlist(ref, allowlist)` (:509, segment-contains, case-insensitive) to filter; empty allowlist `()` = no filter (ingest all).
- `cp ingest-assets` CLI — cli.py:2728 (`ingest_assets_cmd`), flags `--all/--scope/--no-cache`; single-project call at :2772 `ingest_project_assets(code, use_cache=use_cache)`; `--all` via `asset_ingest_cli.fan_out_ingest`.
- Webhook ingest: cp-engine `webhook/main.py` — `/api/assets/ingest` handler (~648) accepts `{code, run_id, mc_project_id}`, spawns `_run_asset_ingest` (~583) which calls `ingest_project_assets(code, mc_project_id=, supabase_url=, supabase_key=)`.
- mc-2 backend proxy: `backend/src/routers/asset_ingest.py` (~176) `IngestAssetsBody{mc_project_id?}` → POSTs to the cp-engine webhook.
- mc-2 frontend: `IngestAssetsButton.tsx` (~43 calls `ingestAssets(code, mcProjectId)`); `AssetIngestFolders.tsx` (the existing folder-list editor); `lib/api.ts` `ingestAssets`. Folder list is on `job.asset_ingest_folders`.

---

## Context the executor needs

- **cp-engine test recipe (uv quirk):** `uv pip install -e . --force-reinstall -q` ONCE, then `uv run --no-sync --dev python -m pytest ...`. Revert `uv.lock` before committing.
- **mc-2 tests:** backend uses `./venv/bin/python -m pytest` from `mc-2/backend` (the venv at `backend/venv`); frontend uses vitest (check `frontend/package.json`). The mc-2 work happens in the mc-2 repo on branch `feature/spine-importance-note` (where migration 082 + the mirror live), NOT the cp-engine worktree.
- **No `SELECT *`.** **Narrow-only** is the load-bearing invariant — test that `only_folder` outside the allowlist scans nothing.

---

# PART 1 — cp-engine (lands on PR #17, in the worktree)

## Task 1: `only_folder` narrows the allowlist in `ingest_project_assets`

**Files:** Modify `src/cp_engine/asset_ingest.py` (`ingest_project_assets` signature + the `list_files` call ~1020). Test: `tests/test_asset_ingest_only_folder.py`.

**Approach:** Add `only_folder: str | None = None` to `ingest_project_assets`. Compute the effective allowlist passed to `list_files`:
```python
allowlist = folders.asset_ingest_folders
if only_folder is not None:
    # Narrow, never widen: only_folder must itself be permitted by the configured
    # allowlist. Empty configured allowlist means "all allowed", so only_folder
    # alone applies. A non-empty allowlist that doesn't permit only_folder →
    # empty effective allowlist → matches NOTHING (scope guard preserved).
    if not folders.asset_ingest_folders:
        allowlist = (only_folder,)
    elif _folder_permitted(only_folder, folders.asset_ingest_folders):
        allowlist = (only_folder,)
    else:
        allowlist = ("\0__none__",)   # sentinel that matches nothing → scans nothing
```
Use the SAME matching discipline as `_matches_allowlist` for `_folder_permitted` (does `only_folder` contains-match, or is contained by, an allowed name — pick the rule consistent with how `_matches_allowlist` treats allowlist entries; READ `_matches_allowlist` and mirror it so "Carol" picks the "Carol Decks" allowed entry the same way the file-matching does). Prefer a clean helper over the sentinel if you find a cleaner "match nothing" path (e.g. pass a flag to skip — but keep `list_files`'s signature stable; the narrowed-allowlist approach needs no `list_files` change).

NOTE: `_matches_allowlist` already treats `()` as "match all", so you can't express "match none" with an empty tuple. The sentinel (an allowlist entry no real folder segment contains) is the simplest "match nothing". Document it.

**Step 1: Failing tests** (drive `ingest_project_assets` with injected fakes mirroring `test_asset_ingest_skip_loop.py`, OR test a smaller extracted `_effective_allowlist(only_folder, configured)` pure helper — PREFER extracting a pure helper for testability):
- `only_folder=None` → effective allowlist == configured (unchanged).
- `only_folder="Carol Decks"`, configured `("Carol Decks","Client Assets")` → effective `("Carol Decks",)`.
- `only_folder="Carol"`, configured `("Carol Decks",)` → effective narrows to Carol Decks (mirror _matches_allowlist's contains rule).
- `only_folder="Secret"`, configured `("Carol Decks",)` → effective matches NOTHING (scope guard).
- `only_folder="Anything"`, configured `()` (empty=all) → effective `("Anything",)` (only_folder alone applies).

**Step 2–4:** extract `_effective_allowlist`, wire it before the `list_files` call, run tests + full suite.

**Step 5: Commit** (`feat(ingest): only_folder narrows the allowlist (targeted scan, never widens)`).

## Task 2: `cp ingest-assets --folder` CLI flag

**Files:** Modify `src/cp_engine/cli.py` (`ingest_assets_cmd`). Test: extend the CLI tests.

Add `--folder TEXT` (default None) to the single-project path → `ingest_project_assets(code, use_cache=use_cache, only_folder=folder)`. `--folder` with `--all` → error (targeted scan is single-project only; echo + exit 2). Help text: "Scan only this configured folder (narrows the allowlist; must be a configured ingest folder)."

**Tests:** `--folder X` passes only_folder=X to the single-project call; `--folder` + `--all` → exit 2. Run + full suite.

**Commit** (`feat(cli): ingest-assets --folder for a targeted single-folder scan`).

## Task 3: webhook accepts `folder` and passes it through

**Files:** Modify `webhook/main.py` (the `/api/assets/ingest` payload model + `_run_asset_ingest`). Test: webhook tests (TestClient).

Add optional `folder: str | None = None` to the ingest request body; thread it into `_run_asset_ingest(run_id, code, mc_project_id, folder)` → `ingest_project_assets(code, ..., only_folder=folder)`. Backward-compatible (absent folder → None → scan all).

**Tests:** POST with `folder` → ingest called with only_folder; POST without → only_folder None. Run webhook tests + full suite.

**Commit** (`feat(webhook): accept folder param for targeted ingest`).

## Task 4 (cp-engine): docstrings + whole-cp-engine-side review, then push to PR #17

- Document `only_folder` on `ingest_project_assets` (narrow-only, scope guard).
- Full suite green; review the cp-engine half end-to-end (CLI → only_folder → effective allowlist → list_files; webhook → only_folder). Confirm narrow-only holds (the "scans nothing" path).
- Push to PR #17; comment the item-4 cp-engine half.

---

# PART 2 — mc-2 (pairs with PR #102, in the mc-2 repo)

> Switch to the mc-2 repo, branch `feature/spine-importance-note`. Tests: `cd mc-2/backend && ./venv/bin/python -m pytest`; frontend vitest.

## Task 5: mc-2 backend proxy forwards `folder`

**Files:** Modify `mc-2 backend/src/routers/asset_ingest.py` (`IngestAssetsBody` + the POST to the webhook). Test: backend test.

Add `folder: str | None = None` to `IngestAssetsBody`; include it in the JSON POSTed to the cp-engine webhook's `/api/assets/ingest`. Backward-compatible.

**Tests:** body with folder → forwarded in the webhook payload; without → omitted/None. Run the touched backend suite.

**Commit (mc-2)** (`feat(asset-ingest): forward folder param to the ingest webhook`).

## Task 6: frontend folder dropdown on the Ingest button

**Files:** Modify `mc-2 frontend/src/components/jobs/overview/IngestAssetsButton.tsx` + `lib/api.ts` (`ingestAssets` signature). Test: vitest if the component has tests.

- `ingestAssets(code, mcProjectId, folder?)` — pass `folder` in the POST body.
- The button gets a dropdown/select of the project's `asset_ingest_folders` (passed in as a prop from the overview page, which already has `job.asset_ingest_folders`) + an "All folders" default (folder=undefined). Selecting a folder then clicking Ingest scans only it.
- Keep the existing fire-and-forget + status-poll behavior unchanged.

**Tests:** selecting a folder calls `ingestAssets` with that folder; default calls with no folder. tsc clean.

**Commit (mc-2)** (`feat(jobs): folder dropdown on Ingest button for targeted scan`).

## Task 7 (mc-2): wire the dropdown's folder list + whole-mc-2-side review, push to PR #102

- The overview page (`jobs/[id]/overview/page.tsx`) passes `job.asset_ingest_folders` to `IngestAssetsButton` as the dropdown options (it already passes them to `AssetIngestFolders`).
- Frontend tests + tsc green; review the mc-2 half (dropdown → api → proxy → webhook payload).
- Push to PR #102; comment the item-4 mc-2 half. Cross-link PR #17.

---

## Out of scope (tracked, not built here)

- **Folder-list editor / add-remove** — already exists (`AssetIngestFolders.tsx`).
- **A real folder PICKER from live Drive/Dropbox subfolders** — the dropdown lists CONFIGURED `asset_ingest_folders` (free-text the user already added), not a live folder browse. The design noted the live picker as post-MVP.
- **Per-chip scan action** — UX chose the dropdown-on-button instead.
- **`--folder` with `--all`** — disallowed (targeted scan is single-project).
