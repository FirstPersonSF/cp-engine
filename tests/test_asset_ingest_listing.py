"""Tests for `cp_engine.asset_ingest` resolve + list logic (Task C3).

These don't hit Supabase, Drive, or Dropbox. They use a small fake Supabase
client (mirroring the `.table().select().eq().execute()` PostgREST chain) and
fake connectors so the pure resolve/list behavior can be exercised in isolation.

Scope: resolve_project_folders + list_files only. Download is a later task.
"""

from __future__ import annotations

import pytest

from cp_engine.asset_ingest import (
    FileRef,
    ProjectFolders,
    list_files,
    resolve_project_folders,
)


# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeExecute:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Records the select() column string and the eq() filter; returns canned data."""

    def __init__(self, data, recorder):
        self._data = data
        self._recorder = recorder

    def select(self, columns):
        self._recorder["select"] = columns
        return self

    def eq(self, col, val):
        self._recorder["eq"] = (col, val)
        return self

    def execute(self):
        return _FakeExecute(self._data)


class _FakeClient:
    def __init__(self, data):
        self._data = data
        self.recorder: dict = {}

    def table(self, name):
        self.recorder["table"] = name
        return _FakeQuery(self._data, self.recorder)


class _FakeDriveConnector:
    """Mirrors GoogleDriveConnector's listing primitive."""

    def __init__(self, files):
        self._files = files
        self.calls: list[dict] = []

    def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
        self.calls.append({"query": query, "fields": fields, "page_size": page_size})
        return self._files


class _FakeDropboxConnector:
    """Mirrors the low-level dbx.files_list_folder primitive used by list_files."""

    class _Dbx:
        def __init__(self, entries):
            self._entries = entries
            self.calls: list = []

        def files_list_folder(self, path):
            self.calls.append(path)

            class _Result:
                pass

            r = _Result()
            r.entries = self._entries
            r.has_more = False
            return r

    def __init__(self, entries):
        self.dbx = self._Dbx(entries)


class _FakeDropboxEntry:
    """Minimal stand-in for dropbox.files.FileMetadata."""

    def __init__(self, name, path, size, modified, id):
        self.name = name
        self.path_display = path
        self.size = size
        self.client_modified = modified
        self.id = id


# ──────────────────────────────────────────────────────────────────────
#  resolve_project_folders
# ──────────────────────────────────────────────────────────────────────


def test_resolve_parses_number_and_returns_folders() -> None:
    client = _FakeClient(
        [
            {
                "id": "proj-uuid",
                "company_id": "co-uuid",
                "google_drive_folder_id": "drive-123",
                "mc_dropbox_folder_id": "/Clients/IBX/5153",
                "enable_google_drive": True,
                "enable_dropbox": True,
                "companies": {"kind": "client"},
            }
        ]
    )
    folders = resolve_project_folders(client, "ibx-5153")
    assert folders is not None
    assert folders.project_id == "proj-uuid"
    assert folders.company_id == "co-uuid"
    assert folders.company_kind == "client"
    assert folders.google_drive_folder_id == "drive-123"
    assert folders.mc_dropbox_folder_id == "/Clients/IBX/5153"
    assert folders.enable_google_drive is True
    assert folders.enable_dropbox is True
    # Resolved by numeric part of the code, never SELECT *.
    assert client.recorder["eq"] == ("number", 5153)
    assert "*" not in client.recorder["select"]


def test_resolve_returns_none_for_unknown_project() -> None:
    client = _FakeClient([])
    assert resolve_project_folders(client, "ibx-9999") is None


def test_resolve_returns_none_for_codeless_input() -> None:
    client = _FakeClient([{"id": "x"}])
    assert resolve_project_folders(client, "no-number-here") is None
    assert resolve_project_folders(client, "") is None


def test_resolve_handles_companies_as_list() -> None:
    # PostgREST sometimes returns a to-one embed as a single-element LIST
    # instead of a dict. resolve must not crash and must read kind from it.
    client = _FakeClient(
        [
            {
                "id": "p",
                "company_id": "c",
                "google_drive_folder_id": "drive-1",
                "mc_dropbox_folder_id": "/p",
                "enable_google_drive": True,
                "enable_dropbox": True,
                "companies": [{"kind": "client"}],  # list, not dict
            }
        ]
    )
    folders = resolve_project_folders(client, "ibx-5153")
    assert folders is not None
    assert folders.company_kind == "client"


def test_resolve_handles_companies_as_empty_list() -> None:
    # Empty embed list must not IndexError and yields empty company_kind.
    client = _FakeClient(
        [
            {
                "id": "p",
                "company_id": "c",
                "google_drive_folder_id": None,
                "mc_dropbox_folder_id": None,
                "enable_google_drive": False,
                "enable_dropbox": False,
                "companies": [],  # empty list
            }
        ]
    )
    folders = resolve_project_folders(client, "ibx-5153")
    assert folders is not None
    assert folders.company_kind == ""


