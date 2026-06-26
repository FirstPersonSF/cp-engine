"""Tests for the per-project folder allowlist (asset-ingest folder filter).

Three layers, all pure / mock-only (no Supabase, Drive, or Dropbox):

  - `_folder_segments(ref)` — the FOLDER segments of a FileRef (filename dropped).
  - `_matches_allowlist(ref, allowlist)` — segment-CONTAINS, case-insensitive.
  - `list_files(..., allowlist=...)` — partitions kept vs excluded + a filter
    source_note; empty allowlist is a true no-op (NO filtering, NO note).

The locked behavior (see the 2026-06-16 design doc "Decisions" section):
  - numbered prefix MATCHES ("01 Client Assets" contains "Client Assets").
  - case-insensitive + suffix ("client assets v2" vs "Client Assets") matches.
  - the match is against FOLDER segments ONLY, never the filename.
"""

from __future__ import annotations

from cp_engine.asset_ingest import (
    FileRef,
    IngestRunResult,
    ProjectFolders,
    _folder_segments,
    _matches_allowlist,
    _row_to_folders,
    ingest_project_assets,
    list_files,
    resolve_project_folders_by_id,
)

# Reuse the Supabase + Dropbox/Drive connector fakes from the listing tests so
# the list_files-level allowlist tests + the resolve test build their fakes
# exactly like the existing tests do.
from tests.test_asset_ingest_listing import (
    _FakeClient,
    _FakeDriveConnector,
    _FakeDropboxConnector,
    _FakeDropboxEntry,
)


# ──────────────────────────────────────────────────────────────────────
#  Builders
# ──────────────────────────────────────────────────────────────────────


def _dbx_ref(path: str, name: str | None = None) -> FileRef:
    """A Dropbox FileRef whose segments derive from `path` (path_display)."""
    return FileRef(
        source="dropbox",
        id="id:x",
        name=name or path.rsplit("/", 1)[-1],
        mime_type=None,
        size=10,
        modified="2026-06-16",
        path=path,
    )


def _drive_ref(folder_path: tuple[str, ...], name: str) -> FileRef:
    """A Drive FileRef whose segments are the recorded folder_path breadcrumb."""
    return FileRef(
        source="drive",
        id="d:x",
        name=name,
        mime_type="application/pdf",
        size=10,
        modified="2026-06-16",
        path=None,
        folder_path=folder_path,
    )


def _client_folders(**overrides) -> ProjectFolders:
    base = dict(
        project_id="p",
        company_id="c",
        company_kind="client",
        google_drive_folder_id="drive-root",
        mc_dropbox_folder_id="/SAP 5174",
        enable_google_drive=True,
        enable_dropbox=True,
    )
    base.update(overrides)
    return ProjectFolders(**base)


# ──────────────────────────────────────────────────────────────────────
#  _folder_segments
# ──────────────────────────────────────────────────────────────────────


def test_segments_dropbox_drops_filename() -> None:
    ref = _dbx_ref("/SAP 5174/01 Client Assets/brief.pdf")
    assert _folder_segments(ref) == ["SAP 5174", "01 Client Assets"]


def test_segments_drive_uses_folder_path() -> None:
    ref = _drive_ref(("01 Client Assets",), "brief.pdf")
    assert _folder_segments(ref) == ["01 Client Assets"]


def test_segments_dropbox_none_path_is_empty() -> None:
    ref = _dbx_ref("/x/y.pdf")
    ref.path = None
    assert _folder_segments(ref) == []


def test_segments_dropbox_empty_path_is_empty() -> None:
    ref = _dbx_ref("/x/y.pdf")
    ref.path = ""
    assert _folder_segments(ref) == []


def test_segments_both_sources_identical_for_equivalent_structure() -> None:
    dbx = _dbx_ref("/SAP 5174/01 Client Assets/brief.pdf")
    drv = _drive_ref(("SAP 5174", "01 Client Assets"), "brief.pdf")
    assert _folder_segments(dbx) == _folder_segments(drv) == [
        "SAP 5174",
        "01 Client Assets",
    ]


# ──────────────────────────────────────────────────────────────────────
#  _matches_allowlist
# ──────────────────────────────────────────────────────────────────────


def test_match_numbered_prefix() -> None:
    # "01 Client Assets" CONTAINS "Client Assets".
    ref = _dbx_ref("/SAP 5174/01 Client Assets/brief.pdf")
    assert _matches_allowlist(ref, ("Client Assets",)) is True


def test_match_case_insensitive_and_suffix() -> None:
    # Folder "client assets v2" vs allowed "Client Assets" — both lowercased.
    ref = _dbx_ref("/SAP 5174/client assets v2/sub/deep.pdf")
    assert _matches_allowlist(ref, ("Client Assets",)) is True


