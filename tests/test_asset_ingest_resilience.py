"""Tests for `list_files` per-source resilience (asset-ingest-button Task 1).

Asset ingest walks Drive THEN Dropbox. A dead source (e.g. Drive auth failure
with no service-account creds) must NOT abort the whole run when the docs the
user wants live in the OTHER source. `list_files` now returns
`(refs, source_notes)` and isolates each source in its own try/except: on a
per-source exception it records a note and continues to the next source.
"""

from __future__ import annotations

from cp_engine.asset_ingest import ProjectFolders, list_files

# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _ExplodingDriveConnector:
    """Any attribute access raises — stands in for a connector whose use blows
    up (e.g. a GoogleDriveConnector that failed to authenticate)."""

    def __getattr__(self, name):
        raise RuntimeError(f"drive auth failed (attr '{name}')")


class _DbxResult:
    def __init__(self, entries, has_more=False, cursor=None):
        self.entries = entries
        self.has_more = has_more
        self.cursor = cursor


class _FakeDropboxEntry:
    """Minimal stand-in for dropbox.files.FileMetadata (has .size)."""

    def __init__(self, name, path, size, modified, id):
        self.name = name
        self.path_display = path
        self.size = size
        self.client_modified = modified
        self.id = id


class _FakeDropboxConnector:
    """Mirrors the low-level dbx.files_list_folder primitive used by list_files."""

    class _Dbx:
        def __init__(self, entries):
            self._entries = entries
            self.calls: list = []

        def files_list_folder(self, path, recursive=False):
            self.calls.append(path)
            return _DbxResult(self._entries or [], has_more=False)

    def __init__(self, entries=None):
        self.dbx = self._Dbx(entries)


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


# ──────────────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────────────


def test_drive_failure_does_not_block_dropbox() -> None:
    """A Drive failure records a note and lets Dropbox still list its file."""
    folders = _client_folders()
    drive = _ExplodingDriveConnector()
    dropbox = _FakeDropboxConnector(
        entries=[
            _FakeDropboxEntry("sow.pdf", "/Clients/IBX/5153/sow.pdf", 4096, "2026", "id:abc")
        ]
    )

    refs, source_notes = list_files(
        folders, drive_connector=drive, dropbox_connector=dropbox
    )

    # The Dropbox file came through despite Drive blowing up.
    assert [r.source for r in refs] == ["dropbox"]
    assert refs[0].name == "sow.pdf"

    # Exactly one note, for the drive source — no dropbox note.
    assert len(source_notes) == 1
    assert source_notes[0]["source"] == "drive"
    # The note now leads with the exception TYPE so operators can tell an
    # expected creds failure (RuntimeError/HttpError) from a programming bug.
    assert source_notes[0]["note"].startswith("RuntimeError: ")
    assert all(n["source"] != "dropbox" for n in source_notes)


def test_both_sources_succeed_no_notes() -> None:
    """Both sources list cleanly → no notes, refs from both."""
    dropbox = _FakeDropboxConnector(
        entries=[
            _FakeDropboxEntry("a.pdf", "/p/a.pdf", 1, "2026", "id:1"),
        ]
    )

    # Tree-driven fake drive that succeeds, with drive enabled.
    folders = _client_folders()

    class _OkDrive:
        def __init__(self):
            self.calls: list = []

        def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
            self.calls.append(query)
            return [
                {
                    "id": "d1",
                    "name": "deck.pptx",
                    "mimeType": "application/vnd.ms-powerpoint",
                    "size": "2048",
                    "modifiedTime": "2026-06-01T00:00:00Z",
                }
            ]

    refs, source_notes = list_files(
        folders, drive_connector=_OkDrive(), dropbox_connector=dropbox
    )

    assert source_notes == []
    assert {r.source for r in refs} == {"drive", "dropbox"}


def test_source_enabled_with_no_folder_id_records_note() -> None:
    """Drive enabled but no folder id → a source_note, no crash, dropbox listed."""
    folders = _client_folders(google_drive_folder_id=None)
    dropbox = _FakeDropboxConnector(
        entries=[_FakeDropboxEntry("a.pdf", "/p/a.pdf", 1, "2026", "id:1")]
    )

    refs, source_notes = list_files(
        folders, drive_connector=None, dropbox_connector=dropbox
    )

    assert [r.source for r in refs] == ["dropbox"]
    assert len(source_notes) == 1
    assert source_notes[0]["source"] == "drive"
