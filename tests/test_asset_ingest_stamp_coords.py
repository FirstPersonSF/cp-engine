"""_stamp_scope must also persist re-fetch coords from the FileRef."""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.asset_ingest import FileRef, ProjectFolders, _stamp_scope


class _FakeTable:
    def __init__(self):
        self.payload = None
        self.filters = {}

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self):
        self.tbl = _FakeTable()

    def table(self, name):
        return self.tbl


def _folders():
    return ProjectFolders(
        project_id="proj-1",
        company_id="co-1",
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=None,
        enable_google_drive=False,
        enable_dropbox=True,
    )


def _dropbox_ref():
    return FileRef(
        source="dropbox",
        id="id:abc",
        name="x.pptx",
        mime_type=None,
        size=None,
        modified=None,
        path="/Remote/x.pptx",
    )


def test_stamp_persists_source_coords():
    client = _FakeClient()
    _stamp_scope(client, _folders(), "/tmp/dropbox-id_x/x.pptx", _dropbox_ref())
    assert client.tbl.payload["scope"] == "project"
    assert client.tbl.payload["company_id"] == _folders().company_id
    assert client.tbl.payload["source_provider"] == "dropbox"
    assert client.tbl.payload["source_file_id"] == "id:abc"
    assert client.tbl.payload["source_path"] == "/Remote/x.pptx"
    assert client.tbl.filters["file_path"] == "/tmp/dropbox-id_x/x.pptx"
    assert client.tbl.filters["project_id"] == "proj-1"
    assert client.tbl.filters["status"] == "active"


def test_stamp_drive_ref_path_is_none():
    # A Drive FileRef has path=None → source_path stamped as None.
    client = _FakeClient()
    ref = FileRef(
        source="drive",
        id="DRIVEID",
        name="x",
        mime_type=None,
        size=None,
        modified=None,
        path=None,
    )
    _stamp_scope(client, _folders(), "/tmp/drive-id_X/x", ref)
    assert client.tbl.payload["source_provider"] == "drive"
    assert client.tbl.payload["source_file_id"] == "DRIVEID"
    assert client.tbl.payload["source_path"] is None
