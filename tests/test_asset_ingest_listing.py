"""Tests for `cp_engine.asset_ingest` resolve + list logic (Task C3).

These don't hit Supabase, Drive, or Dropbox. They use a small fake Supabase
client (mirroring the `.table().select().eq().execute()` PostgREST chain) and
fake connectors so the pure resolve/list behavior can be exercised in isolation.

Scope: resolve_project_folders + list_files only. Download is a later task.
"""

from __future__ import annotations

import re

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


_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_file(id, name, mime="application/pdf", size="1024", modified="2026-06-01T00:00:00Z"):
    """A non-folder Drive child dict (mirrors the API's files() shape)."""
    return {"id": id, "name": name, "mimeType": mime, "size": size, "modifiedTime": modified}


def _drive_folder(id, name):
    """A folder Drive child dict — recursion descends into these."""
    return {"id": id, "name": name, "mimeType": _DRIVE_FOLDER_MIME}


def _parent_id_from_query(query: str) -> str | None:
    """Pull the `'<id>' in parents` folder id out of a Drive query string."""
    m = re.search(r"'([^']+)' in parents", query)
    return m.group(1) if m else None


class _FakeDriveConnector:
    """Mirrors GoogleDriveConnector's listing primitive.

    Two construction modes:
      - `files=[...]`: a flat single-folder listing (the original behavior). The
        same list is returned for any query — fine for the original tests whose
        root folder has files and no subfolders.
      - `tree={folder_id: [child, ...]}`: a folder TREE keyed by folder id. The
        connector dispatches on the `'<id>' in parents` clause in the query so
        recursion into a subfolder returns that subfolder's children.
    """

    def __init__(self, files=None, tree=None):
        self._files = files
        self._tree = tree
        self.calls: list[dict] = []

    def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
        self.calls.append({"query": query, "fields": fields, "page_size": page_size})
        if self._tree is not None:
            parent = _parent_id_from_query(query)
            return list(self._tree.get(parent, []))
        return self._files


class _InfiniteDriveConnector:
    """Every folder contains exactly one fresh subfolder — infinite nesting.

    Used to prove the depth cap halts the tree-walk (no hang / no stack blow-up).
    """

    def __init__(self):
        self.calls: list[dict] = []

    def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
        self.calls.append({"query": query, "fields": fields, "page_size": page_size})
        parent = _parent_id_from_query(query)
        # One file (so we can see depth-of-descent) + one deeper subfolder.
        return [
            _drive_file(f"file-at-{parent}", f"f-{parent}.pdf"),
            _drive_folder(f"{parent}-child", f"sub-{parent}"),
        ]


class _CycleDriveConnector:
    """Folder A → B → A: a cycle. Proves the visited-set stops infinite looping."""

    def __init__(self):
        self.calls: list[dict] = []
        self._graph = {
            "A": [_drive_file("fa", "a.pdf"), _drive_folder("B", "folder-B")],
            "B": [_drive_file("fb", "b.pdf"), _drive_folder("A", "folder-A")],
        }

    def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
        self.calls.append({"query": query, "fields": fields, "page_size": page_size})
        parent = _parent_id_from_query(query)
        return list(self._graph.get(parent, []))


class _DbxResult:
    """A files_list_folder / _continue result page."""

    def __init__(self, entries, has_more=False, cursor=None):
        self.entries = entries
        self.has_more = has_more
        self.cursor = cursor


class _FakeDropboxConnector:
    """Mirrors the low-level dbx.files_list_folder primitive used by list_files.

    Single-page mode: pass `entries`. Records the `recursive` kwarg so tests can
    assert recursive=True is passed. Multi-page mode: pass `pages` (a list of
    entry-lists); the connector drains them via has_more + files_list_folder_continue.
    """

    class _Dbx:
        def __init__(self, entries=None, pages=None):
            self._entries = entries
            self._pages = list(pages) if pages is not None else None
            self.calls: list = []
            self.continue_calls: list = []
            self.recursive_flags: list = []
            self._page_idx = 0

        def files_list_folder(self, path, recursive=False):
            self.calls.append(path)
            self.recursive_flags.append(recursive)
            if self._pages is not None:
                self._page_idx = 0
                entries = self._pages[0]
                has_more = len(self._pages) > 1
                return _DbxResult(entries, has_more=has_more, cursor="cursor-0")
            return _DbxResult(self._entries or [], has_more=False)

        def files_list_folder_continue(self, cursor):
            self.continue_calls.append(cursor)
            self._page_idx += 1
            entries = self._pages[self._page_idx]
            has_more = self._page_idx < len(self._pages) - 1
            return _DbxResult(entries, has_more=has_more, cursor=f"cursor-{self._page_idx}")

    def __init__(self, entries=None, pages=None):
        self.dbx = self._Dbx(entries=entries, pages=pages)


class _FakeDropboxEntry:
    """Minimal stand-in for dropbox.files.FileMetadata (has .size)."""

    def __init__(self, name, path, size, modified, id):
        self.name = name
        self.path_display = path
        self.size = size
        self.client_modified = modified
        self.id = id


class _FakeDropboxFolderEntry:
    """Minimal stand-in for dropbox.files.FolderMetadata (NO .size — skipped)."""

    def __init__(self, name, path, id):
        self.name = name
        self.path_display = path
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


