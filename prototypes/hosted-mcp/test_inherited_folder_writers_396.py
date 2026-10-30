"""The hosted Dropbox writers read an INHERITED folder the way ingest does (#396).

WHAT BROKE. PR #401 taught ingest to use a parent's subfolder for a
workstream with Dropbox on and no folder bound. `push_to_dropbox`,
`ingest_url` and `add_project_link` still read only the bound folder, so the
same workstream was refused there ("has no Dropbox folder in MC-2") even
though its ingest would have read the folder.

WHAT THESE PIN.
1. Each writer uploads into the inherited folder and queues the ingest of
   that file, and the result names the folder and the ancestor
   (`folder_inherited`).
2. No unique subfolder in the parent's folder → still refused, and the
   refusal says why nothing was inherited.
3. A bound folder is used as before, with no `folder_inherited` and no
   listing of the parent.

Dropbox, MC-2 and mc-2 are mocked; the resolver itself is the real one.

    python -m pytest prototypes/hosted-mcp/test_inherited_folder_writers_396.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

ACCT = "/1P Active Projects/IBX 5217 Infoblox"
JOB_DIR = f"{ACCT}/IBX 5153 AI Campaign"


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


def _folders(pid, **kw):
    from cp_engine.asset_ingest import ProjectFolders

    base = dict(project_id=pid, company_id="co1", company_kind="client",
                google_drive_folder_id=None, mc_dropbox_folder_id=None,
                enable_google_drive=False, enable_dropbox=True)
    base.update(kw)
    return ProjectFolders(**base)


def _entry(name, path):
    return SimpleNamespace(name=name, path_display=path)  # no `size` → a folder


class _Dbx:
    """{folder: [entries]}; an unknown folder raises not_found, like the API."""

    def __init__(self, tree):
        self.tree = tree
        self.listed: list[str] = []

    def files_list_folder(self, folder):
        self.listed.append(folder)
        if folder not in self.tree:
            raise RuntimeError("ApiError('path', LookupError('not_found'))")
        return SimpleNamespace(entries=list(self.tree[folder]), has_more=False, cursor=None)

    def files_get_temporary_upload_link(self, commit_info):
        return SimpleNamespace(link="https://up.invalid/t")


@pytest.fixture
def env(server, monkeypatch):
    client = SimpleNamespace(name="caller-client")  # no .table: children unreadable
    audit = _Audit()
    state = SimpleNamespace(
        rows={"acct": dict(code="ibx-5217-infoblox", mc_dropbox_folder_id=ACCT),
              "job": dict(parent_id="acct", number=5153, name="AI Campaign",
                          full_job_name="IBX 5153 AI Campaign",
                          code="ibx-5153-ai-campaign", has_agreement=True)},
        dbx=_Dbx({ACCT: [_entry("IBX 5153 AI Campaign", JOB_DIR),
                         _entry("Brand", f"{ACCT}/Brand")]}),
        pushes=[], hops=[], audit=audit,
    )
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@firstperson.is")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "job")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "upstream_code", lambda code, scope=None: "ibx-5153-ai-campaign")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code})
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "t")
    import cloud_storage.dropbox_connector as dc

    monkeypatch.setattr(dc, "DropboxConnector", lambda: SimpleNamespace(dbx=state.dbx))
    # A fresh row per read: the rules set the inherited folder IN PLACE.
    monkeypatch.setattr("cp_engine.asset_ingest.resolve_project_folders_by_id",
                        lambda c, pid: _folders(pid, **state.rows[pid])
                        if pid in state.rows else None)
    monkeypatch.setattr(server.ported_tools, "_store_link_cards", lambda c, pid: [])

    def fake_push(connector, folder_id, local_path, dest_name=None, overwrite=False,
                  dest_path=None):
        state.pushes.append({"folder_id": folder_id, "dest_name": dest_name})
        root = dest_path or folder_id
        return {"dropbox_path": f"{root}/{dest_name}", "name": Path(dest_name).name,
                "size": 1, "overwrote": False}

    monkeypatch.setattr(server._engine_project_sources, "push_to_dropbox", fake_push)
    monkeypatch.setattr(server._engine_project_sources, "dropbox_destination",
                        lambda conn, fid, name, dest_path=None: f"{dest_path or fid}/{name}")

    def fake_hop(code, mc_project_id, file_ids):
        state.hops.append({"mc_project_id": mc_project_id, "file_ids": list(file_ids)})
        return {"ok": True, "run_id": "r1", "status": "running"}

    monkeypatch.setattr(server, "call_mc2_ingest_assets", fake_hop)
    return state


def _b64(data=b"deck"):
    return base64.b64encode(data).decode()


INHERITED = {"path": JOB_DIR, "from": "ibx-5217-infoblox"}


def test_push_uploads_into_the_inherited_folder_and_queues_it(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "deck.pdf", _b64(), ingest=True)
    assert "error" not in out, out
    assert env.pushes[0]["folder_id"] == JOB_DIR
    assert out["dropbox_path"] == f"{JOB_DIR}/03 Assets/06 Spine/deck.pdf"
    assert out["folder_inherited"] == INHERITED
    assert out["ingest"]["queued"] is True
    assert env.hops == [{"mc_project_id": "job", "file_ids": [out["dropbox_path"]]}]


def test_push_upload_link_names_the_inherited_folder(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "deck.pdf")
    assert out["dropbox_path"].startswith(JOB_DIR + "/")
    assert out["folder_inherited"] == INHERITED


def test_ingest_url_writes_to_and_queues_from_the_inherited_folder(pt, env, monkeypatch):
    from cp_engine import url_source

    monkeypatch.setattr(url_source, "fetch_url", lambda url, **kw: url_source.Fetched(
        url=url, content_type="text/html",
        data=b"<html><head><title>Guide</title></head><body><main><h1>Guide</h1>"
             + b"<p>" + b"word " * 400 + b"</p></main></body></html>"))
    out = pt.ingest_url("ibx-5153", "https://acme.example/guide")
    assert "error" not in out, out
    assert env.pushes[0]["folder_id"] == JOB_DIR
    assert out["folder_inherited"] == INHERITED
    assert out["ingest"]["queued"] is True


def test_add_project_link_writes_the_card_into_the_inherited_folder(pt, env):
    out = pt.add_project_link("ibx-5153", "https://www.figma.com/design/abc/Schedule",
                              title="Schedule")
    assert "error" not in out, out
    assert env.pushes[0]["folder_id"] == JOB_DIR
    assert out["folder_inherited"] == INHERITED
    assert out["ingest"]["queued"] is True


def test_no_matching_subfolder_is_refused_and_says_why(pt, env):
    env.dbx.tree[ACCT] = [_entry("Brand", f"{ACCT}/Brand")]
    out = pt.push_to_dropbox("ibx-5153", "deck.pdf", _b64(), ingest=True)
    assert "has no Dropbox folder in MC-2" in out["error"]
    assert "Nothing to inherit from a parent: no subfolder for this workstream" in out["error"]
    assert not env.pushes and not env.hops


def test_a_bound_folder_is_used_without_listing_the_parent(pt, env):
    env.rows["job"]["mc_dropbox_folder_id"] = "/Elsewhere/IBX 5153"
    out = pt.push_to_dropbox("ibx-5153", "deck.pdf", _b64(), ingest=True)
    assert env.pushes[0]["folder_id"] == "/Elsewhere/IBX 5153"
    assert "folder_inherited" not in out
    assert env.dbx.listed == []
    assert out["ingest"]["queued"] is True


def test_a_disabled_source_inherits_nothing(pt, env):
    env.rows["job"]["enable_dropbox"] = False
    out = pt.push_to_dropbox("ibx-5153", "deck.pdf", _b64())
    assert "has no Dropbox folder in MC-2" in out["error"]
    assert env.dbx.listed == []
