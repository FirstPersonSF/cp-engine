"""`where_is` and `push_to_dropbox(kind=...)`: cp knows where files are kept.

WHAT BROKE. On 2026-11-13 a session pushed an invoice to Dropbox
`03 Assets/06 Spine` when it was already filed on Drive under
`Management/02 Contracts and Financials/05 Invoices PDFs`. Nothing told it
where invoices live. Drew: "I think we should teach cp where all of the files
are kept between dropbox and drive".

WHAT THESE PIN.
1. `where_is(code)` returns the whole map resolved for the project, with the
   client-inputs rule, and lists nothing.
2. `where_is(code, kind)` lists the folder for a Dropbox kind and for a Drive
   kind (walking the Drive path by exact folder name), caps the listing and
   says so, names the missing segment when the folder is not there, and
   says so when the project has no folder in that store.
3. An unknown or ambiguous kind returns candidates; a non-team caller is
   refused before any folder is read.
4. `push_to_dropbox(kind=...)` files a Dropbox kind in its folder, refuses a
   Drive kind before any credential, folder or upload is touched, and
   refuses a `dest_name` / `dest_path` that names another folder.

Dropbox, Drive and MC-2 are fakes; the file map and the folder rules are the
real ones. Nothing here reaches a live store.

    python -m pytest prototypes/hosted-mcp/test_where_is.py -v
"""
from __future__ import annotations

import base64
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = "/1P Active Projects/SLT 5196 Brand Campaign 26"
DRIVE_ROOT = "drv-root"
FOLDER_MIME = "application/vnd.google-apps.folder"
INVOICES = "Management/02 Contracts and Financials/05 Invoices PDFs"


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


def _dfolder(name, parent):
    return SimpleNamespace(name=name, path_display=f"{parent}/{name}")  # no `size`


def _dfile(name, parent, size=10):
    return SimpleNamespace(name=name, path_display=f"{parent}/{name}", size=size,
                           server_modified=dt.datetime(2026, 11, 12, 9, 30))


class _Dbx:
    """{folder path: [entries]}. An unknown path raises not_found, like the API."""

    def __init__(self, tree):
        self.tree = tree
        self.listed: list[str] = []
        self.calls = 0

    def files_list_folder(self, folder):
        self.calls += 1
        self.listed.append(folder)
        if folder not in self.tree:
            raise RuntimeError("ApiError('path', LookupError('not_found'))")
        return SimpleNamespace(entries=list(self.tree[folder]), has_more=False, cursor=None)

    def files_get_metadata(self, path):
        self.calls += 1
        if path in self.tree:
            return SimpleNamespace(name=path.rsplit("/", 1)[-1], path_display=path)
        raise RuntimeError("ApiError('path', LookupError('not_found'))")

    def files_get_temporary_upload_link(self, commit_info):
        self.calls += 1
        return SimpleNamespace(link="https://up.invalid/t")


class _Drive:
    """{folder id: [items]}: the one listing primitive the engine uses."""

    def __init__(self, tree):
        self.tree = tree
        self.queries: list[str] = []

    def _list_files_with_pagination(self, query, fields, page_size=100, **kw):
        self.queries.append(query)
        parent = query.split("'")[1]
        if parent not in self.tree:
            raise RuntimeError("HttpError 404: File not found")
        items = list(self.tree[parent])
        if "mimeType=" in query:
            items = [i for i in items if i.get("mimeType") == FOLDER_MIME]
        return items


def _gfolder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME}


def _gfile(fid, name, **kw):
    return {"id": fid, "name": name, "mimeType": "application/pdf", "size": "2048",
            "modifiedTime": "2026-11-10T17:00:00.000Z",
            "webViewLink": f"https://drive.google.com/file/d/{fid}/view", **kw}


def _folders(pid, **kw):
    from cp_engine.asset_ingest import ProjectFolders

    base = dict(project_id=pid, company_id="co1", company_kind="client",
                google_drive_folder_id=DRIVE_ROOT, mc_dropbox_folder_id=ROOT,
                enable_google_drive=True, enable_dropbox=True, has_agreement=True)
    base.update(kw)
    return ProjectFolders(**base)


