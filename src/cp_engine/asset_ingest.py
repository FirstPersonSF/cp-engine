"""1P asset-ingest glue — resolve a project's cloud folders and list their files.

This is the first half of the asset-ingest pipeline (Task C3). It does two
things and nothing more:

1. `resolve_project_folders` — given a cp project code (`ibx-5153`), look up the
   matching MC-2 `projects` row by its numeric part and return the Drive /
   Dropbox folder identifiers + enable flags + the owning company's `kind`.

2. `list_files` — given those folders, enumerate the files in the Drive folder
   and/or Dropbox folder, normalized into `FileRef`s. Connectors are injectable
   so the heavy `cloud_storage` clients (and their network/credential setup) can
   be mocked in tests.

`ingest_project_assets` (Task C5) is the run loop that ties resolve → list →
download → document-ingest pipeline (with the Voyage embedder) together and
applies the 1P scope stamp to each freshly-written `rag_assets` row. Two caches
keep re-runs cheap: a per-folder in-process listing cache (shared across an
`--all` sweep) and a per-file ingest skip — files whose provider content token
matches the one stamped into `meta.change_token` on a prior run are skipped
before download. `--no-cache` (`use_cache=False`) disables both.

Scope guard: asset ingest targets *client* companies and *initiatives*
(mc-2 #192). Non-client ENGAGEMENT owners (self-fpsf / self-canonic projects)
are house/framework territory and out of scope; `list_files` returns `[]` for
them. Initiatives — which also live under self-* companies — are explicitly
allowed via `ProjectFolders.is_initiative` and write `initiative_id`-owned
rag_assets rows.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cp_engine.mc2_db import Tables

# Columns we read from MC-2's `projects` table. Explicit list (never `*`) per
# Drew's global Supabase rule — the projects table carries multi-megabyte JSONB
# cache columns (`cached_messages`, `cached_analysis`) we must not pull. The
# nested `companies(kind)` embed gives us the scope guard (client vs self-*).
# Folder ids (google_drive_folder_id / mc_dropbox_folder_id) come from
# project_integrations bindings (read-flip) — hydrated onto the row by the
# resolve functions before `_row_to_folders` maps it.
_PROJECT_COLUMNS = (
    "id, company_id, "
    "enable_google_drive, enable_dropbox, asset_ingest_folders, companies(kind)"
)

# Columns we read from MC-2's `initiatives` table (the initiative resolve
# path, mc-2 #192). Initiatives carry NO per-source enable flags and NO
# asset_ingest_folders column — a source is "configured" iff its
# initiative-owned binding exists (hydrated by `_hydrate_initiative`).
# Explicit list, never `*`.
_INITIATIVE_COLUMNS = "id, company_id, companies(kind)"


@dataclass
class ProjectFolders:
    """The cloud-folder coordinates for one MC-2 project."""

    project_id: str
    company_id: str
    company_kind: str
    google_drive_folder_id: str | None
    mc_dropbox_folder_id: str | None
    enable_google_drive: bool
    enable_dropbox: bool
    # Per-project folder allowlist: folder-name strings a file's ancestry must
    # CONTAIN to be ingested (see `_matches_allowlist`). NULL / missing / [] →
    # `()` → no filter (ingest the whole tree, today's behavior). Defaults to ()
    # so existing construction sites are unaffected.
    asset_ingest_folders: tuple[str, ...] = ()

    # Owner KIND. When True, `project_id` is actually an `initiatives.id` and the
    # rag_assets row must be owned via `initiative_id` (migration 081 CHECK:
    # exactly one of project_id/initiative_id). Defaults to False so every
    # existing engagement construction site is unaffected. See `_owner_filter`.
    #
    # Set True by the initiatives-table resolve path (mc-2 #192):
    # `resolve_project_folders` falls back to the `initiatives` table for slug
    # codes, and `resolve_project_folders_by_id` falls back on a `projects.id`
    # miss — both via `_initiative_row_to_folders`, which hydrates folder ids
    # from initiative-owned `project_integrations` bindings.
    is_initiative: bool = False


@dataclass
class FileRef:
    """A single file discovered in a project's Drive or Dropbox folder.

    `path` is the Dropbox path_display (used by the later download step to fetch
    by path); it is `None` for Drive files, which are fetched by `id`.
    """

    source: str  # 'drive' | 'dropbox'
    id: str
    name: str
    mime_type: str | None
    size: int | None
    modified: str | None
    path: str | None = None  # dropbox path_display; None for drive
    # Ordered folder NAMES from the project root down to (but NOT including) the
    # file itself. Recorded so a later per-project folder allowlist can match a
    # file when any folder in its path matches an allowed name. Defaults to ()
    # so other FileRef construction sites are unaffected.
    folder_path: tuple[str, ...] = ()
    # Per-file CONTENT change-token captured FROM THE LISTING at zero extra API
    # cost: Drive `md5Checksum`, Dropbox `content_hash`. Lets a later ingest-cache
    # step skip unchanged files before download. None when the provider omits it.
    # Defaulted so all other FileRef construction sites stay valid.
    change_token: str | None = None


def file_selection_key(ref: FileRef) -> str:
    """A stable per-file key the picker uses to select files for a scoped
    ingest. Drive files are fetched by `id`; Dropbox files by `path` — so the
    key is the path for Dropbox (its download coordinate) and the id for Drive.
    Kept in ONE place so the listing endpoint (which stamps this onto each
    returned file) and the `only_file_ids` filter agree on the key space.
    """
    if ref.source == "dropbox" and ref.path:
        return ref.path
    return ref.id


# ──────────────────────────────────────────────────────────────────────
#  Resolve
# ──────────────────────────────────────────────────────────────────────


def _project_number(project_code: str) -> int | None:
    """Extract the trailing numeric part of a cp code (`ibx-5153` → 5153).

    Returns None when the code carries no number — initiatives use slug codes
    with no number and standalone repos are bare slugs; neither maps to a
    `projects.number`. Slug codes fall through to the initiatives-table
    resolve path in `resolve_project_folders`.
    """
    if not project_code:
        return None
    match = re.search(r"(\d+)", project_code)
    return int(match.group(1)) if match else None


def _company_kind(row: dict) -> str:
    """The owning company's `kind` from a `companies(kind)` embed.

    PostgREST returns the to-one `companies` embed as either a dict or a
    single-element list (shape varies); defend against both — see the same
    guard in sync_mc2._engagement_canonical_id / _repo_row_to_state.
    """
    companies = row.get("companies") or {}
    if isinstance(companies, list):
        companies = companies[0] if companies else {}
    return (companies.get("kind") if isinstance(companies, dict) else "") or ""


def _row_to_folders(row: dict) -> ProjectFolders:
    """Map one MC-2 `projects` row (selected via `_PROJECT_COLUMNS`) to a
    ProjectFolders. Shared by both resolve paths (by-number and by-id) so the
    companies-embed guard + field mapping live in exactly one place.

    ENGAGEMENT construction site only — it never sets `is_initiative` (defaults
    False). Initiative rows map through `_initiative_row_to_folders` instead."""
    company_kind = _company_kind(row)

    return ProjectFolders(
        project_id=row.get("id"),
        company_id=row.get("company_id"),
        company_kind=company_kind,
        google_drive_folder_id=row.get("google_drive_folder_id") or None,
        mc_dropbox_folder_id=row.get("mc_dropbox_folder_id") or None,
        enable_google_drive=bool(row.get("enable_google_drive")),
        enable_dropbox=bool(row.get("enable_dropbox")),
        # `row.get(...) or ()` is NULL-safe AND forward-deploy-safe: a NULL value,
        # an empty array, OR a MISSING key (the mc-2 column lands in a later
        # migration) all collapse to `()` → no filter. So cp-engine can ship
        # before the column exists without crashing.
        #
        # `.strip()`-and-drop-empties defends the SERVICE BOUNDARY: cp-engine
        # takes whatever is in the DB column and must NOT assume mc-2 sanitized
        # it. An empty/whitespace-only allowed name is a CRITICAL footgun —
        # `"" in seg` is always True in `_matches_allowlist`, so a stored
        # ['Client Assets', ''] would silently match EVERY file and re-ingest the
        # whole tree, defeating the filter. Dropping empties here kills that; the
        # strip also fixes a surprising silent no-match where a padded
        # ' Client Assets ' failed to match folder 'Client Assets'. After this,
        # ['Client Assets', ''] → ('Client Assets',) and [' Client Assets '] →
        # ('Client Assets',). NULL/[]/missing still → ().
        asset_ingest_folders=tuple(
            s.strip() for s in (row.get("asset_ingest_folders") or ()) if s and s.strip()
        ),
    )


def resolve_project_folders(client, project_code: str) -> ProjectFolders | None:
    """Look up the Drive/Dropbox folders for `project_code` from MC-2.

    `client` is a Supabase client (the same `create_client(url, key)` MC2Backend
    uses). Returns None when nothing matches in either table.

    Resolution order:
      1. codes with a numeric part resolve by `projects.number` (engagements);
      2. slug codes (no number) — and numbered codes that miss the projects
         table — fall back to the `initiatives` table by `code` (mc-2 #192),
         mirroring the cp-sources MCP resolver's initiative fallback. The
         fallback-on-miss also covers initiative slugs that happen to embed a
         digit (`web3-lab`), which `_project_number` would otherwise mis-parse.

    NOTE: the by-number branch works for bare-numeric / `<co>-<number>` codes,
    but NOT for slug-style engagement codes (`SAP-vision-update-2026`) where a
    year embedded in the slug would be mis-parsed as the project number. When
    the caller has the MC-2 row id (e.g. the mc-2 button), prefer
    `resolve_project_folders_by_id`, which is authoritative.
    """
    number = _project_number(project_code)
    if number is None:
        # Slug code — not a numbered engagement; try the initiatives table.
        return _resolve_initiative_folders(client, project_code)

    rows = (
        client.table(Tables.PROJECTS)
        .select(_PROJECT_COLUMNS)
        .eq("number", number)
        .execute()
        .data
        or []
    )
    if not rows:
        # No engagement with that number — the "number" may be a digit inside
        # an initiative slug; try the initiatives table by full code.
        folders = _resolve_initiative_folders(client, project_code, quiet=True)
        if folders is not None:
            return folders
        print(
            f"[asset-ingest] no MC-2 project with number={number} "
            f"(code '{project_code}') and no initiative with that code",
            file=sys.stderr,
        )
        return None

    return _row_to_folders(_hydrate(client, rows[0]))


def _resolve_initiative_folders(
    client, code: str, *, quiet: bool = False
) -> ProjectFolders | None:
    """Look up an INITIATIVE's Drive/Dropbox folders by its slug code.

    Initiatives live in their own `initiatives` table (parallel to projects,
    slug codes like `mission-control`). Folder coordinates come from
    initiative-owned `project_integrations` bindings, hydrated onto the row
    before mapping. Returns None (with a stderr note unless `quiet`) when no
    initiative matches.
    """
    if not code:
        return None
    rows = (
        client.table(Tables.INITIATIVES)
        .select(_INITIATIVE_COLUMNS)
        .eq("code", code)
        .execute()
        .data
        or []
    )
    if not rows:
        if not quiet:
            print(
                f"[asset-ingest] no MC-2 initiative with code '{code}'",
                file=sys.stderr,
            )
        return None
    return _initiative_row_to_folders(_hydrate_initiative(client, rows[0]))


def _initiative_row_to_folders(row: dict) -> ProjectFolders:
    """Map one MC-2 `initiatives` row (selected via `_INITIATIVE_COLUMNS` and
    bindings-hydrated) to a ProjectFolders with `is_initiative=True`.

    Initiatives have NO per-source enable flags (there is no
    enable_google_drive/enable_dropbox column on the table): a source is
    configured iff its binding exists. Both enable flags are therefore set
    True so `folders_unconfigured_reason` reads a missing binding as
    "enabled but folder not set" — gating an unconfigured initiative exactly
    like an unconfigured client project rather than silently passing.
    `asset_ingest_folders` has no initiative column either → `()` → no filter.
    """
    return ProjectFolders(
        project_id=row.get("id"),
        company_id=row.get("company_id"),
        company_kind=_company_kind(row),
        google_drive_folder_id=row.get("google_drive_folder_id") or None,
        mc_dropbox_folder_id=row.get("mc_dropbox_folder_id") or None,
        enable_google_drive=True,
        enable_dropbox=True,
        asset_ingest_folders=(),
        is_initiative=True,
    )


def resolve_project_folders_by_id(
    client, mc_project_id: str
) -> ProjectFolders | None:
    """Look up the Drive/Dropbox folders for an MC-2 project by its row `id`.

    This is the authoritative resolution path: `mc_project_id` is `projects.id`,
    so there is no number-parsing and no risk of mis-reading a year embedded in
    a slug code (`SAP-vision-update-2026`). The mc-2 "Ingest assets" button
    already has this id; prefer it over `resolve_project_folders` (by-number)
    whenever available.

    On a `projects.id` miss, falls back to the `initiatives` table by id
    (mc-2 #192) — the initiative workspace's button passes `initiatives.id`
    through the same field. Returns None when neither table matches.
    """
    rows = (
        client.table(Tables.PROJECTS)
        .select(_PROJECT_COLUMNS)
        .eq("id", mc_project_id)
        .execute()
        .data
        or []
    )
    if not rows:
        init_rows = (
            client.table(Tables.INITIATIVES)
            .select(_INITIATIVE_COLUMNS)
            .eq("id", mc_project_id)
            .execute()
            .data
            or []
        )
        if init_rows:
            return _initiative_row_to_folders(
                _hydrate_initiative(client, init_rows[0])
            )
        print(
            f"[asset-ingest] no MC-2 project or initiative with "
            f"id={mc_project_id}",
            file=sys.stderr,
        )
        return None

    return _row_to_folders(_hydrate(client, rows[0]))


def _hydrate(client, row: dict) -> dict:
    """Overlay bindings-derived folder ids (read-flip) onto the project row."""
    from cp_engine.mc2_bindings import fetch_binding_rows, hydrate_project_row

    grouped = fetch_binding_rows(client, project_ids=[row["id"]])
    return hydrate_project_row(row, grouped.get(row["id"], []))


def _hydrate_initiative(client, row: dict) -> dict:
    """Overlay bindings-derived folder ids onto an initiatives row (#192)."""
    from cp_engine.mc2_bindings import fetch_binding_rows, hydrate_initiative_row

    grouped = fetch_binding_rows(client, initiative_ids=[row["id"]])
    return hydrate_initiative_row(row, grouped.get(row["id"], []))


def folders_unconfigured_reason(folders: ProjectFolders) -> str | None:
    """The #59 confirm-gate predicate: WHY this project would ingest nothing.

    Returns None (configured — proceed) when at least one ENABLED source has a
    folder id. Returns a human-readable reason when no enabled source does —
    i.e. both folder columns are NULL/empty, every enabled source's folder is
    NULL, or no source is enabled at all. A DISABLED source's NULL folder is
    NOT a gap (that source was never going to list), so drive-on+configured
    with dropbox-off+NULL passes cleanly.

    Non-client companies return None: asset ingest skips them wholesale via
    `list_files`' existing kind guard, and a "no folder configured" message
    would misdiagnose that case (folders aren't the reason nothing ingests).
    EXCEPTION: initiatives (`is_initiative=True`) are ingestable despite their
    self-* company kind (mc-2 #192), so the gate applies to them exactly as it
    does to a client project — an initiative with no folder bindings must
    refuse visibly, not silently pass.

    Deliberately NOT URL/path validation — a present-but-wrong folder id
    surfaces as a per-source listing failure note, exactly as before.
    """
    if folders.company_kind != "client" and not folders.is_initiative:
        return None
    if folders.enable_google_drive and folders.google_drive_folder_id:
        return None
    if folders.enable_dropbox and folders.mc_dropbox_folder_id:
        return None

    def _state(enabled: bool, folder_id: str | None) -> str:
        if not enabled:
            return "disabled"
        return "enabled but folder not set" if not folder_id else "configured"

    return (
        "no Drive/Dropbox folder configured "
        f"(drive: {_state(folders.enable_google_drive, folders.google_drive_folder_id)}; "
        f"dropbox: {_state(folders.enable_dropbox, folders.mc_dropbox_folder_id)})"
    )


# ──────────────────────────────────────────────────────────────────────
#  List
# ──────────────────────────────────────────────────────────────────────


def _coerce_size(v) -> int | None:
    """Drive returns `size` as a string; Dropbox as an int. Normalize to int."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Drive folders carry this mimeType; everything else is a leaf file.
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

# Recursion safety rail for the Drive tree-walk. Real client folder trees are a
# handful of levels deep; 10 is generous headroom. The cap (alongside a visited-
# set of folder ids) guarantees termination even on a pathological/cyclic tree —
# Drive shortcuts can introduce loops. Hitting it is surfaced to stderr, never a
# silent truncation.
_DRIVE_MAX_DEPTH = 10

# Shortcut / pointer files carry no ingestable content (a .url is a link, not a
# document). Skip them before download so they don't churn the pipeline or get
# miscounted as ingest "failures".
_NON_INGESTABLE_EXTENSIONS = (".url", ".lnk", ".webloc")


# ──────────────────────────────────────────────────────────────────────
#  In-process TTL listing cache (the SECOND ingest cache)
# ──────────────────────────────────────────────────────────────────────
#
# The first cache (Tasks 1-4) is a PRE-DOWNLOAD skip: per file, don't re-download
# bytes unchanged since last ingest. This second cache is coarser and earlier — it
# memoizes the whole folder LISTING (the recursive tree-walk) so back-to-back
# scans of the SAME folder within one process don't re-walk it.
#
# Module-level + TTL'd: a single `cp ingest-assets` invocation is one process, so
# this collapses repeated walks of the SAME folder WITHIN one invocation. The big
# win is `--all`: when several projects share a parent (provider, folder_id), the
# tree-walk runs ONCE and every later project reuses the cached listing. The clear
# lives at the CLI entry points (`fan_out_ingest` once before its loop, and the
# single-project branch) — NOT inside `ingest_project_assets`, which runs once per
# project, so clearing there would wipe the cache between every project and defeat
# cross-project sharing. Net effect: clean per CLI invocation, shared across the
# projects of that invocation, NEVER persisted across separate invocations.
_LISTING_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_LISTING_TTL_SECONDS = 600.0


def _cached_listing(
    provider: str,
    folder_id: str,
    lister: Callable[[], list],
    *,
    ttl: float = _LISTING_TTL_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> list:
    """Return a memoized listing for `(provider, folder_id)`.

    Recomputes via `lister()` when the key is absent or its cached entry is older
    than `ttl`. `now`/`ttl` are injectable for deterministic tests. Pass `ttl=0`
    (or negative) to disable caching entirely — always recompute, never store.

    The default `now` is `time.monotonic` (a relative, monotonically-increasing
    clock) — never an arg-less wall clock — both because expiry only needs
    elapsed-time and because a monotonic source is immune to wall-clock jumps.
    """
    key = (provider, folder_id)
    if ttl > 0:
        hit = _LISTING_CACHE.get(key)
        if hit is not None and (now() - hit[0]) < ttl:
            return hit[1]
    refs = lister()
    if ttl > 0:
        _LISTING_CACHE[key] = (now(), refs)
    return refs


def _clear_listing_cache() -> None:
    """Reset the in-process listing cache.

    Called ONCE per CLI invocation — `fan_out_ingest` clears before its
    per-project loop (so the cache is shared across the projects of an `--all`
    sweep) and the single-project CLI branch clears before its one run. Also
    called by tests: the module-level dict survives the whole pytest session, so
    tests must clear it to avoid leaking cached listings into one another."""
    _LISTING_CACHE.clear()


def _drive_file_ref(item: dict, folder_path: tuple[str, ...] = ()) -> FileRef:
    """Map one non-folder Drive child dict to a FileRef (download is by id).

    `folder_path` is the breadcrumb of folder names from the project root down to
    this file's parent (see `_list_drive._walk`); the FileRef-mapping for Drive
    lives here in one place.
    """
    return FileRef(
        source="drive",
        id=item.get("id"),
        name=item.get("name"),
        mime_type=item.get("mimeType"),
        size=_coerce_size(item.get("size")),
        modified=item.get("modifiedTime"),
        path=None,
        folder_path=folder_path,
        # None for Google-native Docs/Sheets/Slides (Drive omits md5Checksum for
        # non-binary files) → those have no change token and always re-ingest.
        change_token=item.get("md5Checksum"),
    )


def _list_drive(connector, folder_id: str) -> list[FileRef]:
    """Recursively list a Drive folder tree via the pagination primitive.

    Real client folders nest their files in subfolders ("Define and Approach" /
    "Management" / …) with ZERO files at the top level, so a single-level listing
    finds nothing. We tree-walk: list each folder's direct children, emit a
    FileRef for every non-folder child, and descend into every folder child.

    `_list_files_with_pagination` is the listing primitive on GoogleDriveConnector
    (it has no simple public `list(folder_id)`); it returns dicts with the fields
    we request, already drained across Drive API pages. We request `mimeType` on
    every child so we can tell folders from leaf files.

    Termination is bounded two ways: a visited-set of folder ids (Drive shortcuts
    can create cycles) AND a depth cap (`_DRIVE_MAX_DEPTH`). Hitting the cap emits
    a stderr note rather than silently dropping the deeper subtree.
    """
    refs: list[FileRef] = []
    visited: set[str] = set()

    def _walk(fid: str, depth: int, path_segments: tuple[str, ...]) -> None:
        # `path_segments` is the breadcrumb of folder NAMES accumulated so far,
        # from the project root down to `fid` (exclusive of `fid`'s own name
        # until we descend INTO a folder child). The top-level call starts with
        # `()` — the project ROOT folder's own name is deliberately NOT a
        # segment.
        #
        # WHY start empty: this keeps Drive CONSISTENT with how Dropbox presents
        # paths so the SAME allowlist (a later task) matches both sources. For
        # Dropbox, `mc_dropbox_folder_id` is the project root and `path_display`
        # includes that whole prefix — but the filter tests segment-CONTAINS
        # against allowed names like "Client Assets"; the root-prefix segments
        # ("1P Active Projects", "SAP 5174 …") simply won't contain that name,
        # so they're harmless noise. For Drive, starting empty at the root means
        # a file at root/"01 Client Assets"/brief.pdf gets
        # folder_path=("01 Client Assets",) — the SUBFOLDER name, which is what
        # the allowlist matches. So Drive records folders BELOW the root;
        # Dropbox happens to include the root prefix too but it doesn't matter
        # for matching.
        if fid in visited:
            return  # cycle guard — Drive shortcuts can point back up the tree
        visited.add(fid)
        if depth > _DRIVE_MAX_DEPTH:
            print(
                f"[asset-ingest] Drive recursion hit depth cap "
                f"({_DRIVE_MAX_DEPTH}) at folder '{fid}' — not descending "
                "further (subtree below this point NOT listed)",
                file=sys.stderr,
            )
            return
        items = connector._list_files_with_pagination(
            query=f"'{fid}' in parents and trashed=false",
            fields="files(id,name,mimeType,size,modifiedTime,md5Checksum)",
            page_size=100,
        )
        for item in items:
            if item.get("mimeType") == _DRIVE_FOLDER_MIME:
                child_id = item.get("id")
                if child_id:
                    # A Drive folder item missing `name` would thread `None` into
                    # the breadcrumb → a later `None.lower()` AttributeError in
                    # `_matches_allowlist`. Coerce to "" so the segment is just
                    # harmless empty noise.
                    name = item.get("name") or ""
                    _walk(
                        child_id,
                        depth + 1,
                        path_segments + (name,),
                    )
            else:
                refs.append(_drive_file_ref(item, path_segments))

    _walk(folder_id, 0, ())
    return refs


def _list_dropbox(connector, folder: str) -> list[FileRef]:
    """Recursively list a Dropbox folder via `dbx.files_list_folder(recursive=True)`.

    ASSUMPTION (documented for the next task): MC-2's `mc_dropbox_folder_id` is
    treated as a value `files_list_folder` accepts directly — i.e. a folder path
    (`/Clients/...`) or a Dropbox folder id (`id:...`), both of which that API
    handles. The connector's public `list_project_files` is path-derived from
    project_code/name and does NOT take the stored folder id, so we use the
    underlying `dbx` client and normalize the FileMetadata entries ourselves.

    Real client folders nest files in subfolders ("01 Project Notes" /
    "02 Working Files" / …) with nothing at the top, so we pass `recursive=True`
    — the SDK returns ALL descendants (files + folders) at every depth in a single
    logical listing, drained across pages via `has_more` + `files_list_folder_continue`.
    No manual tree-walk is needed: recursive=True already flattens the whole tree.

    We keep only FileMetadata entries (FolderMetadata has no `.size`) and use each
    entry's `path_display` (the full nested path) as the FileRef path, which is
    what the download step fetches by.
    """
    result = connector.dbx.files_list_folder(folder, recursive=True)
    refs: list[FileRef] = []
    while True:
        for entry in getattr(result, "entries", []) or []:
            # FileMetadata has size + client_modified; FolderMetadata does not —
            # skip folders, keep files (at any depth, since recursive=True).
            if not hasattr(entry, "size"):
                continue
            refs.append(
                FileRef(
                    source="dropbox",
                    id=getattr(entry, "id", None),
                    name=getattr(entry, "name", None),
                    mime_type=None,  # Dropbox doesn't expose a mime type on list
                    size=_coerce_size(getattr(entry, "size", None)),
                    modified=str(getattr(entry, "client_modified", None) or "")
                    or None,
                    path=getattr(entry, "path_display", None),
                    change_token=getattr(entry, "content_hash", None),
                )
            )
        # Drain the cursor: recursive results page just like a flat listing.
        if not getattr(result, "has_more", False):
            break
        result = connector.dbx.files_list_folder_continue(result.cursor)
    return refs


# ──────────────────────────────────────────────────────────────────────
#  Folder allowlist (per-project filter)
# ──────────────────────────────────────────────────────────────────────


def _folder_segments(ref: FileRef) -> list[str]:
    """The FOLDER name segments of a ref — its ancestry, NOT the filename.

    Drive: the recorded breadcrumb (`folder_path`) already excludes the file.
    Dropbox: split `path_display` on `/`, drop empties, and DROP THE LAST
    element (which is the filename) so only folder names remain. A None/empty
    Dropbox path yields `[]`.
    """
    if ref.source == "drive":
        return list(ref.folder_path)
    if not ref.path:
        return []
    parts = [seg for seg in ref.path.split("/") if seg]
    return parts[:-1]  # drop the filename


def _matches_allowlist(ref: FileRef, allowlist: tuple[str, ...]) -> bool:
    """True if ANY folder segment CONTAINS any allowed name, case-insensitive.

    Segment-CONTAINS (not equality) so the common agency convention of a numbered
    prefix — "01 Client Assets" — still matches allowed "Client Assets", as does a
    suffix ("Client Assets v2"). Tested against folder segments ONLY (never the
    filename, which `_folder_segments` drops).
    """
    # `if allowed and allowed.strip()` is belt-and-suspenders: `_row_to_folders`
    # already strips + drops empty allowed names at the service boundary, but
    # this is a pure, public-ish function that could be called with unsanitized
    # input directly. An empty/whitespace allowed name must NOT match every
    # segment (`"" in seg` is always True) — skip it.
    return any(
        allowed.lower() in seg.lower()
        for seg in _folder_segments(ref)
        for allowed in allowlist
        if allowed and allowed.strip()
    )


# A match-nothing sentinel for `_effective_allowlist`. Distinct from `()` — which
# `list_files` reads as "match ALL" (no filter). When `only_folder` is NOT
# permitted by the configured allowlist we must scan NOTHING, so we return THIS
# instead of `()`: it's a string no real folder segment can contain (a NUL byte
# is never present in a Drive/Dropbox folder name), so `_matches_allowlist`
# contains-checks it against every segment and always returns False.
_MATCH_NOTHING: tuple[str, ...] = ("\0__no_folder__",)


def _effective_allowlist(
    only_folder: str | None, configured: tuple[str, ...]
) -> tuple[str, ...]:
    """The allowlist to actually scan, NARROWING `configured` by `only_folder`.

    This can RESTRICT the configured per-project allowlist but NEVER widen it: a
    targeted scan can only reach a folder the project is already configured to
    ingest. Rules:

    - `only_folder is None` → no narrowing requested → return `configured`
      unchanged (today's behavior — identical to no `only_folder` at all).
    - `configured` is empty `()` → "all allowed", so `only_folder` is within
      scope by definition → narrow to `(only_folder,)`.
    - otherwise `only_folder` must be PERMITTED by `configured`. only_folder is
      permitted IFF it is itself matched by the configured allowlist: it may
      EQUAL an allowed name ("Carol Decks") or be a SUPER-name that CONTAINS one
      ("Carol Decks Archive" contains allowed "Carol Decks"). It must NOT be a
      mere FRAGMENT of an allowed name ("Carol" is only a fragment of "Carol
      Decks" → NOT permitted). The permit direction is ONLY `allowed in
      only_folder`, which makes the result provably SUBSET-SAFE (never widens):
        if a real folder segment contains only_folder, AND only_folder contains
        a configured `allowed`, THEN the segment contains `allowed` → the
        configured allowlist already matched that segment → effective ⊆ original
        always holds.
      The reverse direction (`only_folder in allowed`, i.e. only_folder a mere
      fragment) is DELIBERATELY EXCLUDED: `("Carol",)` would match "Carol
      Photos", "Carolina HR", etc. — folders configured `("Carol Decks",)` would
      never ingest. That would WIDEN the allowlist, defeating the feature.
        * permitted → narrow to `(only_folder,)`, which `_matches_allowlist`
          will then contains-match against real folder segments.
        * NOT permitted → return the match-nothing sentinel `_MATCH_NOTHING`
          (NOT `()`, which `list_files` reads as "match ALL"; "none" needs a
          dedicated sentinel that no real folder segment contains).
    """
    if only_folder is None:
        return configured
    if not configured:
        return (only_folder,)
    needle = only_folder.lower()
    permitted = any(
        allowed.lower() in needle  # configured entry contained in only_folder
        for allowed in configured
        if allowed and allowed.strip()
    )
    return (only_folder,) if permitted else _MATCH_NOTHING


def _dropbox_connector():
    """Construct a `DropboxConnector` with DROPBOX_* creds ensured first (#154).

    The connector self-configures from `os.getenv`, which works on Railway/cron
    (env preset) and under `cp mcp` (#111 loads creds per-verb) but NOT from a
    bare-terminal CLI run — the .env auto-load exports only SUPABASE_*, so the
    same ingest that succeeds everywhere else fails locally with "No Dropbox
    credentials found". Best-effort fill DROPBOX_* from the mc-2 clone's .env
    before constructing (already-set env vars WIN, matching
    `mc2_db.load_dropbox_creds`). Loader errors are swallowed: outside a tenant
    repo there is nothing to load, and in that case the connector's own
    "No Dropbox credentials found" is the actionable message.
    """
    from cloud_storage.dropbox_connector import DropboxConnector

    try:
        from cp_engine import config as cp_config
        from cp_engine import mc2_db
        from cp_engine.capture_session import find_tenant_root

        root = find_tenant_root(Path.cwd()) or Path.cwd()
        mc2_db.load_dropbox_creds(cp_config.load(root))
    except Exception:  # noqa: BLE001 — creds loading is optional enrichment
        pass
    return DropboxConnector()


def list_files(
    folders: ProjectFolders,
    drive_connector=None,
    dropbox_connector=None,
    allowlist: tuple[str, ...] = (),
    *,
    use_cache: bool = True,
) -> tuple[list[FileRef], list[dict[str, str]]]:
    """Enumerate the files in a project's enabled Drive + Dropbox folders.

    Returns `(refs, source_notes)`: the discovered `FileRef`s, and a list of
    `{"source": "drive"|"dropbox", "note": "<reason>"}` notes describing anything
    that went wrong or was skipped for a source. The notes let a UI surface *why*
    a source produced nothing without aborting the run.

    Connectors are injectable for testability; when omitted they are constructed
    from the environment (service-account file / Dropbox refresh-token creds).

    PER-SOURCE RESILIENCE (the point of this function's shape): the two sources
    are INDEPENDENT. A failure walking Drive (auth failure, bad folder id, rate
    limit) MUST NOT abort the Dropbox walk — the docs the user wants may live in
    the other source, and this is driven from a button where the user can't fix
    creds. So each source block is wrapped in its own `try/except`: on a raise we
    record a note, log to stderr, and CONTINUE to the next source rather than
    propagate. Config gaps (non-client company, disabled source, missing folder
    id) are likewise recorded as notes, not errors.

    `use_cache` (default True) gates the in-process listing cache: when True the
    recursive Drive/Dropbox tree-walks route through `_cached_listing` keyed by
    `(provider, folder_id)`, so a second scan of the same folder in one process is
    a cache hit. When False the walk runs directly (ttl=0) — identical behavior to
    before this cache existed. Same flag that gates the per-file ingest cache in
    `ingest_project_assets`, so `--no-cache` disables both layers together.
    """
    source_notes: list[dict[str, str]] = []
    # ttl=0 makes `_cached_listing` a pure pass-through (always recompute, never
    # store) so `use_cache=False` reproduces the pre-cache behavior exactly.
    _listing_ttl = _LISTING_TTL_SECONDS if use_cache else 0.0

    # Initiatives are ingestable despite their self-* company kind (mc-2 #192);
    # the kind guard only screens out non-client ENGAGEMENT owners.
    if folders.company_kind != "client" and not folders.is_initiative:
        print(
            f"[asset-ingest] skipping non-client company kind="
            f"'{folders.company_kind}' (asset ingest targets clients "
            "and initiatives only)",
            file=sys.stderr,
        )
        return [], []

    results: list[FileRef] = []

    # ── Drive ──
    if folders.enable_google_drive:
        if folders.google_drive_folder_id:
            try:
                if drive_connector is None:
                    from cloud_storage.google_drive_connector import (
                        GoogleDriveConnector,
                    )

                    drive_connector = GoogleDriveConnector(service_account_file=None)
                _conn = drive_connector
                _fid = folders.google_drive_folder_id
                results.extend(
                    _cached_listing(
                        "drive",
                        _fid,
                        lambda: _list_drive(_conn, _fid),
                        ttl=_listing_ttl,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a dead Drive source must not kill Dropbox; record + continue
                source_notes.append(
                    {"source": "drive", "note": f"{type(exc).__name__}: {exc}"}
                )
                print(
                    f"[asset-ingest] drive source failed, continuing: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc()
        else:
            note = "enable_google_drive set but no google_drive_folder_id"
            source_notes.append({"source": "drive", "note": note})
            print(f"[asset-ingest] {note} — skipping Drive", file=sys.stderr)

    # ── Dropbox ──
    if folders.enable_dropbox:
        if folders.mc_dropbox_folder_id:
            try:
                if dropbox_connector is None:
                    dropbox_connector = _dropbox_connector()
                _conn = dropbox_connector
                _fid = folders.mc_dropbox_folder_id
                results.extend(
                    _cached_listing(
                        "dropbox",
                        _fid,
                        lambda: _list_dropbox(_conn, _fid),
                        ttl=_listing_ttl,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a dead Dropbox source must not kill Drive; record + continue
                source_notes.append(
                    {"source": "dropbox", "note": f"{type(exc).__name__}: {exc}"}
                )
                print(
                    f"[asset-ingest] dropbox source failed, continuing: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc()
        else:
            note = "enable_dropbox set but no mc_dropbox_folder_id"
            source_notes.append({"source": "dropbox", "note": note})
            print(f"[asset-ingest] {note} — skipping Dropbox", file=sys.stderr)

    # ── Folder allowlist ──
    # Applied AFTER both sources have built `results`, so a single segment-CONTAINS
    # rule covers Drive + Dropbox uniformly. An EMPTY allowlist is a true no-op —
    # no filtering, no filter note — so existing callers (which pass nothing) are
    # unaffected and a project with no configured folders ingests its whole tree.
    if allowlist:
        total = len(results)
        kept = [r for r in results if _matches_allowlist(r, allowlist)]
        excluded = total - len(kept)
        names = ", ".join(allowlist)
        if kept:
            note = (
                f"allowlist [{names}] matched {len(kept)} of {total} files "
                f"({excluded} excluded by folder filter)"
            )
        else:
            note = (
                f"allowlist [{names}] matched 0 of {total} files "
                "(check folder names)"
            )
        source_notes.append({"source": "filter", "note": note})
        results = kept

    return results, source_notes


# ──────────────────────────────────────────────────────────────────────
#  Download
# ──────────────────────────────────────────────────────────────────────


def download_file(
    file_ref: FileRef,
    tmp_dir: Path,
    drive_connector=None,
    dropbox_connector=None,
) -> Path:
    """Download one `FileRef` into `tmp_dir`, returning the written path.

    Connectors are injectable for testability; when omitted they are constructed
    from the environment (same lazy pattern as `list_files`). `tmp_dir` is owned
    by the caller (the next task creates and cleans it); this function only
    writes into it.

    The returned path may differ from `tmp_dir / file_ref.name`: for Drive,
    Google-native files (Docs/Sheets/Slides) are EXPORTED with an office/PDF
    suffix the connector appends, and we MUST return the connector's actual path
    because the downstream parser dispatches on file extension.
    """
    if file_ref.source == "drive":
        if drive_connector is None:
            from cloud_storage.google_drive_connector import GoogleDriveConnector

            drive_connector = GoogleDriveConnector(service_account_file=None)
        # download_file auto-exports Google-native files and RETURNS the actual
        # written path (it may add `.docx`/`.pdf`/etc. when exporting). Trust the
        # returned path — do NOT assume it equals the destination we passed in.
        return drive_connector.download_file(file_ref.id, tmp_dir / file_ref.name)

    if file_ref.source == "dropbox":
        if dropbox_connector is None:
            dropbox_connector = _dropbox_connector()
        # WORKAROUND: the Dropbox connector has no download_file; using the SDK
        # client's files_download directly. Retire when the component adds
        # download_file. Fetch by path_display (the value `list_files` stored on
        # the FileRef); Dropbox returns (metadata, response) and the bytes live
        # on response.content.
        # Dropbox's `path` arg accepts a literal `/path`, an `id:<id>`, or a
        # `rev:<rev>`. Normal ingest fetches by path_display. Backfilled historic
        # rows recover source_file_id (already the `id:<body>` form) but never the
        # dead temp path_display, so their FileRef has path=None — fall back to
        # fetching by the id-form path, which Dropbox accepts as-is.
        if file_ref.path:
            download_arg = file_ref.path
        elif file_ref.id:
            download_arg = file_ref.id
        else:
            raise ValueError(
                "[asset-ingest] cannot download Dropbox FileRef with neither "
                f"path nor id (name={file_ref.name!r})"
            )
        local_path = tmp_dir / file_ref.name
        _metadata, response = dropbox_connector.dbx.files_download(path=download_arg)
        local_path.write_bytes(response.content)
        return local_path

    raise ValueError(
        f"[asset-ingest] cannot download FileRef with unknown source "
        f"'{file_ref.source}' (expected 'drive' or 'dropbox')"
    )


# ──────────────────────────────────────────────────────────────────────
#  Ingest run (Task C5)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class IngestRunResult:
    """Summary of one `ingest_project_assets` run.

    Counts are by IngestResult.action. `failures` carries one (file_name, error)
    tuple per file that failed (a 'failed' action OR a raising download) so the
    caller can surface every failure — nothing is silently swallowed.
    `project_found` is False only for the "no matching MC-2 project" case.
    """

    created: int = 0
    versioned: int = 0
    skipped: int = 0
    failed: int = 0
    # Cross-path content duplicates skipped by the (project_id, file_hash)
    # pre-check: the same bytes already live in this project under a DIFFERENT
    # file_path (same file in Drive AND Dropbox, or a copy within one source).
    # The pipeline's own dedup keys on file_path and can't see these, so we catch
    # them before parse/embed/insert. Distinct from `skipped` (pipeline's
    # same-path unchanged-file skip) and `skipped_shortcuts` (pointer files).
    deduped: int = 0
    # Non-ingestable shortcut/pointer files (.url/.lnk/.webloc) skipped BEFORE
    # download. Kept distinct from `skipped` (dedup) and `failed` (real failure)
    # so the counts stay semantically clean; surfaced via a source_note.
    skipped_shortcuts: int = 0
    # Files skipped by the ingest cache: an active rag_asset already carries a
    # meta.change_token equal to the freshly-listed provider token → unchanged →
    # no download/hash/embed needed (see _unchanged_since_last_ingest). Distinct
    # from `skipped` (pipeline same-path), `deduped` (cross-path content dup), and
    # `skipped_shortcuts` (pointer files). Zero when use_cache=False or no hits.
    skipped_unchanged: int = 0
    # Prior active assets superseded by a same-title re-ingest (#57): the doc's
    # CONTENT changed since its last ingest, so the pipeline created a brand-new
    # row (hash dedup can't catch it, and the path-keyed dedup only versions
    # same-path re-arrivals). The post-create supersede chains the new row via
    # `prev_asset_id` and retires the old copies so retrieval only ever serves
    # the newest. Counts OLD assets retired, not new files ingested.
    superseded: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    project_found: bool = True
    # Per-source listing notes from list_files: a dead/skipped source records a
    # {"source", "note"} entry here rather than aborting the whole run, so the
    # caller (and the button UI) can show why a source produced nothing.
    source_notes: list[dict[str, str]] = field(default_factory=list)
    # The confirm-gate verdict (#59): non-None when the project resolved fine
    # but NO ENABLED source has a folder id configured — the run would silently
    # ingest nothing, so `ingest_project_assets` short-circuits and records WHY
    # here instead of returning a normal-looking empty run. Callers decide the
    # UX: the single-project CLI refuses (unless --allow-empty), the --all
    # fan-out skips with a visible note, and the webhook records a structured
    # refusal on the run row. None on every configured (or not-found) run.
    unconfigured_reason: str | None = None


# configure_ingest() wires document-ingest's module-level singletons (settings +
# OpenAI client factory). It is idempotent at the engine's level, but we also
# guard here so repeated calls in the same process are a no-op.
_pipeline_configured = False


def _configure_pipeline_once() -> None:
    """Wire document-ingest's settings + OpenAI client factory exactly once.

    The pipeline needs an OpenAI client even when embeddings come from Voyage:
    `IngestPipeline.__init__` eagerly constructs the audio/image/video parsers,
    which read OPENAI_API_KEY. cp's env has it, so that's fine — we just hand the
    engine a factory that builds a plain `OpenAI(api_key=OPENAI_API_KEY)`.
    """
    global _pipeline_configured
    if _pipeline_configured:
        return

    from ingest.config import configure_ingest

    from cp_engine.asset_ingest_settings import AssetIngestSettings

    def _openai_client_factory():
        from openai import OpenAI

        return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    configure_ingest(
        settings=AssetIngestSettings(),
        openai_client_factory=_openai_client_factory,
    )
    _pipeline_configured = True


def _build_pipeline(project_id: str, supabase_url: str, supabase_key: str):
    """Construct an IngestPipeline wired to the Voyage embedder.

    Kept tiny + separate so `ingest_project_assets` can inject a pipeline (or a
    pipeline_factory) in tests and never touch real Supabase/OpenAI/Voyage.
    """
    from ingest.embedding_service import IngestEmbeddingService
    from ingest.pipeline import IngestPipeline

    return IngestPipeline(
        project_id=project_id,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        embed_model="voyage-3-large",
        chunking_strategy="text",
        embedder=IngestEmbeddingService(model="voyage-3-large"),
    )


def _adapt_pipeline_for_initiative(pipeline, initiative_id: str) -> None:
    """Rebind a real IngestPipeline's owner column to `initiative_id` (#192).

    The document-ingest package is engagement-shaped: `ingest_file` calls
    `deduplication.check_asset(project_id, …)` (a rag_assets lookup filtered on
    the `project_id` column) and `storage.create_asset(project_id=…)` (an
    INSERT carrying `project_id`). For an initiative, both are wrong — the
    lookup would never match (owner lives in `initiative_id`) and the INSERT
    would FK-crash (`rag_assets.project_id` references `projects`; migration
    081's CHECK wants exactly one owner). Since the pipeline has no owner-column
    seam of its own, we rebind the two instance methods here:

      - `deduplication.check_asset` → the same path+hash decision tree, but
        filtered on `initiative_id` (explicit columns, never `*`);
      - `storage.create_asset` → the same INSERT shape with `initiative_id`
        as the owner column (and the same prev-version supersede behavior).

    Chunk/embedding writes key on `asset_id` and are untouched. No-ops when
    the pipeline lacks the `storage`/`deduplication` seams (an injected test
    fake) — such fakes never hit the real table, so there is nothing to rebind.
    """
    storage = getattr(pipeline, "storage", None)
    dedup = getattr(pipeline, "deduplication", None)
    if storage is None or dedup is None:
        return

    def _check_asset(project_id, file_path, file_hash):
        # Mirrors DeduplicationService.check_asset, owner-filtered on
        # initiative_id. `project_id` (the pipeline passes its own configured
        # id positionally) is deliberately ignored in favor of the closure.
        from ingest.deduplication_api import DedupeDecision

        result = (
            dedup.client.table(Tables.RAG_ASSETS)
            .select("id, file_hash, status")
            .eq("initiative_id", initiative_id)
            .eq("file_path", file_path)
            .in_("status", ["active", "archived"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if not rows:
            return DedupeDecision(
                action="new", reason="File path not found in database"
            )
        row = rows[0]
        # Archived-guard parity with the lib's project path (#126): a
        # curator's archive survives re-ingest; changed content is new.
        if row.get("status") == "archived":
            if row["file_hash"] == file_hash:
                return DedupeDecision(
                    action="skip",
                    existing_asset_id=row["id"],
                    reason="Asset was archived by a curator (unchanged content)",
                )
            return DedupeDecision(
                action="new",
                reason="Prior copy archived; file content has since changed",
            )
        if row["file_hash"] == file_hash:
            return DedupeDecision(
                action="skip",
                existing_asset_id=row["id"],
                reason="File content unchanged (hash match)",
            )
        return DedupeDecision(
            action="new_version",
            existing_asset_id=row["id"],
            reason="File content changed (hash mismatch)",
        )

    def _create_asset(
        project_id,
        source_type,
        title,
        url,
        file_path,
        file_hash,
        metadata,
        prev_asset_id=None,
    ):
        # Mirrors StorageService.create_asset with the initiative owner column;
        # `project_id` is ignored (see _check_asset). project_id stays absent
        # from the INSERT so migration 081's exactly-one-owner CHECK holds.
        # Same-title disambiguation as the project path (lib helper, rebound
        # onto the initiative owner column) — recurring recordings share a
        # title and title-pulls must not merge distinct documents.
        try:
            title = storage._disambiguate_title(
                initiative_id, title, prev_asset_id,
                owner_column="initiative_id",
            )
        except AttributeError:
            pass  # older lib pin without the helper — keep the raw title
        result = (
            storage.client.table(Tables.RAG_ASSETS)
            .insert(
                {
                    "initiative_id": initiative_id,
                    "source_type": source_type,
                    "title": title,
                    "url": url,
                    "file_path": file_path,
                    "file_hash": file_hash,
                    "meta": metadata,
                    "prev_asset_id": prev_asset_id,
                }
            )
            .execute()
        )
        asset_id = result.data[0]["id"]
        if prev_asset_id:
            storage.client.table(Tables.RAG_ASSETS).update(
                {"status": "superseded"}
            ).eq("id", prev_asset_id).execute()
        return asset_id

    dedup.check_asset = _check_asset
    storage.create_asset = _create_asset


def _stable_dir_for(tmp_root: Path, file_ref: FileRef) -> Path:
    """A per-source, deterministic temp subdir for one file.

    WHY deterministic and not a random `tempfile.mkdtemp()`: the pipeline's
    `ingest_file(file_path, ...)` uses `file_path` for THREE things at once —
    it (a) opens & parses it, (b) keys deduplication on it, and (c) stores it as
    `rag_assets.file_path`. There is no separate "dedup key" arg. So the path we
    download to IS the dedup key. If it were a random tmp name, every re-run
    would produce a brand-new file_path → dedup never matches → re-ingest always
    re-creates instead of skipping (spec checklist item 4 demands re-run = all
    skipped). Deriving the directory from a STABLE source identifier
    (`<source>-<id>`) makes the download land at the same path on every run, so
    the file_hash check in DeduplicationService.check_asset short-circuits to
    'skip'. The Drive-export suffix the connector may append is itself
    deterministic, so the returned path is stable too.
    """
    # Sanitize the source id into a filesystem-safe directory name.
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", f"{file_ref.source}-{file_ref.id}")
    return tmp_root / safe_id


def _source_url(file_ref: FileRef, drive_connector=None, dropbox_connector=None) -> str | None:
    """Return a durable, human-clickable web link for a source file, or None.

    - Drive: a `/file/d/<id>/view` link built from the file id — no API call.
    - Dropbox: a TEAM-ONLY shared link via the connector. This is CLIENT source
      material (decks, emails, briefs), so the link is requested team-only
      (members of the Dropbox team), never "anyone with the link" public. We FAIL
      CLOSED: the connector must prove it can honor team-only (its
      `get_shareable_link` signature must include a `team_only` parameter) before
      we call it; a connector that can't enforce team-only yields None rather than
      risk a public link.
    - Any other source, missing connector, or any exception → None. A link is a
      nice-to-have; it must never block or fail ingest.

    The pipeline stores this verbatim as `rag_assets.url` and builds citation
    deep-links from it, so a missing url is strictly safer than a wrong one.
    """
    try:
        if file_ref.source == "drive":
            return f"https://drive.google.com/file/d/{file_ref.id}/view"
        if file_ref.source == "dropbox":
            # Need both a connector and the dropbox path_display to resolve.
            if dropbox_connector is None or not file_ref.path:
                return None
            # FAIL CLOSED on the team-only guarantee. This is CLIENT material;
            # a public "anyone with the link" Dropbox URL would leak it. The
            # team-only enforcement lives in a SEPARATELY-deployed connector
            # (social-builder-app). We cannot tell a team-only link from a public
            # one by inspecting the URL string (both are dropbox.com/s|scl/...).
            # So instead of trusting the URL, we require the connector to PROVE it
            # can honor team-only: its get_shareable_link must accept a `team_only`
            # parameter. If an old/intermediate connector is deployed whose
            # signature LACKS that param, we must NOT call it / must NOT trust any
            # link it returns — we return None. Only when the param exists do we
            # call with team_only=True and trust the connector's own internal
            # verification (Task 2: it raises rather than return a public link).
            try:
                sig = inspect.signature(dropbox_connector.get_shareable_link)
            except (TypeError, ValueError):
                # Can't introspect the connector → can't prove team-only → bail.
                return None
            if "team_only" not in sig.parameters:
                return None
            return dropbox_connector.get_shareable_link(file_ref.path, team_only=True)
        return None
    except Exception:
        # Never let a link lookup abort ingest of the file.
        return None


def ingest_project_assets(
    project_code: str,
    *,
    mc_project_id: str | None = None,
    client=None,
    drive_connector=None,
    dropbox_connector=None,
    tmp_root: Path | None = None,
    pipeline=None,
    pipeline_factory=None,
    supabase_url: str | None = None,
    supabase_key: str | None = None,
    use_cache: bool = True,
    only_folder: str | None = None,
    only_file_ids: set[str] | None = None,
) -> IngestRunResult:
    """Resolve, list, download, ingest, and scope-stamp a project's cloud assets.

    Flow: resolve the project's Drive/Dropbox folders → list their files →
    for each file download it to a STABLE temp path → run it through the
    document-ingest pipeline (Voyage embedder) → stamp the freshly-written
    `rag_assets` row with `scope='project'` + `company_id`.

    Injectable seams (all for testability; real deps are built lazily otherwise):
      - `client`: the MC-2 Supabase client. Built from creds if omitted.
      - `pipeline`: a ready IngestPipeline. Overrides `pipeline_factory`.
      - `pipeline_factory`: `(project_id, url, key) -> pipeline`. Defaults to
        `_build_pipeline`.
      - `supabase_url`/`supabase_key`: the Supabase coordinates the pipeline
        writes to. Resolved from cp config if omitted.

    Failure policy: a 'failed' IngestResult OR a raising download is recorded in
    `failures` and the loop continues — one bad file never aborts the run, and no
    failure is silently swallowed. Downloaded bytes are always cleaned up.

    INGEST CACHE (per-file skip): each listed file carries a provider content
    token (`FileRef.change_token` — Drive `md5Checksum`, Dropbox `content_hash`).
    Before downloading, `_unchanged_since_last_ingest` looks up the active
    `rag_assets` row for this (owner, provider, file id) and, if its stored
    `meta.change_token` equals the freshly-listed token, the file is skipped
    entirely — no download, hash, or embed — and counted in
    `result.skipped_unchanged` (surfaced in the run summary). The matching token
    is written by `_stamp_scope` into `meta.change_token` after each successful
    create/version, so run 1 stamps and run 2+ skips; editing the file changes its
    provider token → mismatch → re-ingest → the stamp updates the token again.
    Caveats: Google-native files (Docs/Sheets/Slides) carry no md5 → no token →
    always re-ingested; the FIRST run after deploying this feature re-ingests once
    (no token stamped yet) and only then begins skipping. The skip-check is
    fail-open: a missing row, missing/mismatched/None token, or any error → not
    skipped. `use_cache=False` (CLI `--no-cache`) bypasses BOTH this per-file skip
    AND the per-folder listing cache (ttl=0) for a guaranteed full re-scan.

    LISTING CACHE: this function deliberately does NOT clear the in-process
    listing cache. The clear lives ONE level up, at the CLI entry points
    (`fan_out_ingest` for `--all`, and the single-project `ingest-assets <code>`
    branch) — so a single CLI invocation clears ONCE and the cache then persists
    ACROSS the per-project runs of an `--all` sweep. That is the whole point of
    the cache: two projects sharing a parent (provider, folder_id) walk it once
    total. Clearing here (per project) would wipe the cache between every project
    and make it useless. A new non-CLI caller that needs a guaranteed-fresh cache
    must call `_clear_listing_cache()` itself before invoking.

    TARGETED SCAN: `only_folder` (default None = today's behavior) NARROWS the
    listing to a single configured folder via `_effective_allowlist`. It can only
    RESTRICT the per-project `asset_ingest_folders` allowlist, never widen it — an
    `only_folder` the allowlist doesn't permit scans NOTHING (scope guard).

    FILE-SCOPED SCAN: `only_file_ids` (default None = no file filter) narrows the
    listed files to an explicit set of selection keys — the picker's "ingest
    exactly these" path. A key is `file_selection_key(ref)`: the Drive file id,
    or (Dropbox) the path_display, or the id. This is a pure NARROWING filter
    applied AFTER `list_files`, so it can only ever be a subset of what a full
    scan would see — no new download path, and the per-file content-hash cache
    still applies. An empty set scans nothing; None disables the filter. Combines
    with `only_folder` (both narrow; the intersection is scanned).
    """
    # Resolve creds lazily, and ONLY the pieces actually missing — so the unit-
    # test path (injected client + pipeline) never touches cp config / Supabase.
    # Build the MC-2 client up front (needed for resolve + the scope stamp).
    if client is None:
        from cp_engine import mc2_db

        if supabase_url is not None and supabase_key is not None:
            client = mc2_db.get_client(url=supabase_url, key=supabase_key)
        else:
            from cp_engine import config as cp_config

            client = mc2_db.get_client(cp_config.load(Path.cwd()))

    # Prefer the authoritative by-id resolution when the caller supplies the
    # MC-2 row id (the mc-2 button does). Fall back to the by-code (by-number)
    # path for callers that only have a code — e.g. the CLI `cp ingest-assets
    # <code>`, where bare-numeric codes still resolve by number.
    folders = (
        resolve_project_folders_by_id(client, mc_project_id)
        if mc_project_id
        else resolve_project_folders(client, project_code)
    )
    if folders is None:
        # No matching MC-2 project — asset ingest doesn't apply. The resolver
        # already printed the reason to stderr.
        return IngestRunResult(project_found=False)

    # Confirm gate (#59): a client project whose ENABLED sources have no folder
    # id would silently list nothing — short-circuit BEFORE building connectors
    # or touching a provider, and record the reason so every entry point (CLI,
    # --all fan-out, webhook button) can surface it instead of a
    # normal-looking empty run. Non-client kinds pass through to list_files'
    # existing skip (the reason there is the company kind, not folders).
    unconfigured = folders_unconfigured_reason(folders)
    if unconfigured is not None:
        print(
            f"[asset-ingest] {project_code}: {unconfigured} — nothing to ingest",
            file=sys.stderr,
        )
        return IngestRunResult(
            unconfigured_reason=unconfigured,
            source_notes=[{"source": "config", "note": unconfigured}],
        )

    # Build the connectors ONCE up front (when their source is enabled) so the
    # same instance is reused for listing, downloading, AND link generation.
    # list_files/download_file otherwise construct their own *local* connectors,
    # which never propagate back here — so without this, _source_url below would
    # always see dropbox_connector=None and never emit a Dropbox link. A
    # construction failure is swallowed: list_files constructs its OWN connector
    # (it does not reuse this instance) and records its own per-source note, and
    # here a missing connector just yields url=None for link generation.
    if drive_connector is None and getattr(folders, "enable_google_drive", False):
        try:
            from cloud_storage.google_drive_connector import GoogleDriveConnector

            drive_connector = GoogleDriveConnector(service_account_file=None)
        except Exception:  # noqa: BLE001 — list_files handles + notes the failure
            drive_connector = None
    if dropbox_connector is None and getattr(folders, "enable_dropbox", False):
        try:
            dropbox_connector = _dropbox_connector()
        except Exception:  # noqa: BLE001 — list_files handles + notes the failure
            dropbox_connector = None

    files, source_notes = list_files(
        folders,
        drive_connector,
        dropbox_connector,
        allowlist=_effective_allowlist(only_folder, folders.asset_ingest_folders),
        use_cache=use_cache,
    )
    # File-scoped narrowing (picker's "ingest exactly these"): keep only the
    # listed files whose selection key was chosen. Pure subset of the scan
    # above — never widens. `None` disables; an empty set legitimately scans
    # nothing (and falls through to the `if not files` short-circuit below).
    if only_file_ids is not None:
        files = [f for f in files if file_selection_key(f) in only_file_ids]
    if not files:
        # Nothing to ingest (non-client company, disabled sources, empty folder,
        # missing folder ids, OR a dead source — all handled + noted inside
        # list_files). Short-circuit BEFORE constructing the pipeline (no
        # Supabase/OpenAI touched), but carry the source notes through so the
        # caller can see why a source listed nothing.
        return IngestRunResult(source_notes=source_notes)

    if pipeline is None:
        # Only now (real files, no injected pipeline) do we need Supabase coords.
        if supabase_url is None or supabase_key is None:
            supabase_url, supabase_key = mc2_db.load_supabase_creds(
                cp_config.load(Path.cwd())
            )
        _configure_pipeline_once()
        factory = pipeline_factory or _build_pipeline
        pipeline = factory(folders.project_id, supabase_url, supabase_key)
        if folders.is_initiative:
            # The document-ingest pipeline hard-writes `project_id` on both
            # its dedup lookup and its rag_assets INSERT; an initiative id
            # there would FK-crash (project_id references projects). Rebind
            # both seams to the `initiative_id` owner column (mc-2 #192).
            _adapt_pipeline_for_initiative(pipeline, folders.project_id)

    result = IngestRunResult(source_notes=source_notes)
    # The single (owner column, owner id) pair for every rag_assets read/write
    # in this run — `initiative_id` for initiatives, `project_id` otherwise.
    owner_col, owner_val = _owner_filter(folders)
    run_root = Path(tmp_root) if tmp_root is not None else Path(tempfile.gettempdir())
    run_root.mkdir(parents=True, exist_ok=True)

    for file_ref in files:
        name = file_ref.name or ""
        if name.lower().endswith(_NON_INGESTABLE_EXTENSIONS):
            # Shortcut/pointer file — nothing to embed. Skip before download so
            # it doesn't churn the pipeline or get miscounted as a `failed`.
            result.skipped_shortcuts += 1
            continue
        # Ingest cache: if this file is unchanged since its last ingest (active
        # rag_asset's meta.change_token == the freshly-listed token), skip it
        # BEFORE any download/hash/embed — the `continue` guarantees none of those
        # run. `use_cache=False` (CLI --no-cache) forces a full re-scan.
        # Keyed on the run's owner pair so initiative-owned rows hit too.
        if use_cache and _unchanged_since_last_ingest(
            client, owner_val, file_ref, owner_col
        ):
            result.skipped_unchanged += 1
            continue
        # Stable, per-source temp dir so the download path (== dedup key) is
        # identical across runs. Cleaned in `finally` regardless of outcome.
        file_dir = _stable_dir_for(run_root, file_ref)
        try:
            file_dir.mkdir(parents=True, exist_ok=True)
            try:
                local = download_file(
                    file_ref, file_dir, drive_connector, dropbox_connector
                )
            except Exception as exc:  # download blew up — collect, keep going
                result.failed += 1
                result.failures.append((file_ref.name, str(exc)))
                continue

            # `file_path` here is BOTH the parse target AND the dedup key AND the
            # stored rag_assets.file_path — the pipeline uses the single arg for
            # all three. Because `local` is derived from a stable source id, it
            # re-derives identically on the next run → dedup → skip.
            file_path = str(local)

            # Cross-path content-duplicate guard. The pipeline dedups on
            # file_path only, so the SAME bytes at a new path (same file in Drive
            # AND Dropbox, or a copy within one source) would create a duplicate
            # row. If this content hash is already active in the project under a
            # DIFFERENT path, skip the file entirely — no parse/embed/insert.
            file_hash: str | None = None
            try:
                file_hash = _content_hash(local)
                if _existing_dup_at_other_path(
                    client, owner_val, file_hash, file_path, owner_col
                ):
                    result.deduped += 1
                    continue
            except Exception as exc:  # hashing failed — fall through to ingest
                # A hash failure must not lose the file: record it and let the
                # pipeline handle it (worst case a duplicate, the prior behavior).
                result.failures.append(
                    (file_ref.name, f"dedup pre-check failed: {exc}")
                )

            # The whole post-download body (ingest + action handling + stamp) is
            # guarded so a raise from ingest_file OR the stamp UPDATE is collected
            # and the loop continues — same collect-and-continue contract as the
            # download above. Without this, a transient PostgREST error on the
            # stamp (or a parser blowup) would propagate out, abort every
            # remaining file, AND replace the IngestRunResult with the exception.
            try:
                ingest_result = pipeline.ingest_file(
                    file_path,
                    title=file_ref.name,
                    url=_source_url(file_ref, drive_connector, dropbox_connector),
                )

                action = getattr(ingest_result, "action", "failed")
                if action in ("created", "versioned"):
                    # Count the ingest action FIRST (the asset is written). The
                    # stamp is a separate concern below so that a stamp failure
                    # doesn't undo a real created/versioned count.
                    if action == "created":
                        result.created += 1
                    else:
                        result.versioned += 1
                    # Stamp-failure policy: the asset IS ingested (it keeps the
                    # column-default scope='project', so it's not lost), only the
                    # company_id stamp didn't land. We DON'T count it as `failed`
                    # (the ingest succeeded) — instead we surface the stamp
                    # failure in `failures` so it's visible and re-runnable,
                    # while still leaving created/versioned incremented.
                    try:
                        _stamp_scope(client, folders, file_path, file_ref)
                    except Exception as exc:
                        result.failures.append(
                            (file_ref.name, f"scope-stamp failed: {exc}")
                        )
                    # Same-title supersede (#57): a re-ingest of a doc whose
                    # CONTENT changed lands at a fresh temp path, so the
                    # path-keyed pipeline dedup sees a brand-new file and
                    # 'created' a duplicate row under the same title. Chain the
                    # new row to the prior copy via prev_asset_id and retire
                    # the old ones (status='superseded' + chunks deleted) so
                    # retrieval only ever serves the newest. 'versioned' rows
                    # are excluded: the pipeline already chained + superseded
                    # their same-path predecessor. Failure policy matches the
                    # stamp: the ingest succeeded, so a supersede failure is
                    # surfaced in `failures` without touching the counts.
                    if action == "created":
                        try:
                            result.superseded += _supersede_same_title(
                                client,
                                folders,
                                title=file_ref.name,
                                file_path=file_path,
                                file_hash=file_hash,
                            )
                        except Exception as exc:
                            result.failures.append(
                                (file_ref.name, f"supersede failed: {exc}")
                            )
                elif action == "skipped":
                    # Already stamped on the run that first created it — no-op.
                    result.skipped += 1
                else:  # 'failed' (or anything unexpected)
                    result.failed += 1
                    err = getattr(ingest_result, "error", None) or f"action={action}"
                    result.failures.append((file_ref.name, err))
            except Exception as exc:  # ingest_file blew up — collect, keep going
                result.failed += 1
                result.failures.append((file_ref.name, f"ingest failed: {exc}"))
        finally:
            # Bytes are never persisted: drop the per-file temp dir whatever
            # happened (success, skip, or failure).
            shutil.rmtree(file_dir, ignore_errors=True)

    if result.skipped_shortcuts > 0:
        # Surface the skip so the row + button UI show why these files produced
        # nothing — without polluting the `failed`/`skipped` counts.
        result.source_notes.append(
            {
                "source": "skip",
                "note": (
                    f"skipped {result.skipped_shortcuts} non-ingestable "
                    "shortcut file(s) (.url/.lnk/.webloc)"
                ),
            }
        )

    return result


def _content_hash(path: Path | str) -> str:
    """SHA-256 of a file's raw bytes, hex-encoded.

    MUST match the document-ingest pipeline's own `compute_file_hash`
    (`hashlib.sha256` over the bytes, hexdigest) so the value we pre-check
    against `rag_assets.file_hash` is the SAME string the pipeline would store.
    Read in chunks so a large deck/PDF doesn't load fully into memory.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _existing_dup_at_other_path(
    client,
    project_id: str,
    file_hash: str,
    current_path: str,
    owner_col: str = "project_id",
) -> bool:
    """True if this project already has an ACTIVE asset with `file_hash` at a
    DIFFERENT `file_path` than `current_path`.

    This is the cross-path content-duplicate guard. The pipeline's own dedup
    keys strictly on `(project_id, file_path)`, so the SAME bytes arriving at a
    new path — the same file present in both Drive and Dropbox, or copied within
    one source — looks brand-new to it and creates a duplicate row. We catch that
    here by content hash BEFORE the parse/embed/insert.

    Crucially we exclude a same-path match: that case is the pipeline's province
    (it decides skip vs new_version), so we must not pre-empt it. Only a hash
    match at a DIFFERENT path means "this exact content is already ingested
    elsewhere in the project" → skip.

    `owner_col` selects the rag_assets owner column (`project_id` for
    engagements — the default, keeping existing callers/tests unchanged —
    `initiative_id` for initiatives; `project_id` here is the owning row's id
    for either kind, mirroring `_owner_filter`).

    Returns False on any query error — a dedup pre-check must never abort a run;
    worst case we fall back to the prior behavior (the pipeline ingests it).
    """
    try:
        resp = (
            client.table(Tables.RAG_ASSETS)
            .select("id, file_path")
            .eq(owner_col, project_id)
            .eq("file_hash", file_hash)
            .eq("status", "active")
            .execute()
        )
        rows = getattr(resp, "data", None) or []
    except Exception as exc:  # noqa: BLE001 — never let the pre-check abort the run
        # Visible, not silent: a query error here degrades to the prior behavior
        # (the pipeline ingests the file), but we surface it on stderr so a real
        # regression — e.g. a renamed column — doesn't masquerade as "no dup".
        print(
            f"[asset-ingest] dedup pre-check query failed ({exc}); "
            "ingesting without it",
            file=sys.stderr,
        )
        return False
    return any(r.get("file_path") != current_path for r in rows)


def _escape_like(value: str) -> str:
    """Escape PostgREST LIKE/ILIKE wildcards so `value` matches literally.

    `ilike` treats `%`/`_` as wildcards and `\\` as the escape char; a title
    like `Q1_report 100%.pdf` would otherwise match unrelated rows. Escaping
    all three turns the pattern into an exact (case-insensitive) equality.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _prior_same_title_assets(
    client,
    owner_col: str,
    owner_val: str,
    title: str,
    file_path: str,
    file_hash: str,
) -> list[dict]:
    """The project's OTHER active assets carrying this exact title.

    Same-title matching is case-insensitive EXACT equality (via an
    escaped-`ilike` pattern — no substring matching; `pull_source` groups by
    exact title string, which is precisely the collision we're preventing).
    Excludes:
      - the just-written row itself (same `file_path`), and any same-path row
        (that case is the pipeline's skip/new_version province);
      - same-hash rows (identical bytes — the cross-path dedup's province, and
        the issue's "same-title-same-hash stays a no-op").
    Returns the raw rows `[{id, created_at, file_hash, file_path}]`.
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .select("id, created_at, file_hash, file_path")
        .eq(owner_col, owner_val)
        .eq("status", "active")
        .ilike("title", _escape_like(title))
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return [
        r
        for r in rows
        if r.get("file_path") != file_path and r.get("file_hash") != file_hash
    ]


def _retire_asset(client, asset_id: str) -> None:
    """Retire ONE old asset: mark it superseded and drop its chunks.

    Mirrors the document-ingest pipeline's own new_version convention
    (`storage_api`: old row → `status='superseded'`), then goes one step
    further and DELETES the old row's `asset_chunks` (embeddings FK-cascade
    with the chunks) so no read path — even one that forgets the status
    filter — can ever serve the stale content. The asset ROW is kept: it is
    the prev_asset_id chain's history and may be referenced by spine
    `sources`.
    """
    client.table(Tables.RAG_ASSETS).update({"status": "superseded"}).eq(
        "id", asset_id
    ).execute()
    client.table(Tables.ASSET_CHUNKS).delete().eq("asset_id", asset_id).execute()


def _supersede_same_title(
    client,
    folders: ProjectFolders,
    *,
    title: str,
    file_path: str,
    file_hash: str | None,
) -> int:
    """Chain a just-'created' asset over its same-title predecessors (#57).

    When the project already held active asset(s) with the SAME title
    (case-insensitive) and DIFFERENT content (hash) at a DIFFERENT path:

      1. stamp the new row's `prev_asset_id` → the newest prior copy (the
         same chain shape the pipeline's own same-path new_version writes);
      2. retire every prior copy — `status='superseded'` + chunks deleted —
         so `pull_source` can never interleave old and new chunks under one
         title/citation.

    No-ops (returns 0) when there is no prior copy or when `file_hash` is
    unavailable (hashing failed earlier — without a hash we can't honor the
    "same-title-same-hash stays a no-op" contract, so we do nothing rather
    than risk retiring an identical copy).

    Returns the number of prior assets retired.
    """
    if not title or file_hash is None:
        return 0
    owner_col, owner_val = _owner_filter(folders)
    priors = _prior_same_title_assets(
        client, owner_col, owner_val, title, file_path, file_hash
    )
    if not priors:
        return 0
    priors.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    # Chain the new row (found by the same dedup key the scope stamp uses:
    # owner + file_path + active is unique) to the NEWEST prior copy.
    client.table(Tables.RAG_ASSETS).update(
        {"prev_asset_id": priors[0]["id"]}
    ).eq(owner_col, owner_val).eq("file_path", file_path).eq(
        "status", "active"
    ).execute()
    for prior in priors:
        _retire_asset(client, prior["id"])
    return len(priors)


def _unchanged_since_last_ingest(
    client, project_id: str, file_ref, owner_col: str = "project_id"
) -> bool:
    """True iff an active rag_asset for this (provider, file) already carries a
    meta.change_token equal to the freshly-listed token → unchanged → safe to
    skip download+embed. Fail-open: missing row / missing or mismatched token /
    None token / any error → False (ingest normally). Keyed on
    (owner, source_provider, source_file_id) so a Drive token is never
    compared against a Dropbox one (different hash algorithms). `owner_col`
    selects the owner column (`initiative_id` for initiatives, per
    `_owner_filter`; the `project_id` default keeps engagement callers/tests
    unchanged — the arg is the owning row's id for either kind).
    """
    token = getattr(file_ref, "change_token", None)
    if not token:
        return False  # no token (e.g. Google-native) → can't prove unchanged
    try:
        rows = (
            client.table(Tables.RAG_ASSETS)
            .select("meta")  # explicit, never *
            .eq(owner_col, project_id)
            .eq("source_provider", file_ref.source)
            .eq("source_file_id", file_ref.id)
            .eq("status", "active")
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return False
        stored = (rows[0].get("meta") or {}).get("change_token")
        return stored is not None and stored == token
    except Exception as exc:  # noqa: BLE001 — fail-open: skip-check failure must
        # never crash the run or wrongly skip; degrade to "ingest normally". But
        # surface it on stderr (matching _existing_dup_at_other_path) so a real
        # regression — e.g. a renamed column — doesn't silently make every file
        # re-ingest with no signal.
        print(
            f"[asset-ingest] ingest-cache skip-check failed for "
            f"{file_ref.id} ({exc}); ingesting without it",
            file=sys.stderr,
        )
        return False


def _stamp_scope(
    client, folders: ProjectFolders, file_path: str, file_ref: FileRef
) -> None:
    """Apply the 1P scope stamp + source re-fetch coords to the just-ingested row.

    The spec wanted `UPDATE rag_assets SET scope, company_id WHERE id=<asset_id>`,
    but IngestResult exposes NO asset_id (the pipeline computes it internally and
    keeps it). So we stamp by the DEDUP KEY instead: the migration makes
    `(project_id, file_path) WHERE status='active'` a UNIQUE index, so this WHERE
    clause matches exactly one row — the asset we just wrote. `file_path` MUST be
    the same value passed to `ingest_file` (it is: both come from `local`). The
    pipeline already set project_id; what the stamp adds is company_id (and it
    re-affirms scope='project', which is also the column default).

    The same UPDATE also persists the durable re-fetch coords from the `FileRef`
    (`source_provider`/`source_file_id`/`source_path`): these can't flow through
    `pipeline.ingest_file`, whose signature only takes (file_path, title, url), so
    this post-ingest stamp is where they land. `source_path` is the Dropbox
    path_display, `None` for Drive files (which re-fetch by id).

    OWNER COLUMN (Task 8): the dedup-key WHERE must filter on whichever owner the
    pipeline wrote — `initiative_id` for an initiative, `project_id` for an
    engagement (mirrors `ingest._owner_column`). `folders.project_id` carries the
    owning row's id for BOTH kinds; `is_initiative` selects the column.
    """
    owner_col, owner_val = _owner_filter(folders)
    payload = {
        "scope": "project",
        "company_id": folders.company_id,
        "source_provider": file_ref.source,
        "source_file_id": file_ref.id,
        "source_path": file_ref.path,
    }
    # Stamp the provider content hash into `meta.change_token` so a LATER run can
    # compare it and skip an unchanged file (ingest caching). `meta` is a jsonb
    # column the pipeline may already populate with embedding/chunk keys, so we
    # MERGE the token in (read-modify-write) rather than clobber the whole column.
    # No token (e.g. a Google-native file) → leave meta untouched (don't write a
    # None token), keeping the existing source_* stamp behavior unchanged.
    if file_ref.change_token is not None:
        resp = (
            client.table(Tables.RAG_ASSETS)
            .select("meta")
            .eq(owner_col, owner_val)
            .eq("file_path", file_path)
            .eq("status", "active")
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        current_meta = rows[0].get("meta") if rows else None
        payload["meta"] = {
            **(current_meta or {}),
            "change_token": file_ref.change_token,
        }
    client.table(Tables.RAG_ASSETS).update(payload).eq(owner_col, owner_val).eq(
        "file_path", file_path
    ).eq("status", "active").execute()


def _owner_filter(folders: ProjectFolders) -> tuple[str, str]:
    """The single (column, value) owner pair for a rag_assets row.

    `rag_assets` (migration 081) carries BOTH `project_id` (FK projects) and
    `initiative_id` (FK initiatives) under a `num_nonnulls(...) == 1` CHECK —
    exactly one owner. Filtering/writing the WRONG column for an initiative
    would miss the row (or FK-crash on insert). `folders.project_id` holds the
    owning id for both kinds; `is_initiative` picks the column. Mirrors
    `ingest._owner_column`.
    """
    if folders.is_initiative:
        return "initiative_id", folders.project_id
    return "project_id", folders.project_id


# ──────────────────────────────────────────────────────────────────────
#  Task C6 — scope-transition verbs (pure rag_assets re-tags + review SELECT)
#
#  These flip `rag_assets.scope ∈ {project, account, archived}` and stamp the
#  matching lifecycle timestamp. They NEVER touch chunks or embeddings: those
#  key to the asset row, which doesn't move — only its scope tag changes.
#
#  Affected-row counting: supabase-py's `update(...).execute()` returns the
#  updated rows in `.data` (PostgREST returns the representation by default), so
#  `len(resp.data)` is the count of rows the WHERE clause matched. We rely on
#  that for the bool / int returns below.
#
#  Timestamps: supabase-py can't pass a raw SQL `now()` into `.update({...})`, so
#  we generate a UTC ISO timestamp in Python — the established cp_engine pattern
#  (see fathom._now_iso / sync). Testable and unambiguous.
# ──────────────────────────────────────────────────────────────────────


# Review-gate columns. Explicit list (never `*`) per Drew's global Supabase rule
# — rag_assets carries large text/JSONB columns (chunk text, embeddings live in a
# sibling table but `meta` can be sizeable) we don't want to haul over the wire.
_PROMOTABLE_COLUMNS = "id, title, url, meta"


def _utc_now_iso() -> str:
    """UTC ISO-8601 timestamp for lifecycle columns (promoted_at/archived_at)."""
    return datetime.now(UTC).isoformat()


def _affected_count(resp) -> int:
    """Number of rows an update touched, from its `.data` representation."""
    return len(getattr(resp, "data", None) or [])


def list_project_files_annotated(
    project_code: str,
    *,
    mc_project_id: str | None = None,
    client=None,
    supabase_url: str | None = None,
    supabase_key: str | None = None,
    use_cache: bool = True,
) -> dict:
    """List a project's cloud files for the ingest PICKER, each annotated with
    whether it's already ingested. Synchronous (no download/embed) — this is the
    read the picker awaits before showing the tree.

    Resolves folders exactly as `ingest_project_assets` does (by-id preferred,
    by-code fallback), builds connectors, calls `list_files`, then batch-joins
    the discovered files against this owner's ACTIVE rag_assets to set
    `already_ingested` per file. A file counts as already ingested when its
    source id matches a live row's `source_file_id` OR its content hash matches
    that row's `meta.change_token` (Drew's answer to Q1: match on both).

    Returns a JSON-able dict:
        {"project_found": bool,
         "unconfigured_reason": str | None,
         "source_notes": [{"source","note"}, ...],
         "files": [{"key","source","id","name","mime_type","size","modified",
                    "path","folder_path":[...],"already_ingested":bool}, ...]}
    """
    from cp_engine import mc2_db

    if client is None:
        if supabase_url is not None and supabase_key is not None:
            client = mc2_db.get_client(url=supabase_url, key=supabase_key)
        else:
            from cp_engine import config as cp_config

            client = mc2_db.get_client(cp_config.load(Path.cwd()))

    folders = (
        resolve_project_folders_by_id(client, mc_project_id)
        if mc_project_id
        else resolve_project_folders(client, project_code)
    )
    if folders is None:
        return {
            "project_found": False,
            "unconfigured_reason": None,
            "source_notes": [],
            "files": [],
        }

    unconfigured = folders_unconfigured_reason(folders)
    if unconfigured is not None:
        return {
            "project_found": True,
            "unconfigured_reason": unconfigured,
            "source_notes": [{"source": "config", "note": unconfigured}],
            "files": [],
        }

    drive_connector = None
    dropbox_connector = None
    if getattr(folders, "enable_google_drive", False):
        try:
            from cloud_storage.google_drive_connector import GoogleDriveConnector

            drive_connector = GoogleDriveConnector(service_account_file=None)
        except Exception:  # noqa: BLE001 — list_files notes the per-source failure
            drive_connector = None
    if getattr(folders, "enable_dropbox", False):
        try:
            dropbox_connector = _dropbox_connector()
        except Exception:  # noqa: BLE001 — list_files notes the per-source failure
            dropbox_connector = None

    files, source_notes = list_files(
        folders,
        drive_connector,
        dropbox_connector,
        allowlist=folders.asset_ingest_folders,
        use_cache=use_cache,
    )

    # Batch-join against this owner's active rag_assets. Pull the two match keys
    # (source_file_id and meta.change_token) for every live row ONCE, then set
    # already_ingested per file in memory — one query, not one-per-file.
    owner_col, owner_val = _owner_filter(folders)
    ingested_ids: set[str] = set()
    ingested_tokens: set[str] = set()
    try:
        rows = (
            client.table(Tables.RAG_ASSETS)
            .select("source_file_id, meta")
            .eq(owner_col, owner_val)
            .eq("status", "active")
            .execute()
            .data
            or []
        )
        for r in rows:
            sid = r.get("source_file_id")
            if sid:
                ingested_ids.add(sid)
            tok = (r.get("meta") or {}).get("change_token")
            if tok:
                ingested_tokens.add(tok)
    except Exception as exc:  # noqa: BLE001 — annotation is best-effort; a join
        # failure must not blank the picker. Degrade to "nothing known ingested"
        # (everything pickable) and note it rather than crash.
        print(
            f"[asset-ingest] already-ingested check failed ({exc}); "
            "listing all files as pickable",
            file=sys.stderr,
        )
        source_notes = [
            *source_notes,
            {"source": "config", "note": f"already-ingested check failed: {exc}"},
        ]

    out_files = []
    for f in files:
        already = f.id in ingested_ids or (
            f.change_token is not None and f.change_token in ingested_tokens
        )
        out_files.append({
            "key": file_selection_key(f),
            "source": f.source,
            "id": f.id,
            "name": f.name,
            "mime_type": f.mime_type,
            "size": f.size,
            "modified": f.modified,
            "path": f.path,
            # `_folder_segments` derives the real ancestry for BOTH sources:
            # Drive already carries a breadcrumb in folder_path, but Dropbox
            # files only carry it in path_display — the raw FileRef.folder_path
            # is empty for every Dropbox file, which collapsed the whole tree
            # into one "(root)" group in the picker. Derive it here so folders
            # group correctly.
            "folder_path": _folder_segments(f),
            "already_ingested": already,
        })

    return {
        "project_found": True,
        "unconfigured_reason": None,
        "source_notes": source_notes,
        "files": out_files,
    }


def promote_asset(client, asset_id: str) -> bool:
    """Promote a project-scoped asset to account scope (human curation).

    `UPDATE rag_assets SET scope='account', promoted_at=now()
     WHERE id=<asset_id> AND scope='project'`.

    The `scope='project'` filter makes this idempotent and atomic: promoting an
    already-account asset (or a missing id) matches 0 rows — a clean no-op.
    The asset's company_id was set at ingest, so promotion only flips the scope.

    Returns True if a row was promoted, False on the already-account/not-found
    no-op.
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .update({"scope": "account", "promoted_at": _utc_now_iso()})
        .eq("id", asset_id)
        .eq("scope", "project")
        .execute()
    )
    return _affected_count(resp) > 0


def demote_asset(client, asset_id: str) -> bool:
    """Reverse a promotion: account scope back to project, clearing promoted_at.

    `UPDATE rag_assets SET scope='project', promoted_at=NULL
     WHERE id=<asset_id> AND scope='account'`.

    Returns True if a row was demoted, False on the no-op (already project /
    not found).
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .update({"scope": "project", "promoted_at": None})
        .eq("id", asset_id)
        .eq("scope", "account")
        .execute()
    )
    return _affected_count(resp) > 0


def archive_project_assets(client, project_id: str) -> int:
    """Archive a project's un-promoted assets on project close.

    `UPDATE rag_assets SET scope='archived', archived_at=now()
     WHERE project_id=<project_id> AND scope='project'`.

    CRITICAL: the `scope='project'` filter is the guard — only un-promoted
    assets archive. Account-scoped (already promoted) assets belong to the
    company now, not the project, and are left untouched.

    Returns the count of assets archived.
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .update({"scope": "archived", "archived_at": _utc_now_iso()})
        .eq("project_id", project_id)
        .eq("scope", "project")
        .execute()
    )
    return _affected_count(resp)


def unarchive_project_assets(client, project_id: str) -> int:
    """Restore a project's archived assets back to project scope (recovery).

    `UPDATE rag_assets SET scope='project', archived_at=NULL
     WHERE project_id=<project_id> AND scope='archived'`.

    Returns the count of assets restored.
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .update({"scope": "project", "archived_at": None})
        .eq("project_id", project_id)
        .eq("scope", "archived")
        .execute()
    )
    return _affected_count(resp)


def list_promotable(client, project_id: str) -> list[dict]:
    """The review-gate surface: project-scoped active assets a human can promote.

    `SELECT id, title, url, meta FROM rag_assets
     WHERE scope='project' AND status='active' AND project_id=<project_id>`.

    Returns each row shaped for a human decision: id, title, url, and the
    classifier's decision lifted out of `meta` (stored as
    `meta->>'classifier_decision'`). `meta` is carried through too in case the
    caller wants more context.
    """
    resp = (
        client.table(Tables.RAG_ASSETS)
        .select(_PROMOTABLE_COLUMNS)
        .eq("scope", "project")
        .eq("status", "active")
        .eq("project_id", project_id)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    out: list[dict] = []
    for row in rows:
        meta = row.get("meta") or {}
        classifier = meta.get("classifier_decision") if isinstance(meta, dict) else None
        out.append(
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "url": row.get("url"),
                "classifier_decision": classifier,
                "meta": row.get("meta"),
            }
        )
    return out


# ──────────────────────────────────────────────────────────────────────
#  Task C9 — the scoped read query (the consumption payoff; Spec C uses it)
# ──────────────────────────────────────────────────────────────────────


def read_scoped_chunks(
    client,
    project_id: str,
    company_id: str,
    query_embedding: list[float] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Read a project's readable asset text: its own + its company's account assets.

    This is how engines retrieve the text a project is allowed to read: the
    project's OWN project-scoped assets PLUS its company's account-scoped (shared)
    assets, EXCLUDING archived. When a `query_embedding` is given the rows are
    ordered by pgvector cosine distance (`embedding <=> query`); otherwise by
    recency. Spec §7 SQL (the contract):

        SELECT c.text, c.meta->>'citation_url' AS cite, a.title, a.scope
        FROM asset_chunks c JOIN rag_assets a ON a.id = c.asset_id
        WHERE a.status='active'
          AND ( (a.scope='project' AND a.project_id = :project_id)
             OR (a.scope='account' AND a.company_id = :company_id) )
        ORDER BY (embedding <=> :query_vec)   -- vector search when given
        LIMIT :k;

    WHY THIS IS AN RPC, NOT A POSTGREST CHAIN: supabase-py's PostgREST query
    builder cannot cleanly express (a) the JOIN across `asset_chunks` and
    `rag_assets`, (b) the OR across two different columns (project_id vs
    company_id), or (c) the pgvector `<=>` distance ordering. The right home for
    that SQL is a Postgres function. So this calls a DB RPC and returns its rows;
    the SQL stays in the database where it belongs.

    REQUIRED — Phase B migration (056) created this RPC; migration 128 (mc-2)
    reshaped it for document order (#152). Expected signature:

        read_scoped_asset_chunks(
            p_project_id     uuid,
            p_company_id     uuid,
            p_query_embedding vector(1024) default null,
            p_limit          int          default 20
        ) returns table (
            text         text,
            citation_url text,
            title        text,
            scope        text,
            chunk_index  integer,   -- meta.chunk_index (null pre-stamp)
            page         numeric    -- meta.page (PDF ingests; else null)
        )

      Body contract:
        - JOIN asset_chunks c ON rag_assets a (a.id = c.asset_id)
        - WHERE a.status = 'active'
          AND ( (a.scope = 'project' AND a.project_id = p_project_id)
             OR (a.scope = 'account' AND a.company_id = p_company_id) )
          -- archived rows are excluded by status='active' + the scope union
        - ORDER BY c.embedding <=> p_query_embedding when p_query_embedding IS
          NOT NULL, else by a.created_at DESC, then chunk_index / page nulls
          last (document order within a doc — #152)
        - LIMIT p_limit
        - select explicit columns only — never SELECT *.

    Returns the RPC's `.data` (a list of dict rows shaped {text, citation_url,
    title, scope, chunk_index, page}).
    """
    resp = client.rpc(
        "read_scoped_asset_chunks",
        {
            "p_project_id": project_id,
            "p_company_id": company_id,
            "p_query_embedding": query_embedding,
            "p_limit": limit,
        },
    ).execute()
    return getattr(resp, "data", None) or []
