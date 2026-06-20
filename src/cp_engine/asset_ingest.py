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
applies the 1P scope stamp to each freshly-written `rag_assets` row.

Scope guard: asset ingest targets *client* companies only. Self-fpsf /
self-canonic companies are house/framework territory and out of scope; `list_files`
returns `[]` for them.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Columns we read from MC-2's `projects` table. Explicit list (never `*`) per
# Drew's global Supabase rule — the projects table carries multi-megabyte JSONB
# cache columns (`cached_messages`, `cached_analysis`) we must not pull. The
# nested `companies(kind)` embed gives us the scope guard (client vs self-*).
_PROJECT_COLUMNS = (
    "id, company_id, google_drive_folder_id, mc_dropbox_folder_id, "
    "enable_google_drive, enable_dropbox, asset_ingest_folders, companies(kind)"
)


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


# ──────────────────────────────────────────────────────────────────────
#  Resolve
# ──────────────────────────────────────────────────────────────────────


def _project_number(project_code: str) -> int | None:
    """Extract the trailing numeric part of a cp code (`ibx-5153` → 5153).

    Returns None when the code carries no number — initiatives use slug codes
    with no number and standalone repos are bare slugs; neither maps to a
    `projects.number`, so asset ingest simply doesn't apply.
    """
    if not project_code:
        return None
    match = re.search(r"(\d+)", project_code)
    return int(match.group(1)) if match else None