@pytest.fixture
def env(server, monkeypatch):
    client = SimpleNamespace(name="caller-client")  # no .table: children unreadable
    audit = _Audit()
    assets = f"{ROOT}/03 Assets"
    state = SimpleNamespace(
        row={},
        dbx=_Dbx({
            ROOT: [_dfolder("03 Assets", ROOT), _dfolder("04 Deliverables", ROOT)],
            assets: [_dfolder("01 Client Assets", assets), _dfolder("06 Spine", assets)],
            f"{assets}/01 Client Assets": [
                _dfile("brand-guide.pdf", f"{assets}/01 Client Assets", size=4096),
                _dfolder("Fonts", f"{assets}/01 Client Assets"),
            ],
            f"{assets}/06 Spine": [],
            f"{ROOT}/04 Deliverables": [],
        }),
        drive=_Drive({
            DRIVE_ROOT: [_gfolder("mgmt", "Management"), _gfolder("def", "Define and Approach")],
            "mgmt": [_gfolder("fin", "02 Contracts and Financials"),
                     _gfolder("exp", "01 1P Expenses")],
            "fin": [_gfolder("inv", "05 Invoices PDFs"), _gfolder("sow", "03 SOWs")],
            "inv": [_gfile("f1", "INV-1042.pdf"), _gfolder("old", "Archive"),
                    {"id": "g1", "name": "Invoice log", "modifiedTime": "2026-11-01T00:00:00Z",
                     "mimeType": "application/vnd.google-apps.spreadsheet"}],
            "sow": [],
            "def": [],
        }),
        pushes=[], audit=audit, drive_built=0,
    )
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "job")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "t")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "not-a-real-file")
    import cloud_storage.dropbox_connector as dc
    import cloud_storage.google_drive_connector as gc

    monkeypatch.setattr(dc, "DropboxConnector", lambda: SimpleNamespace(dbx=state.dbx))

    def _drive(**kw):
        state.drive_built += 1
        return state.drive

    monkeypatch.setattr(gc, "GoogleDriveConnector", _drive)
    monkeypatch.setattr("cp_engine.asset_ingest.resolve_project_folders_by_id",
                        lambda c, pid: _folders(pid, **state.row))

    def fake_push(connector, folder_id, local_path, dest_name=None, overwrite=False,
                  dest_path=None):
        state.pushes.append({"folder_id": folder_id, "dest_name": dest_name,
                             "dest_path": dest_path})
        return {"dropbox_path": f"{dest_path or folder_id}/{dest_name}",
                "name": Path(dest_name).name, "size": 1, "overwrote": False}

    monkeypatch.setattr(server._engine_project_sources, "push_to_dropbox", fake_push)
    return state


def _b64(data=b"deck"):
    return base64.b64encode(data).decode()


# ── the whole map ─────────────────────────────────────────────────────


def test_no_kind_returns_the_whole_map_resolved_for_the_project(pt, env):
    from cp_engine import file_map

    out = pt.where_is("slt-5196")
    assert "error" not in out, out
    assert out["stores"]["dropbox"] == {"root": ROOT}
    assert out["stores"]["drive"] == {
        "root_id": DRIVE_ROOT,
        "root_url": f"https://drive.google.com/drive/folders/{DRIVE_ROOT}"}
    assert out["client_inputs_rule"] == file_map.CLIENT_INPUTS_RULE
    by_kind = {k["kind"]: k for k in out["kinds"]}
    assert set(by_kind) == {k.key for k in file_map.FILE_KINDS}
    assert by_kind["invoices"]["store"] == "drive"
    assert by_kind["invoices"]["path"] == INVOICES
    assert by_kind["invoices"]["root_url"].endswith(DRIVE_ROOT)
    assert by_kind["client-inputs"]["dropbox_path"] == f"{ROOT}/03 Assets/01 Client Assets"
    assert "Google-native" in by_kind["client-inputs"]["note"]
    assert by_kind["deliverables"]["ingest_reads_it"] is True
    # The map is a lookup: no folder was listed in either store.
    assert env.dbx.calls == 0 and env.drive.queries == [] and env.drive_built == 0
    assert env.audit == [("where_is", {"project_code": "slt-5196"}, len(out["kinds"]))]


def test_the_map_says_which_folders_ingest_does_not_read(pt, env):
    env.row["asset_ingest_folders"] = ("Client Assets", "Spine")
    by_kind = {k["kind"]: k for k in pt.where_is("slt-5196")["kinds"]}
    assert by_kind["client-inputs"]["ingest_reads_it"] is True
    assert by_kind["deliverables"]["ingest_reads_it"] is False
    assert "ingest folders" in by_kind["deliverables"]["ingest_note"]
    assert by_kind["invoices"]["ingest_reads_it"] is False


