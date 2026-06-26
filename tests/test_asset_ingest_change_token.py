"""Tests for `FileRef.change_token` — the per-file CONTENT change-token captured
from the listing response at ZERO extra API cost (Drive `md5Checksum`, Dropbox
`content_hash`).

These reuse the fake-connector pattern from `test_asset_ingest_listing.py` so the
fakes match the real connector interface (`_list_files_with_pagination` for Drive,
`dbx.files_list_folder` for Dropbox).
"""

from __future__ import annotations

import re

from cp_engine.asset_ingest import _list_drive, _list_dropbox


_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _parent_id_from_query(query: str) -> str | None:
    m = re.search(r"'([^']+)' in parents", query)
    return m.group(1) if m else None


class _FakeDriveConnector:
    """Mirrors GoogleDriveConnector's listing primitive (flat single-folder)."""

    def __init__(self, files):
        self._files = files
        self.calls: list[dict] = []

    def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
        self.calls.append({"query": query, "fields": fields, "page_size": page_size})
        return self._files


class _DbxResult:
    def __init__(self, entries, has_more=False, cursor=None):
        self.entries = entries
        self.has_more = has_more
        self.cursor = cursor


class _FakeDropboxConnector:
    class _Dbx:
        def __init__(self, entries):
            self._entries = entries
            self.calls: list = []

        def files_list_folder(self, path, recursive=False):
            self.calls.append(path)
            return _DbxResult(self._entries, has_more=False)

    def __init__(self, entries):
        self.dbx = self._Dbx(entries)


class _FakeDropboxEntry:
    """FileMetadata stand-in WITH a content_hash attribute."""

    def __init__(self, name, path, size, modified, id, content_hash):
        self.name = name
        self.path_display = path
        self.size = size
        self.client_modified = modified
        self.id = id
        self.content_hash = content_hash


class _FakeDropboxEntryNoHash:
    """FileMetadata stand-in with NO content_hash (backward-compat)."""

    def __init__(self, name, path, size, modified, id):
        self.name = name
        self.path_display = path
        self.size = size
        self.client_modified = modified
        self.id = id


def test_drive_fileref_carries_md5_as_change_token() -> None:
    files = [
        {
            "id": "d1",
            "name": "deck.pptx",
            "mimeType": "application/pdf",
            "size": "1024",
            "modifiedTime": "2026-06-01T00:00:00Z",
            "md5Checksum": "abc123",
        }
    ]
    drive = _FakeDriveConnector(files=files)
    refs = _list_drive(drive, "root")
    assert refs[0].change_token == "abc123"


def test_dropbox_fileref_carries_content_hash_as_change_token() -> None:
    entries = [
        _FakeDropboxEntry(
            name="sow.pdf",
            path="/p/sow.pdf",
            size=4096,
            modified="2026-06-02",
            id="id:abc",
            content_hash="deadbeef",
        )
    ]
    dropbox = _FakeDropboxConnector(entries=entries)
    refs = _list_dropbox(dropbox, "/p")
    assert refs[0].change_token == "deadbeef"


def test_drive_fields_mask_requests_md5() -> None:
    drive = _FakeDriveConnector(files=[])
    _list_drive(drive, "root")
    assert drive.calls, "expected at least one Drive list call"
    assert "md5Checksum" in drive.calls[0]["fields"]


def test_change_token_none_when_provider_omits_hash() -> None:
    # Backward-compat: a listing WITHOUT the hash → change_token is None.
    drive_files = [
        {
            "id": "d2",
            "name": "no-hash.pdf",
            "mimeType": "application/pdf",
            "size": "1",
            "modifiedTime": "2026-06-01T00:00:00Z",
        }
    ]
    drive = _FakeDriveConnector(files=drive_files)
    drive_refs = _list_drive(drive, "root")
    assert drive_refs[0].change_token is None

    dbx_entries = [_FakeDropboxEntryNoHash("a.pdf", "/p/a.pdf", 1, "2026", "id:1")]
    dropbox = _FakeDropboxConnector(entries=dbx_entries)
    dbx_refs = _list_dropbox(dropbox, "/p")
    assert dbx_refs[0].change_token is None