def _row_to_folders(row: dict) -> ProjectFolders:
    """Map one MC-2 `projects` row (selected via `_PROJECT_COLUMNS`) to a
    ProjectFolders. Shared by both resolve paths (by-number and by-id) so the
    companies-embed guard + field mapping live in exactly one place."""
    # PostgREST returns the to-one `companies` embed as either a dict or a
    # single-element list (shape varies); defend against both — see the same
    # guard in sync_mc2._engagement_canonical_id / _repo_row_to_state.
    companies = row.get("companies") or {}
    if isinstance(companies, list):
        companies = companies[0] if companies else {}
    company_kind = (companies.get("kind") if isinstance(companies, dict) else "") or ""

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
    uses). Returns None when the code has no number or no matching project row.

    NOTE: this resolves by `projects.number` parsed out of the code. That works
    for bare-numeric / `<co>-<number>` codes, but NOT for slug-style codes
    (`SAP-vision-update-2026`, `IBX-ai-campaign`) which carry no number — and
    worse, a year embedded in the slug (`…-2026`) would be mis-parsed as the
    project number. When the caller has the MC-2 row id (e.g. the mc-2 button),
    prefer `resolve_project_folders_by_id`, which is authoritative.
    """
    number = _project_number(project_code)
    if number is None:
        print(
            f"[asset-ingest] no numeric part in '{project_code}' — not a "
            "numbered engagement; skipping asset resolve",
            file=sys.stderr,
        )
        return None

    rows = (
        client.table("projects")
        .select(_PROJECT_COLUMNS)
        .eq("number", number)
        .execute()
        .data
        or []
    )
    if not rows:
        print(
            f"[asset-ingest] no MC-2 project with number={number} "
            f"(code '{project_code}')",
            file=sys.stderr,
        )
        return None

    return _row_to_folders(rows[0])


def resolve_project_folders_by_id(
    client, mc_project_id: str
) -> ProjectFolders | None:
    """Look up the Drive/Dropbox folders for an MC-2 project by its row `id`.

    This is the authoritative resolution path: `mc_project_id` is `projects.id`,
    so there is no number-parsing and no risk of mis-reading a year embedded in
    a slug code (`SAP-vision-update-2026`). The mc-2 "Ingest assets" button
    already has this id; prefer it over `resolve_project_folders` (by-number)
    whenever available. Returns None when no row matches.
    """
    rows = (
        client.table("projects")
        .select(_PROJECT_COLUMNS)
        .eq("id", mc_project_id)
        .execute()
        .data
        or []
    )
    if not rows:
        print(
            f"[asset-ingest] no MC-2 project with id={mc_project_id}",
            file=sys.stderr,
        )
        return None

    return _row_to_folders(rows[0])


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
            fields="files(id,name,mimeType,size,modifiedTime)",
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


def list_files(
    folders: ProjectFolders,
    drive_connector=None,
    dropbox_connector=None,
    allowlist: tuple[str, ...] = (),
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
    """
    source_notes: list[dict[str, str]] = []

    if folders.company_kind != "client":
        print(
            f"[asset-ingest] skipping non-client company kind="
            f"'{folders.company_kind}' (asset ingest targets clients only)",
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
                results.extend(
                    _list_drive(drive_connector, folders.google_drive_folder_id)
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
                    from cloud_storage.dropbox_connector import DropboxConnector

                    dropbox_connector = DropboxConnector()
                results.extend(
                    _list_dropbox(dropbox_connector, folders.mc_dropbox_folder_id)
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
            from cloud_storage.dropbox_connector import DropboxConnector

            dropbox_connector = DropboxConnector()
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
    failures: list[tuple[str, str]] = field(default_factory=list)
    project_found: bool = True
    # Per-source listing notes from list_files: a dead/skipped source records a
    # {"source", "note"} entry here rather than aborting the whole run, so the
    # caller (and the button UI) can show why a source produced nothing.
    source_notes: list[dict[str, str]] = field(default_factory=list)


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
    """
    # Resolve creds lazily, and ONLY the pieces actually missing — so the unit-
    # test path (injected client + pipeline) never touches cp config / Supabase.
    def _resolve_creds() -> tuple[str, str]:
        from cp_engine import config as cp_config
        from cp_engine.sync_mc2 import _load_supabase_creds

        return _load_supabase_creds(cp_config.load(Path.cwd()))

    # Build the MC-2 client up front (needed for resolve + the scope stamp).
    if client is None:
        if supabase_url is None or supabase_key is None:
            supabase_url, supabase_key = _resolve_creds()
        from supabase import create_client

        client = create_client(supabase_url, supabase_key)

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
            from cloud_storage.dropbox_connector import DropboxConnector

            dropbox_connector = DropboxConnector()
        except Exception:  # noqa: BLE001 — list_files handles + notes the failure
            dropbox_connector = None

    files, source_notes = list_files(
        folders,
        drive_connector,
        dropbox_connector,
        allowlist=folders.asset_ingest_folders,
    )
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
            supabase_url, supabase_key = _resolve_creds()
        _configure_pipeline_once()
        factory = pipeline_factory or _build_pipeline
        pipeline = factory(folders.project_id, supabase_url, supabase_key)

    result = IngestRunResult(source_notes=source_notes)
    run_root = Path(tmp_root) if tmp_root is not None else Path(tempfile.gettempdir())
    run_root.mkdir(parents=True, exist_ok=True)

    for file_ref in files:
        name = file_ref.name or ""
        if name.lower().endswith(_NON_INGESTABLE_EXTENSIONS):
            # Shortcut/pointer file — nothing to embed. Skip before download so
            # it doesn't churn the pipeline or get miscounted as a `failed`.
            result.skipped_shortcuts += 1
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
            try:
                file_hash = _content_hash(local)
                if _existing_dup_at_other_path(
                    client, folders.project_id, file_hash, file_path
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
    client, project_id: str, file_hash: str, current_path: str
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

    Returns False on any query error — a dedup pre-check must never abort a run;
    worst case we fall back to the prior behavior (the pipeline ingests it).
    """
    try:
        resp = (
            client.table("rag_assets")
            .select("id, file_path")
            .eq("project_id", project_id)
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
    """
    client.table("rag_assets").update(
        {
            "scope": "project",
            "company_id": folders.company_id,
            "source_provider": file_ref.source,
            "source_file_id": file_ref.id,
            "source_path": file_ref.path,
        }
    ).eq("project_id", folders.project_id).eq("file_path", file_path).eq(
        "status", "active"
    ).execute()


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
        client.table("rag_assets")
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
        client.table("rag_assets")
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
        client.table("rag_assets")
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
        client.table("rag_assets")
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
        client.table("rag_assets")
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

    REQUIRED — Phase B migration (056) MUST create this RPC. Expected signature:

        read_scoped_asset_chunks(
            p_project_id     uuid,
            p_company_id     uuid,
            p_query_embedding vector(1024) default null,
            p_limit          int          default 20
        ) returns table (
            text         text,
            citation_url text,
            title        text,
            scope        text
        )

      Body contract:
        - JOIN asset_chunks c ON rag_assets a (a.id = c.asset_id)
        - WHERE a.status = 'active'
          AND ( (a.scope = 'project' AND a.project_id = p_project_id)
             OR (a.scope = 'account' AND a.company_id = p_company_id) )
          -- archived rows are excluded by status='active' + the scope union
        - ORDER BY c.embedding <=> p_query_embedding when p_query_embedding IS NOT
          NULL, else by a.created_at DESC (recency fallback)
        - LIMIT p_limit
        - select explicit columns only (c.text, c.meta->>'citation_url', a.title,
          a.scope) — never SELECT *.

    Returns the RPC's `.data` (a list of dict rows shaped {text, citation_url,
    title, scope}).
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