def test_match_filename_not_folder_is_false() -> None:
    # Folder is "Internal"; the file is "Client Assets summary.pdf". The would-be
    # match is the FILENAME, which segments deliberately drop → no match.
    ref = _dbx_ref("/x/Internal/Client Assets summary.pdf")
    assert _matches_allowlist(ref, ("Client Assets",)) is False


def test_match_other_folder_is_false() -> None:
    ref = _dbx_ref("/x/Deliverables/y.pdf")
    assert _matches_allowlist(ref, ("Client Assets",)) is False


def test_match_drive_numbered_prefix() -> None:
    ref = _drive_ref(("01 Client Assets",), "brief.pdf")
    assert _matches_allowlist(ref, ("Client Assets",)) is True


def test_match_both_sources_behave_identically() -> None:
    dbx_hit = _dbx_ref("/SAP 5174/01 Client Assets/brief.pdf")
    drv_hit = _drive_ref(("01 Client Assets",), "brief.pdf")
    dbx_miss = _dbx_ref("/SAP 5174/Deliverables/y.pdf")
    drv_miss = _drive_ref(("Deliverables",), "y.pdf")
    al = ("Client Assets",)
    assert _matches_allowlist(dbx_hit, al) is _matches_allowlist(drv_hit, al) is True
    assert _matches_allowlist(dbx_miss, al) is _matches_allowlist(drv_miss, al) is False


def test_match_empty_allowlist_is_false() -> None:
    # The pure matcher with an empty allowlist matches nothing (the no-op short-
    # circuit lives in list_files, not here).
    ref = _dbx_ref("/x/Client Assets/y.pdf")
    assert _matches_allowlist(ref, ()) is False


def test_match_empty_string_allowlist_is_false() -> None:
    # CRITICAL FOOTGUN: a stored empty allowed name would make `"" in seg`
    # always True → every file matches → re-ingest the whole tree, defeating the
    # filter. The matcher's belt-and-suspenders guard skips empty/whitespace
    # allowed names, so a normal file does NOT match.
    ref = _dbx_ref("/x/Internal/y.pdf")
    assert _matches_allowlist(ref, ("",)) is False
    assert _matches_allowlist(ref, ("   ",)) is False


def test_match_padded_allowlist_still_matches() -> None:
    # A properly-stripped allowlist of "Client Assets" matches "01 Client
    # Assets"; the matcher itself doesn't strip, so the real defense is the
    # boundary strip in `_row_to_folders` (covered below). Here we confirm the
    # stripped name works.
    ref = _dbx_ref("/SAP 5174/01 Client Assets/brief.pdf")
    assert _matches_allowlist(ref, ("Client Assets",)) is True


def test_segments_dropbox_bare_filename_no_folder() -> None:
    # A bare-filename path with NO folder → no folder segments at all.
    ref = _dbx_ref("/brief.pdf", name="brief.pdf")
    assert _folder_segments(ref) == []


# ──────────────────────────────────────────────────────────────────────
#  list_files allowlist integration
# ──────────────────────────────────────────────────────────────────────


def _mixed_tree_drive() -> _FakeDriveConnector:
    """Drive root with two subfolders: one under "Client Assets", one not."""
    tree = {
        "drive-root": [
            {"id": "ca", "name": "01 Client Assets", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "del", "name": "Deliverables", "mimeType": "application/vnd.google-apps.folder"},
        ],
        "ca": [
            {"id": "d1", "name": "brief.pdf", "mimeType": "application/pdf", "size": "1", "modifiedTime": "2026"},
        ],
        "del": [
            {"id": "d2", "name": "final.pdf", "mimeType": "application/pdf", "size": "1", "modifiedTime": "2026"},
        ],
    }
    return _FakeDriveConnector(tree=tree)


def _mixed_dropbox() -> _FakeDropboxConnector:
    """Dropbox listing with one file under "Client Assets" and one not."""
    return _FakeDropboxConnector(
        entries=[
            _FakeDropboxEntry(
                "sow.pdf", "/SAP 5174/Client Assets/sow.pdf", 4096, "2026", "id:sow"
            ),
            _FakeDropboxEntry(
                "notes.docx", "/SAP 5174/Internal/notes.docx", 2048, "2026", "id:notes"
            ),
        ]
    )


def test_list_files_allowlist_keeps_only_matching() -> None:
    folders = _client_folders()
    drive = _mixed_tree_drive()
    dropbox = _mixed_dropbox()
    out, notes = list_files(
        folders,
        drive_connector=drive,
        dropbox_connector=dropbox,
        allowlist=("Client Assets",),
    )

    # 4 files total (2 drive + 2 dropbox); 2 under a "Client Assets" folder kept.
    kept_names = {f.name for f in out}
    assert kept_names == {"brief.pdf", "sow.pdf"}

    filter_notes = [n for n in notes if n["source"] == "filter"]
    assert len(filter_notes) == 1
    assert filter_notes[0]["note"] == (
        "allowlist [Client Assets] matched 2 of 4 files (2 excluded by folder filter)"
    )


