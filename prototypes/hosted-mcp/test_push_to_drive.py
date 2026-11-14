"""`push_to_drive`: cp files a document in a project's Google Drive folder.

WHY. Half of the file map is on Google Drive (invoices, SOWs, the wrap
report) and cp could only read it. On 2026-11-13 the project folders moved
into a Shared Drive, where the service account can hold files. Drew: "Let's
build the verb to write to drive".

WHAT THESE PIN.
1. A Drive kind is filed in that kind's folder, found by walking the
   project's Drive folder by exact name. Nothing is guessed.
2. No content returns an upload link for the same destination.
3. A name already in the folder is refused with its link; `overwrite`
   replaces that file and keeps its id; two files sharing the name are
   refused even with `overwrite`.
4. A missing folder is refused with the missing segment and nothing is
   created. A Dropbox kind, an unknown kind and a missing kind are refused
   before a project, a folder or a credential is read.
5. A non-team caller is refused first.

Drive and MC-2 are fakes; the file map and the folder rules are the real
ones. Nothing here reaches a live store.

    python -m pytest prototypes/hosted-mcp/test_push_to_drive.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

DRIVE_ROOT = "drv-root"
FOLDER_MIME = "application/vnd.google-apps.folder"
WRAP = "Management/04 Wrap"


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    mp.delenv("TENANT_REPO", raising=False)
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def pt(server):
    return server.ported_tools


class _Audit(list):
    def __call__(self, client, tool, args, rows):
        self.append((tool, dict(args), rows))


def _gfolder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME}


class _Reader:
    """The read connector: the one listing primitive the folder walk uses."""

    def __init__(self, tree):
        self.tree = tree

    def _list_files_with_pagination(self, query, fields, page_size=100, **kw):
        assert kw.get("supportsAllDrives") and kw.get("includeItemsFromAllDrives")
        return [i for i in self.tree.get(query.split("'")[1], [])
                if i.get("mimeType") == FOLDER_MIME]


class _Writer:
    """Stands in for `_DriveWriter`: records every write it is asked for."""

    account = "svc@example.invalid"

    def __init__(self, files):
        self.files = files  # {folder id: [file dicts]}
        self.writes: list[tuple] = []

    def find(self, folder_id, name):
        return [f for f in self.files.get(folder_id, []) if f["name"] == name]

    def drive_upload(self, folder_id, name, data, mime):
        self.writes.append(("upload", folder_id, name, data, mime))
        return {"id": "new1", "name": name, "webViewLink": "https://drive.invalid/new1"}

    def drive_replace(self, file_id, data, mime):
        self.writes.append(("replace", file_id, data, mime))
        return {"id": file_id, "webViewLink": f"https://drive.invalid/{file_id}"}

    def drive_upload_link(self, folder_id, name, mime, replace_id=None):
        self.writes.append(("link", folder_id, name, mime, replace_id))
        return "https://upload.invalid/session"


def _folders(pid, **kw):
    from cp_engine.asset_ingest import ProjectFolders

    base = dict(project_id=pid, company_id="co1", company_kind="client",
                google_drive_folder_id=DRIVE_ROOT, mc_dropbox_folder_id="/x",
                enable_google_drive=True, enable_dropbox=True, has_agreement=True)
    base.update(kw)
    return ProjectFolders(**base)


@pytest.fixture
def env(server, pt, monkeypatch):
    client = SimpleNamespace(name="caller-client")
    audit = _Audit()
    state = SimpleNamespace(
        row={}, audit=audit, writers_built=0,
        reader=_Reader({
            DRIVE_ROOT: [_gfolder("mgmt", "Management")],
            "mgmt": [_gfolder("wrap", "04 Wrap"), _gfolder("fin", "02 Contracts and Financials")],
            "fin": [_gfolder("inv", "05 Invoices PDFs")],
        }),
        writer=_Writer({"wrap": [{"id": "old1", "name": "wrap-v01.docx",
                                  "webViewLink": "https://drive.invalid/old1",
                                  "modifiedTime": "2026-11-13T00:00:00Z"}]}),
    )
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "job")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "not-a-real-file")
    monkeypatch.setattr("cp_engine.asset_ingest.resolve_project_folders_by_id",
                        lambda c, pid: _folders(pid, **state.row))
    monkeypatch.setattr(pt, "_new_drive_connector", lambda: state.reader)

    def _writer():
        state.writers_built += 1
        return state.writer

    monkeypatch.setattr(pt, "_new_drive_writer", _writer)
    return state


def _b64(data=b"report"):
    return base64.b64encode(data).decode()


def test_a_drive_kind_is_uploaded_to_that_kinds_folder(pt, env):
    out = pt.push_to_drive("slt-5196", "wrap-v02.docx", "wrap report", _b64(b"abc"))
    assert "error" not in out, out
    assert env.writer.writes == [("upload", "wrap", "wrap-v02.docx", b"abc",
                                  "application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document")]
    assert out["kind"] == "wrap" and out["store"] == "drive"
    assert out["drive_path"] == f"{WRAP}/wrap-v02.docx"
    assert out["folder_id"] == "wrap"
    assert out["folder_url"] == "https://drive.google.com/drive/folders/wrap"
    assert out["id"] == "new1" and out["url"] == "https://drive.invalid/new1"
    assert out["size"] == 3 and out["overwrote"] is False and out["ingested"] is False
    assert env.audit[-1][0] == "push_to_drive" and env.audit[-1][2] == 1
    assert env.audit[-1][1]["file_id"] == "new1"


def test_no_content_returns_an_upload_link_and_writes_nothing(pt, env):
    out = pt.push_to_drive("slt-5196", "INV_4415.pdf", "invoices")
    assert out["upload_url"] == "https://upload.invalid/session"
    assert "curl -X PUT" in out["how"] and out["overwrite"] is False
    assert env.writer.writes == [("link", "inv", "INV_4415.pdf", "application/pdf", None)]
    assert out["ingested"] is False


def test_an_existing_name_is_refused_with_its_link(pt, env):
    out = pt.push_to_drive("slt-5196", "wrap-v01.docx", "wrap", _b64())
    assert "Nothing was uploaded" in out["error"] and "overwrite=True" in out["error"]
    assert out["existing"]["id"] == "old1"
    assert out["existing"]["url"] == "https://drive.invalid/old1"
    assert env.writer.writes == []


def test_overwrite_replaces_the_existing_file_and_keeps_its_id(pt, env):
    out = pt.push_to_drive("slt-5196", "wrap-v01.docx", "wrap", _b64(b"new"), overwrite=True)
    assert [w[:2] for w in env.writer.writes] == [("replace", "old1")]
    assert out["id"] == "old1" and out["overwrote"] is True


def test_overwrite_with_no_content_links_to_the_existing_file(pt, env):
    out = pt.push_to_drive("slt-5196", "wrap-v01.docx", "wrap", overwrite=True)
    assert env.writer.writes[0][0] == "link" and env.writer.writes[0][-1] == "old1"
    assert out["overwrite"] is True


def test_two_files_sharing_the_name_are_refused_even_with_overwrite(pt, env):
    env.writer.files["wrap"].append({"id": "old2", "name": "wrap-v01.docx"})
    out = pt.push_to_drive("slt-5196", "wrap-v01.docx", "wrap", _b64(), overwrite=True)
    assert "2 files named" in out["error"]
    assert [e["id"] for e in out["existing"]] == ["old1", "old2"]
    assert env.writer.writes == []


def test_a_missing_folder_is_refused_and_nothing_is_created(pt, env):
    out = pt.push_to_drive("slt-5196", "sow.pdf", "sows", _b64())
    assert "Nothing was uploaded" in out["error"]
    assert out["missing_segment"] == "03 SOWs"
    assert out["found_path"] == "Management/02 Contracts and Financials"
    assert out["folders_there"] == ["05 Invoices PDFs"]
    assert env.writers_built == 0 and env.writer.writes == []


@pytest.mark.parametrize("kind, needle", [
    ("deliverables", "push_to_dropbox"),
    ("no such kind", "not a kind of file cp knows"),
    ("", "`kind` is required"),
])
def test_a_kind_that_is_not_a_drive_kind_is_refused_before_anything_is_read(
        pt, env, server, monkeypatch, kind, needle):
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: pytest.fail("resolved a project"))
    out = pt.push_to_drive("slt-5196", "deck.pdf", kind, _b64())
    assert needle in out["error"]
    assert env.writers_built == 0
    if kind == "":
        assert out["candidates"] and all(c["store"] == "drive" for c in out["candidates"])


def test_a_project_with_no_drive_folder_says_so(pt, env):
    env.row = {"google_drive_folder_id": None}
    out = pt.push_to_drive("slt-5196", "wrap-v02.docx", "wrap", _b64())
    assert "no Google Drive folder" in out["error"]
    assert env.writers_built == 0


def test_a_filename_with_a_folder_in_it_is_refused(pt, env):
    out = pt.push_to_drive("slt-5196", "sub/wrap.docx", "wrap", _b64())
    assert "bare file name" in out["error"] and env.writers_built == 0


def test_a_non_team_caller_is_refused_first(pt, env, server, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "no tree access"))
    out = pt.push_to_drive("slt-5196", "wrap-v02.docx", "wrap", _b64())
    assert out["error"] == "no access" and env.writers_built == 0


def test_the_dropbox_push_points_a_drive_kind_at_this_verb(pt, env):
    out = pt.push_to_dropbox("slt-5196", "INV_4415.pdf", _b64(), kind="invoice")
    assert "push_to_drive(project_code, 'INV_4415.pdf', kind=\"invoices\")" in out["error"]
