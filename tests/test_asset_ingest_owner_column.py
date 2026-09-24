"""Task 8 Part B — the owner column on the rag_assets row.

`_stamp_scope` filters on `project_id` — the one owner column since mc-2
mig 192 / #301 — for a client job and an internal workstream alike.
"""

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


def _engagement_folders():
    return ProjectFolders(
        project_id="proj-1",
        company_id="co-1",
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=None,
        enable_google_drive=False,
        enable_dropbox=True,
    )


def _internal_folders():
    return ProjectFolders(
        project_id="init-1",
        company_id="co-1",
        company_kind="self-fpsf",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=None,
        enable_google_drive=False,
        enable_dropbox=True,
        has_agreement=False,
    )


def _ref():
    return FileRef(
        source="dropbox",
        id="id:abc",
        name="x.pptx",
        mime_type=None,
        size=None,
        modified=None,
        path="/Remote/x.pptx",
    )


def test_stamp_writes_project_id_for_internal_workstream():
    client = _FakeClient()
    _stamp_scope(client, _internal_folders(), "/tmp/x.pptx", _ref())
    assert client.tbl.filters.get("project_id") == "init-1"
    assert "initiative_id" not in client.tbl.filters


def test_stamp_writes_project_id_for_engagement():
    client = _FakeClient()
    _stamp_scope(client, _engagement_folders(), "/tmp/x.pptx", _ref())
    assert client.tbl.filters.get("project_id") == "proj-1"
    assert "initiative_id" not in client.tbl.filters