def test_list_files_allowlist_zero_match() -> None:
    folders = _client_folders()
    drive = _mixed_tree_drive()
    dropbox = _mixed_dropbox()
    out, notes = list_files(
        folders,
        drive_connector=drive,
        dropbox_connector=dropbox,
        allowlist=("Nonexistent",),
    )

    assert out == []
    filter_notes = [n for n in notes if n["source"] == "filter"]
    assert len(filter_notes) == 1
    assert filter_notes[0]["note"] == (
        "allowlist [Nonexistent] matched 0 of 4 files (check folder names)"
    )


def test_list_files_empty_allowlist_is_noop() -> None:
    folders = _client_folders()
    drive = _mixed_tree_drive()
    dropbox = _mixed_dropbox()
    out, notes = list_files(
        folders,
        drive_connector=drive,
        dropbox_connector=dropbox,
        allowlist=(),
    )

    # ALL files returned, and NO filter source_note (true no-op / back-compat).
    assert {f.name for f in out} == {"brief.pdf", "final.pdf", "sow.pdf", "notes.docx"}
    assert [n for n in notes if n["source"] == "filter"] == []


# ──────────────────────────────────────────────────────────────────────
#  resolve reads asset_ingest_folders from the project row
# ──────────────────────────────────────────────────────────────────────


def _project_row(asset_ingest_folders) -> dict:
    return {
        "id": "proj-uuid",
        "company_id": "co-uuid",
        "google_drive_folder_id": "drive-1",
        "mc_dropbox_folder_id": "/p",
        "enable_google_drive": True,
        "enable_dropbox": True,
        "asset_ingest_folders": asset_ingest_folders,
        "companies": {"kind": "client"},
    }


def test_resolve_reads_asset_ingest_folders() -> None:
    client = _FakeClient([_project_row(["Client Assets"])])
    folders = resolve_project_folders_by_id(client, "proj-uuid")
    assert folders is not None
    assert folders.asset_ingest_folders == ("Client Assets",)
    # Still never SELECT *; the column is in the explicit select.
    assert "asset_ingest_folders" in client.recorder["select"]
    assert "*" not in client.recorder["select"]


def test_resolve_null_asset_ingest_folders_is_empty() -> None:
    client = _FakeClient([_project_row(None)])  # NULL in DB
    folders = resolve_project_folders_by_id(client, "proj-uuid")
    assert folders is not None
    assert folders.asset_ingest_folders == ()


def test_row_to_folders_drops_empty_allowed_name() -> None:
    # FOOTGUN DEFENSE at the service boundary: a stored ['Client Assets', '']
    # must drop the empty name → ('Client Assets',), NOT match-everything.
    folders = _row_to_folders(_project_row(["Client Assets", ""]))
    assert folders.asset_ingest_folders == ("Client Assets",)


def test_row_to_folders_strips_padded_allowed_name() -> None:
    # A padded ' Client Assets ' gets stripped at resolve time so it matches
    # folder 'Client Assets' (otherwise a surprising silent no-match).
    folders = _row_to_folders(_project_row([" Client Assets "]))
    assert folders.asset_ingest_folders == ("Client Assets",)


def test_row_to_folders_drops_whitespace_only_allowed_name() -> None:
    folders = _row_to_folders(_project_row(["   ", "Deliverables"]))
    assert folders.asset_ingest_folders == ("Deliverables",)


def test_resolve_missing_column_is_empty() -> None:
    # Forward-deploy safety: cp-engine may run BEFORE the mc-2 migration adds the
    # column, so the key is absent from the row entirely. Must not crash → ().
    row = _project_row(["x"])
    del row["asset_ingest_folders"]
    client = _FakeClient([row])
    folders = resolve_project_folders_by_id(client, "proj-uuid")
    assert folders is not None
    assert folders.asset_ingest_folders == ()


# ──────────────────────────────────────────────────────────────────────
#  ingest_project_assets threads the allowlist into list_files
# ──────────────────────────────────────────────────────────────────────


def test_ingest_passes_allowlist_to_list_files(monkeypatch) -> None:
    folders = _client_folders(asset_ingest_folders=("Client Assets",))
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda client, code: folders,
    )

    captured: dict = {}

    def _fake_list_files(
        f, drive_connector=None, dropbox_connector=None, allowlist=(), **_kw
    ):
        captured["allowlist"] = allowlist
        return [], []  # empty → short-circuits before any pipeline construction

    monkeypatch.setattr("cp_engine.asset_ingest.list_files", _fake_list_files)

    result = ingest_project_assets("acme-1", client=object())

    assert isinstance(result, IngestRunResult)
    assert captured["allowlist"] == ("Client Assets",)