# ──────────────────────────────────────────────────────────────────────
#  Recursive subfolder listing — Drive tree-walk
# ──────────────────────────────────────────────────────────────────────


def test_list_drive_recurses_into_subfolders() -> None:
    """Root has ZERO files and two subfolders; the files live one level down.

    This mirrors real client folders (Drive nests under 'Define and Approach' /
    'Client Share' / 'Management' with nothing at the top). A non-recursive
    listing would return nothing — recursion must descend and find the files.
    """
    folders = _client_folders(enable_dropbox=False, google_drive_folder_id="root")
    tree = {
        "root": [_drive_folder("sub-a", "Define and Approach"),
                 _drive_folder("sub-b", "Management")],
        "sub-a": [_drive_file("fa1", "narrative.docx"), _drive_file("fa2", "approach.pdf")],
        "sub-b": [_drive_file("fb1", "sow.pdf")],
    }
    drive = _FakeDriveConnector(tree=tree)
    out = list_files(folders, drive_connector=drive, dropbox_connector=None)

    names = {f.name for f in out}
    assert names == {"narrative.docx", "approach.pdf", "sow.pdf"}
    assert all(f.source == "drive" for f in out)
    # Each nested file keeps its own id (download is by id).
    assert {f.id for f in out} == {"fa1", "fa2", "fb1"}
    # It actually queried the two subfolders' ids (proof it recursed).
    queried_parents = {_parent_id_from_query(c["query"]) for c in drive.calls}
    assert {"root", "sub-a", "sub-b"} <= queried_parents


def test_list_drive_recursion_depth_cap(capsys: pytest.CaptureFixture[str]) -> None:
    """Infinitely-nested tree must stop at the depth cap, not hang/recurse forever."""
    folders = _client_folders(enable_dropbox=False, google_drive_folder_id="root")
    drive = _InfiniteDriveConnector()
    out = list_files(folders, drive_connector=drive, dropbox_connector=None)

    # It terminated (no hang) and collected the files down to the cap.
    assert len(out) >= 1
    assert all(f.source == "drive" for f in out)
    # The number of list calls is bounded (cap + root), not unbounded.
    assert len(drive.calls) <= 12  # max_depth=10 → root + 10 descents, with slack
    # The cap was surfaced — NOT a silent truncation.
    err = capsys.readouterr().err.lower()
    assert "depth" in err or "cap" in err


def test_list_drive_cycle_guard() -> None:
    """Folder A→B→A is a cycle; the visited-set must stop the infinite loop."""
    folders = _client_folders(enable_dropbox=False, google_drive_folder_id="A")
    drive = _CycleDriveConnector()
    out = list_files(folders, drive_connector=drive, dropbox_connector=None)

    names = {f.name for f in out}
    assert names == {"a.pdf", "b.pdf"}
    # A and B were each visited exactly once (no re-descent into the cycle).
    queried = [_parent_id_from_query(c["query"]) for c in drive.calls]
    assert sorted(queried) == ["A", "B"]


# ──────────────────────────────────────────────────────────────────────
#  Recursive subfolder listing — Dropbox recursive=True
# ──────────────────────────────────────────────────────────────────────


def test_list_dropbox_recursive_flag() -> None:
    """recursive=True is passed; FolderMetadata entries are skipped, files kept."""
    folders = _client_folders(enable_google_drive=False)
    entries = [
        _FakeDropboxFolderEntry("01 Project Notes", "/p/01 Project Notes", "id:f1"),
        _FakeDropboxEntry("brief.pdf", "/p/01 Project Notes/brief.pdf", 10, "2026", "id:a"),
        _FakeDropboxFolderEntry("02 Working Files", "/p/02 Working Files", "id:f2"),
        _FakeDropboxEntry(
            "deep.docx", "/p/02 Working Files/sub/deep.docx", 20, "2026", "id:b"
        ),
    ]
    dropbox = _FakeDropboxConnector(entries=entries)
    out = list_files(folders, drive_connector=None, dropbox_connector=dropbox)

    # recursive=True was passed to the SDK.
    assert dropbox.dbx.recursive_flags == [True]
    # Only the FileMetadata entries became FileRefs (folders skipped).
    assert {f.name for f in out} == {"brief.pdf", "deep.docx"}
    # path_display (full nested path) is carried for download-by-path.
    paths = {f.path for f in out}
    assert "/p/01 Project Notes/brief.pdf" in paths
    assert "/p/02 Working Files/sub/deep.docx" in paths
    assert all(f.source == "dropbox" for f in out)


def test_list_dropbox_paginates_has_more() -> None:
    """has_more drains via files_list_folder_continue; both pages' files included."""
    folders = _client_folders(enable_google_drive=False)
    page1 = [_FakeDropboxEntry("p1.pdf", "/p/a/p1.pdf", 1, "2026", "id:1")]
    page2 = [_FakeDropboxEntry("p2.pdf", "/p/b/p2.pdf", 2, "2026", "id:2")]
    dropbox = _FakeDropboxConnector(pages=[page1, page2])
    out = list_files(folders, drive_connector=None, dropbox_connector=dropbox)

    assert {f.name for f in out} == {"p1.pdf", "p2.pdf"}
    # The cursor was drained exactly once (one continue call for the 2nd page).
    assert len(dropbox.dbx.continue_calls) == 1