def test_resolve_coerces_enable_flags_to_bool() -> None:
    client = _FakeClient(
        [
            {
                "id": "p",
                "company_id": "c",
                "google_drive_folder_id": None,
                "mc_dropbox_folder_id": None,
                "enable_google_drive": None,  # null in DB → False
                "enable_dropbox": 1,  # truthy int → True
                "companies": {"kind": "client"},
            }
        ]
    )
    folders = resolve_project_folders(client, "ibx-5153")
    assert folders is not None
    assert folders.enable_google_drive is False
    assert folders.enable_dropbox is True


# ──────────────────────────────────────────────────────────────────────
#  list_files
# ──────────────────────────────────────────────────────────────────────


def _client_folders(**overrides) -> ProjectFolders:
    base = dict(
        project_id="p",
        company_id="c",
        company_kind="client",
        google_drive_folder_id="drive-123",
        mc_dropbox_folder_id="/Clients/IBX/5153",
        enable_google_drive=True,
        enable_dropbox=True,
    )
    base.update(overrides)
    return ProjectFolders(**base)


def test_list_skips_non_client_company(capsys: pytest.CaptureFixture[str]) -> None:
    folders = _client_folders(company_kind="self-fpsf")
    drive = _FakeDriveConnector(files=[{"id": "x"}])
    dropbox = _FakeDropboxConnector(entries=[])
    out = list_files(folders, drive_connector=drive, dropbox_connector=dropbox)
    assert out == []
    # Connectors must never be touched for non-client kinds.
    assert drive.calls == []
    assert dropbox.dbx.calls == []
    assert "self-fpsf" in capsys.readouterr().err


def test_list_drive_and_dropbox_merged() -> None:
    folders = _client_folders()
    drive = _FakeDriveConnector(
        files=[
            {
                "id": "d1",
                "name": "deck.pptx",
                "mimeType": "application/vnd.ms-powerpoint",
                "size": "2048",
                "modifiedTime": "2026-06-01T00:00:00Z",
            }
        ]
    )
    dropbox = _FakeDropboxConnector(
        entries=[
            _FakeDropboxEntry(
                name="sow.pdf",
                path="/Clients/IBX/5153/sow.pdf",
                size=4096,
                modified="2026-06-02",
                id="id:abc",
            )
        ]
    )
    out = list_files(folders, drive_connector=drive, dropbox_connector=dropbox)
    by_source = {f.source for f in out}
    assert by_source == {"drive", "dropbox"}

    drive_ref = next(f for f in out if f.source == "drive")
    assert drive_ref.id == "d1"
    assert drive_ref.name == "deck.pptx"
    assert drive_ref.mime_type == "application/vnd.ms-powerpoint"
    assert drive_ref.size == 2048  # coerced from str
    assert drive_ref.modified == "2026-06-01T00:00:00Z"
    assert drive_ref.path is None

    dbx_ref = next(f for f in out if f.source == "dropbox")
    assert dbx_ref.id == "id:abc"
    assert dbx_ref.name == "sow.pdf"
    assert dbx_ref.path == "/Clients/IBX/5153/sow.pdf"
    assert dbx_ref.size == 4096

    # Drive query targeted the folder id with the trashed filter.
    assert "'drive-123' in parents" in drive.calls[0]["query"]
    assert "trashed=false" in drive.calls[0]["query"]
    # Dropbox listed the stored folder path/id.
    assert dropbox.dbx.calls == ["/Clients/IBX/5153"]


def test_list_skips_drive_when_no_folder_id(capsys: pytest.CaptureFixture[str]) -> None:
    folders = _client_folders(google_drive_folder_id=None)
    drive = _FakeDriveConnector(files=[{"id": "x"}])
    dropbox = _FakeDropboxConnector(
        entries=[
            _FakeDropboxEntry("a", "/p/a", 1, "2026", "id:1"),
        ]
    )
    out = list_files(folders, drive_connector=drive, dropbox_connector=dropbox)
    # Drive skipped (no error), dropbox still listed.
    assert [f.source for f in out] == ["dropbox"]
    assert drive.calls == []
    assert "skipping" in capsys.readouterr().err.lower()


def test_list_respects_enable_flags() -> None:
    folders = _client_folders(enable_google_drive=False)
    drive = _FakeDriveConnector(files=[{"id": "x", "name": "n"}])
    dropbox = _FakeDropboxConnector(
        entries=[_FakeDropboxEntry("a", "/p/a", 1, "2026", "id:1")]
    )
    out = list_files(folders, drive_connector=drive, dropbox_connector=dropbox)
    # Drive connector must not be called when the flag is off, even with a folder id.
    assert drive.calls == []
    assert [f.source for f in out] == ["dropbox"]


def test_list_skips_dropbox_when_disabled() -> None:
    folders = _client_folders(enable_dropbox=False)
    drive = _FakeDriveConnector(files=[{"id": "d", "name": "n"}])
    dropbox = _FakeDropboxConnector(entries=[_FakeDropboxEntry("a", "/p/a", 1, "2026", "i")])
    out = list_files(folders, drive_connector=drive, dropbox_connector=dropbox)
    assert dropbox.dbx.calls == []
    assert [f.source for f in out] == ["drive"]
