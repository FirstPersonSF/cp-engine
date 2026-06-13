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

Download / extraction / scope-stamping are deliberately NOT here — they are the
next task. Keep this module list-only.

Scope guard: asset ingest targets *client* companies only. Self-fpsf /
self-canonic companies are house/framework territory and out of scope; `list_files`
returns `[]` for them.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Columns we read from MC-2's `projects` table. Explicit list (never `*`) per
# Drew's global Supabase rule — the projects table carries multi-megabyte JSONB
# cache columns (`cached_messages`, `cached_analysis`) we must not pull. The
# nested `companies(kind)` embed gives us the scope guard (client vs self-*).
_PROJECT_COLUMNS = (
    "id, company_id, google_drive_folder_id, mc_dropbox_folder_id, "
    "enable_google_drive, enable_dropbox, companies(kind)"
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


def resolve_project_folders(client, project_code: str) -> ProjectFolders | None:
    """Look up the Drive/Dropbox folders for `project_code` from MC-2.

    `client` is a Supabase client (the same `create_client(url, key)` MC2Backend
    uses). Returns None when the code has no number or no matching project row.
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

    row = rows[0]
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


def _list_drive(connector, folder_id: str) -> list[FileRef]:
    """List a Drive folder via the connector's pagination primitive.

    `_list_files_with_pagination` is the listing primitive on GoogleDriveConnector
    (it has no simple public `list(folder_id)`); it returns dicts with the fields
    we request. We query direct children of the folder, excluding trashed files.
    """
    items = connector._list_files_with_pagination(
        query=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,size,modifiedTime)",
        page_size=100,
    )
    return [
        FileRef(
            source="drive",
            id=item.get("id"),
            name=item.get("name"),
            mime_type=item.get("mimeType"),
            size=_coerce_size(item.get("size")),
            modified=item.get("modifiedTime"),
            path=None,
        )
        for item in items
    ]


def _list_dropbox(connector, folder: str) -> list[FileRef]:
    """List a Dropbox folder via the low-level `dbx.files_list_folder`.

    ASSUMPTION (documented for the next task): MC-2's `mc_dropbox_folder_id` is
    treated as a value `files_list_folder` accepts directly — i.e. a folder path
    (`/Clients/...`) or a Dropbox folder id (`id:...`), both of which that API
    handles. The connector's public `list_project_files` is path-derived from
    project_code/name and does NOT take the stored folder id, so we use the
    underlying `dbx` client and normalize the FileMetadata entries ourselves.
    Pagination (`has_more`/`cursor`) is left to the download task; client folders
    here are small. We filter to FileMetadata (skip subfolders) defensively by
    duck-typing on `path_display`.
    """
    result = connector.dbx.files_list_folder(folder)
    refs: list[FileRef] = []
    for entry in getattr(result, "entries", []) or []:
        # FileMetadata has size + client_modified; FolderMetadata does not.
        if not hasattr(entry, "size"):
            continue
        refs.append(
            FileRef(
                source="dropbox",
                id=getattr(entry, "id", None),
                name=getattr(entry, "name", None),
                mime_type=None,  # Dropbox doesn't expose a mime type on list
                size=_coerce_size(getattr(entry, "size", None)),
                modified=str(getattr(entry, "client_modified", None) or "") or None,
                path=getattr(entry, "path_display", None),
            )
        )
    return refs


def list_files(
    folders: ProjectFolders,
    drive_connector=None,
    dropbox_connector=None,
) -> list[FileRef]:
    """Enumerate the files in a project's enabled Drive + Dropbox folders.

    Connectors are injectable for testability; when omitted they are constructed
    from the environment (service-account file / Dropbox refresh-token creds).

    Skips (not errors, surfaced as stderr notes):
      - non-client company kinds (out of scope)
      - a source with its enable flag off
      - a source missing its folder id

    Config gaps (non-client company, disabled source, missing folder id) are
    skipped with a stderr note. Connector/API errors (auth failure, bad folder
    id, rate limit) propagate uncaught — the caller handles per-source failure.
    """
    if folders.company_kind != "client":
        print(
            f"[asset-ingest] skipping non-client company kind="
            f"'{folders.company_kind}' (asset ingest targets clients only)",
            file=sys.stderr,
        )
        return []

    results: list[FileRef] = []

    # ── Drive ──
    if folders.enable_google_drive:
        if folders.google_drive_folder_id:
            if drive_connector is None:
                from cloud_storage.google_drive_connector import GoogleDriveConnector

                drive_connector = GoogleDriveConnector(service_account_file=None)
            results.extend(
                _list_drive(drive_connector, folders.google_drive_folder_id)
            )
        else:
            print(
                "[asset-ingest] enable_google_drive set but no "
                "google_drive_folder_id — skipping Drive",
                file=sys.stderr,
            )

    # ── Dropbox ──
    if folders.enable_dropbox:
        if folders.mc_dropbox_folder_id:
            if dropbox_connector is None:
                from cloud_storage.dropbox_connector import DropboxConnector

                dropbox_connector = DropboxConnector()
            results.extend(
                _list_dropbox(dropbox_connector, folders.mc_dropbox_folder_id)
            )
        else:
            print(
                "[asset-ingest] enable_dropbox set but no "
                "mc_dropbox_folder_id — skipping Dropbox",
                file=sys.stderr,
            )

    return results


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
        local_path = tmp_dir / file_ref.name
        _metadata, response = dropbox_connector.dbx.files_download(path=file_ref.path)
        local_path.write_bytes(response.content)
        return local_path

    raise ValueError(
        f"[asset-ingest] cannot download FileRef with unknown source "
        f"'{file_ref.source}' (expected 'drive' or 'dropbox')"
    )