def test_the_map_says_when_a_store_has_no_folder(pt, env):
    env.row["google_drive_folder_id"] = None
    out = pt.where_is("slt-5196")
    assert out["stores"]["drive"]["root_url"] is None
    assert "no Google Drive folder in MC-2" in out["stores"]["drive"]["missing"]
    invoices = next(k for k in out["kinds"] if k["kind"] == "invoices")
    assert invoices["root_url"] is None and "ingest_reads_it" not in invoices
    assert out["stores"]["dropbox"]["root"] == ROOT


# ── one kind: Dropbox ─────────────────────────────────────────────────


def test_a_dropbox_kind_lists_its_folder(pt, env):
    out = pt.where_is("slt-5196", "client-inputs")
    assert "error" not in out, out
    assert out["store"] == "dropbox" and out["exists"] is True
    assert out["dropbox_path"] == f"{ROOT}/03 Assets/01 Client Assets"
    assert out["entries"] == [
        {"name": "Fonts", "is_folder": True, "size": None, "modified": None},
        {"name": "brand-guide.pdf", "is_folder": False, "size": 4096,
         "modified": "2026-11-12T09:30:00"},
    ]
    assert out["count"] == 2 and out["capped"] is False
    assert "Google-native" in out["client_inputs_rule"]
    assert env.audit[-1] == ("where_is", {"project_code": "slt-5196",
                                          "kind": "client-inputs"}, 2)


def test_a_plain_word_finds_the_kind(pt, env):
    out = pt.where_is("slt-5196", "Finished Deliverables")
    assert out["kind"] == "deliverables" and out["entries"] == []
    assert out["exists"] is True


def test_a_long_dropbox_listing_is_capped_and_says_so(pt, env):
    folder = f"{ROOT}/04 Deliverables"
    env.dbx.tree[folder] = [_dfile(f"cut-{i:03}.mov", folder) for i in range(130)]
    out = pt.where_is("slt-5196", "deliverables")
    assert out["count"] == 100 == len(out["entries"])
    assert out["capped"] is True and "more than 100" in out["capped_note"]


def test_a_missing_dropbox_folder_names_the_missing_segment(pt, env):
    out = pt.where_is("slt-5196", "renders")
    assert "error" not in out, out
    assert out["exists"] is False
    assert out["missing_segment"] == "03 Renders"
    assert out["found_path"] == f"{ROOT}/03 Assets"
    assert out["folders_there"] == ["01 Client Assets", "06 Spine"]
    assert "`03 Renders`" in out["missing"] and "Nothing was created" in out["missing"]
    assert "entries" not in out


def test_an_initiatives_spine_folder_is_its_bare_spine(pt, env):
    """An initiative's Dropbox folder has no template: generated work is in
    `Spine`, and `where_is` looks there, as `push_to_dropbox` writes there."""
    env.dbx.tree[f"{ROOT}/Spine"] = [_dfile("note.md", f"{ROOT}/Spine")]
    out = pt.where_is("slt-5196", "spine")
    assert out["path"] == "Spine" and out["dropbox_path"] == f"{ROOT}/Spine"
    assert [e["name"] for e in out["entries"]] == ["note.md"]


# ── one kind: Drive ───────────────────────────────────────────────────


def test_a_drive_kind_walks_the_path_and_lists_the_folder(pt, env):
    out = pt.where_is("slt-5196", "invoices")
    assert "error" not in out, out
    assert out["store"] == "drive" and out["exists"] is True
    assert out["path"] == INVOICES
    assert out["folder_id"] == "inv"
    assert out["folder_url"] == "https://drive.google.com/drive/folders/inv"
    assert out["root_id"] == DRIVE_ROOT
    assert out["entries"] == [
        {"name": "Archive", "is_folder": True, "size": None, "modified": None,
         "id": "old", "url": None},
        {"name": "INV-1042.pdf", "is_folder": False, "size": 2048,
         "modified": "2026-11-10T17:00:00.000Z", "id": "f1",
         "url": "https://drive.google.com/file/d/f1/view"},
        # A Google-native file has no size on Drive.
        {"name": "Invoice log", "is_folder": False, "size": None,
         "modified": "2026-11-01T00:00:00Z", "id": "g1", "url": None},
    ]
    assert out["count"] == 3 and out["capped"] is False
    # Walked one folder at a time from the project's root, then listed once.
    parents = [q.split("'")[1] for q in env.drive.queries]
    assert parents == [DRIVE_ROOT, "mgmt", "fin", "inv"]
    assert env.dbx.calls == 0


def test_a_long_drive_listing_is_capped_and_says_so(pt, env):
    env.drive.tree["sow"] = [_gfile(f"s{i}", f"SOW-{i:03}.pdf") for i in range(101)]
    out = pt.where_is("slt-5196", "sow")
    assert out["kind"] == "sows" and out["count"] == 100 and out["capped"] is True


