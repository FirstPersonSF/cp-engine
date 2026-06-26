"""_stamp_scope must merge file_ref.change_token into rag_assets.meta.

Task 2 of the ingest-caching arc: stamp the provider content hash
(`FileRef.change_token`) into `rag_assets.meta.change_token` so a later run
can compare it and skip unchanged files. The stamp MUST merge into existing
meta (the pipeline writes embedding/chunk keys there), never clobber it, and
must NOT write a meta key when change_token is None.
"""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.asset_ingest import FileRef, ProjectFolders, _stamp_scope


class _FakeTable:
    """Captures the SELECT (returns a configured current meta) and the UPDATE
    payload, mirroring supabase-py's chained-builder shape."""

    def __init__(self, current_meta):
        self._current_meta = current_meta
        self.update_payload = None
        self.update_filters = {}
        self.select_cols = None
        self._mode = None

    def select(self, cols):
        self.select_cols = cols
        self._mode = "select"
        return self

    def update(self, payload):
        self.update_payload = payload
        self._mode = "update"
        return self

    def eq(self, col, val):
        if self._mode == "update":
            self.update_filters[col] = val
        return self

    def execute(self):
        if self._mode == "select":
            return SimpleNamespace(data=[{"meta": self._current_meta}])
        return SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self, current_meta):
        self.tbl = _FakeTable(current_meta)

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


def _ref(change_token):
    return FileRef(
        source="dropbox",
        id="id:abc",
        name="x.pptx",
        mime_type=None,
        size=None,
        modified=None,
        path="/Remote/x.pptx",
        change_token=change_token,
    )


def test_merge_preserves_existing_meta():
    client = _FakeClient(current_meta={"chunks": 5})
    _stamp_scope(client, _folders(), "/tmp/x/x.pptx", _ref("abc"))
    assert client.tbl.update_payload["meta"] == {"chunks": 5, "change_token": "abc"}
    # source_* stamp unchanged.
    assert client.tbl.update_payload["scope"] == "project"
    assert client.tbl.update_payload["source_provider"] == "dropbox"


def test_token_none_meta_not_written():
    client = _FakeClient(current_meta={"chunks": 5})
    _stamp_scope(client, _folders(), "/tmp/x/x.pptx", _ref(None))
    assert "meta" not in client.tbl.update_payload
    # source_* fields still stamped as before.
    assert client.tbl.update_payload["scope"] == "project"
    assert client.tbl.update_payload["company_id"] == "co-1"
    assert client.tbl.update_payload["source_provider"] == "dropbox"
    assert client.tbl.update_payload["source_file_id"] == "id:abc"
    assert client.tbl.update_payload["source_path"] == "/Remote/x.pptx"


def test_no_preexisting_meta():
    client = _FakeClient(current_meta=None)
    _stamp_scope(client, _folders(), "/tmp/x/x.pptx", _ref("abc"))
    assert client.tbl.update_payload["meta"] == {"change_token": "abc"}


def test_select_meta_is_explicit_not_star():
    client = _FakeClient(current_meta={"chunks": 5})
    _stamp_scope(client, _folders(), "/tmp/x/x.pptx", _ref("abc"))
    assert client.tbl.select_cols == "meta"
