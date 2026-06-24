"""Task 8 Part B — correct owner column on the rag_assets row.

`_stamp_scope` writes the single owner column matching the item's kind:
`initiative_id` for an initiative, `project_id` for an engagement — satisfying
the `num_nonnulls(project_id, initiative_id) = 1` CHECK (migration 081).
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


def _initiative_folders():
    return ProjectFolders(
        project_id="init-1",
        company_id="co-1",
        company_kind="self-fpsf",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=None,
        enable_google_drive=False,
        enable_dropbox=True,
        is_initiative=True,
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


def test_stamp_writes_initiative_id_for_initiative():
    client = _FakeClient()
    _stamp_scope(client, _initiative_folders(), "/tmp/x.pptx", _ref())
    # Owner column: filter on initiative_id, NEVER project_id (CHECK: one owner).
    assert client.tbl.filters.get("initiative_id") == "init-1"
    assert "project_id" not in client.tbl.filters
    assert "project_id" not in client.tbl.payload


def test_stamp_writes_project_id_for_engagement():
    client = _FakeClient()
    _stamp_scope(client, _engagement_folders(), "/tmp/x.pptx", _ref())
    assert client.tbl.filters.get("project_id") == "proj-1"
    assert "initiative_id" not in client.tbl.filters
    assert "initiative_id" not in client.tbl.payload