def test_a_missing_drive_segment_is_named_and_no_path_is_invented(pt, env):
    out = pt.where_is("slt-5196", "purchase-orders")
    assert "error" not in out, out
    assert out["exists"] is False
    assert out["missing_segment"] == "04 Purchase Orders"
    assert out["found_path"] == "Management/02 Contracts and Financials"
    assert out["found_url"] == "https://drive.google.com/drive/folders/fin"
    assert out["folders_there"] == ["03 SOWs", "05 Invoices PDFs"]
    assert "folder_id" not in out and "folder_url" not in out and "entries" not in out
    assert "`04 Purchase Orders`" in out["missing"]


def test_a_missing_first_segment_points_at_the_projects_own_folder(pt, env):
    env.drive.tree[DRIVE_ROOT] = [_gfolder("def", "Define and Approach")]
    out = pt.where_is("slt-5196", "invoices")
    assert out["missing_segment"] == "Management" and out["found_path"] == ""
    assert "the project's Drive folder" in out["missing"]


def test_two_drive_folders_with_one_name_are_not_chosen_between(pt, env):
    env.drive.tree["fin"].append(_gfolder("inv2", "05 Invoices PDFs"))
    out = pt.where_is("slt-5196", "invoices")
    assert out["exists"] is False and out["missing_segment"] == "05 Invoices PDFs"
    assert "2 folders named" in out["missing"] and "folder_id" not in out


def test_a_drive_failure_is_a_named_error_not_an_empty_folder(pt, env):
    del env.drive.tree["mgmt"]
    out = pt.where_is("slt-5196", "invoices")
    assert "could not read the project's Google Drive folder" in out["error"]
    assert "entries" not in out and "exists" not in out


# ── no folder, no credential, no such kind, no access ─────────────────


def test_no_drive_folder_bound_says_so(pt, env):
    env.row["google_drive_folder_id"] = None
    out = pt.where_is("slt-5196", "invoices")
    assert "has no Google Drive folder in MC-2" in out["error"]
    assert "service=google_drive" in out["error"]
    assert out["path"] == INVOICES and env.drive.queries == []


def test_no_dropbox_folder_bound_says_so(pt, env):
    env.row["mc_dropbox_folder_id"] = None
    out = pt.where_is("slt-5196", "deliverables")
    assert "has no Dropbox folder in MC-2" in out["error"]
    assert env.dbx.calls == 0


def test_a_missing_drive_credential_is_named_without_a_value(pt, env, monkeypatch):
    from cp_engine.mc2_db import DRIVE_CRED_KEYS

    for key in DRIVE_CRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    out = pt.where_is("slt-5196", "invoices")
    assert "no Google Drive credentials on the hosted server" in out["error"]
    assert env.drive_built == 0


def test_an_ambiguous_kind_returns_the_candidates(pt, env):
    out = pt.where_is("slt-5196", "vendor")
    assert "matches several kinds" in out["error"]
    assert [c["kind"] for c in out["candidates"]] == ["vendor-invoices", "vendor-contracts"]
    assert env.dbx.calls == 0 and env.drive.queries == []


def test_an_unknown_kind_returns_every_kind_to_choose_from(pt, env):
    from cp_engine import file_map

    out = pt.where_is("slt-5196", "timesheets")
    assert "is not a kind of file cp knows" in out["error"]
    assert len(out["candidates"]) == len(file_map.FILE_KINDS)


def test_an_unknown_project_is_not_found(pt, env):
    out = pt.where_is("ghost", "invoices")
    assert "no project or initiative resolves" in out["error"]


def test_a_non_team_caller_is_refused_before_anything_is_read(pt, env, server, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member",
                        lambda: (False, "tree access is limited to team members"))
    for kind in ("", "invoices", "deliverables"):
        out = pt.where_is("slt-5196", kind)
        assert out == {"project_code": "slt-5196",
                       "error": "access is limited to team members"}
    assert env.dbx.calls == 0 and env.drive.queries == [] and env.drive_built == 0
    assert all(a[1].get("result") == "error" for a in env.audit) and len(env.audit) == 3


def test_where_is_is_served_on_the_read_only_endpoint(server):
    assert "where_is" in server.READ_ONLY_TOOLS
    assert "where_is" in {t.name for t in server.read_server._tool_manager.list_tools()}


# ── push_to_dropbox(kind=...) ─────────────────────────────────────────


def test_push_with_a_dropbox_kind_files_it_in_that_kinds_folder(pt, env):
    out = pt.push_to_dropbox("slt-5196", "final-cut.mov", _b64(), kind="deliverables")
    assert "error" not in out, out
    assert env.pushes == [{"folder_id": ROOT, "dest_name": "04 Deliverables/final-cut.mov",
                           "dest_path": None}]
    assert out["dropbox_path"] == f"{ROOT}/04 Deliverables/final-cut.mov"


def test_push_with_a_kind_and_no_content_links_to_that_kinds_folder(pt, env, server,
                                                                   monkeypatch):
    monkeypatch.setattr(server._engine_project_sources, "dropbox_destination",
                        lambda conn, fid, name, dest_path=None: f"{dest_path or fid}/{name}")
    out = pt.push_to_dropbox("slt-5196", "logo.ai", kind="client assets")
    assert out["dropbox_path"] == f"{ROOT}/03 Assets/01 Client Assets/logo.ai"
    assert out["upload_url"]


def test_push_with_a_drive_kind_is_refused_before_any_upload(pt, env, server, monkeypatch):
    """The 11-13 failure: an invoice must not land on Dropbox. Refused before
    a credential, a folder or a project row is read."""
    monkeypatch.delenv("DROPBOX_ACCESS_TOKEN")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: pytest.fail("resolved a project"))
    out = pt.push_to_dropbox("slt-5196", "INV-1043.pdf", _b64(), kind="invoice")
    assert "Nothing was uploaded" in out["error"]
    assert "Google Drive" in out["error"] and f"`{INVOICES}`" in out["error"]
    assert "cannot write to Google Drive yet" in out["error"]
    assert out["kind"] == "invoices" and out["store"] == "drive" and out["path"] == INVOICES
    assert env.pushes == [] and env.dbx.calls == 0
    assert env.audit == [("push_to_dropbox",
                          {"project_code": "slt-5196", "result": "error"}, 0)]


@pytest.mark.parametrize("extra", [
    {"dest_name": "03 Assets/06 Spine/deck.pdf"},
    {"dest_name": "deck.pdf"},
    {"dest_path": "/1P Active Projects/Other/03 Assets/06 Spine"},
    {"dest_path": f"{ROOT}/04 Deliverables", "dest_name": "sub/deck.pdf"},
])
def test_push_with_a_kind_and_a_different_destination_is_refused(pt, env, extra):
    out = pt.push_to_dropbox("slt-5196", "deck.pdf", _b64(), kind="deliverables", **extra)
    assert "Nothing was uploaded" in out["error"] and "`04 Deliverables`" in out["error"]
    assert env.pushes == [] and env.dbx.calls == 0


@pytest.mark.parametrize("extra, pushed", [
    ({"dest_name": "04 Deliverables/deck-v02.pdf"},
     {"folder_id": ROOT, "dest_name": "04 Deliverables/deck-v02.pdf", "dest_path": None}),
    ({"dest_path": f"{ROOT}/04 Deliverables"},
     {"folder_id": ROOT, "dest_name": "deck.pdf", "dest_path": f"{ROOT}/04 Deliverables"}),
])
def test_push_with_a_kind_and_the_same_destination_goes_through(pt, env, extra, pushed):
    out = pt.push_to_dropbox("slt-5196", "deck.pdf", _b64(), kind="deliverables", **extra)
    assert "error" not in out, out
    assert env.pushes == [pushed]


def test_push_with_an_unknown_kind_is_refused_with_candidates(pt, env):
    out = pt.push_to_dropbox("slt-5196", "deck.pdf", _b64(), kind="vendor")
    assert "matches several kinds" in out["error"]
    assert [c["kind"] for c in out["candidates"]] == ["vendor-invoices", "vendor-contracts"]
    assert env.pushes == []


def test_push_without_a_kind_still_defaults_to_the_spine_folder(pt, env):
    out = pt.push_to_dropbox("slt-5196", "deck.pdf", _b64())
    assert env.pushes == [{"folder_id": ROOT, "dest_name": "03 Assets/06 Spine/deck.pdf",
                           "dest_path": None}]
    assert out["dropbox_path"] == f"{ROOT}/03 Assets/06 Spine/deck.pdf"


def test_the_servers_folder_names_come_from_the_one_map(pt):
    from cp_engine import file_map

    assert file_map.get("spine").path == pt.SPINE_OUTPUT_DIR
    assert pt.SPINE_OUTPUT_DIR == "03 Assets/06 Spine"
    assert file_map.get("client-inputs").path == pt.URL_SOURCE_DIR
    assert pt.INITIATIVE_SPINE_DIR == file_map.INITIATIVE_SPINE_DIR == "Spine"
