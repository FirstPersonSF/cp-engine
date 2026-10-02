"""The verbs ported from the retired stdio server (architecture plan step 5b).

`cxp mcp` is gone; these seven verbs moved to the hosted server as thin
wrappers over engine functions (`ported_tools.py`). What each test pins is the
part that CHANGED in the move, because that is where a port goes wrong
quietly:

- the caller's disk is not the server's — bytes travel in the result
  (`content_base64`, a temporary `download_url`) or out through an
  `upload_url`, never as a `local_path`;
- a read that uses the service's Drive/Dropbox credentials reaches past RLS,
  so it is team-gated, and a missing credential is a named error;
- every engine call gets the CALLER's client, never one the verb builds.

    python -m pytest prototypes/hosted-mcp/test_ported_tools.py -v
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

PORTED = ("fetch_project_source", "compare_project_sources", "pull_document_comments",
          "push_to_dropbox", "preflight", "list_vendors", "list_rfp_respondents")


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


@pytest.fixture
def caller(server, monkeypatch):
    """A team member whose client is a sentinel, resolving every code to p1."""
    client = SimpleNamespace(name="caller-client")
    audit = _Audit()
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p1")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "_with_project_status", lambda r, *a: r)
    monkeypatch.setattr(server, "audit", audit)
    for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN",
              "DROPBOX_ACCESS_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON",
              "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_CREDENTIALS_PATH"):
        monkeypatch.delenv(k, raising=False)
    return SimpleNamespace(client=client, audit=audit)


def _fake_fetch(content: bytes, provider="dropbox", seen=None):
    def fetch(client, pid, title, dest, company_id=None):
        if seen is not None:
            seen.append((client, pid, title, company_id))
        path = Path(dest) / f"{title}.pptx"
        path.write_bytes(content)
        return {"local_path": str(path), "title": title, "provider": provider,
                "url": "https://example.invalid/x", "source_path": "/Clients/Acme/01",
                "source_file_id": "id:abc"}
    return fetch


# ── registration ──────────────────────────────────────────────────────


def test_every_ported_verb_is_registered_audited_and_strict(server):
    tools = {t.name: t for t in server.mcp_server._tool_manager.list_tools()}
    for name in PORTED:
        assert name in tools, name
        assert getattr(tools[name].fn, "__cp_audit_guaranteed__", False), name
        assert tools[name].parameters.get("additionalProperties") is False, name


def test_the_writer_is_main_only_and_the_reads_are_on_the_read_endpoint(server):
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    assert "push_to_dropbox" not in read
    assert "push_to_dropbox" in server.MAIN_ONLY_TOOLS
    assert set(PORTED) - {"push_to_dropbox"} <= read


def test_a_local_path_argument_from_the_stdio_era_is_refused(server):
    """`push_to_dropbox(local_path=...)` was the stdio signature; the server
    cannot read the caller's disk, so the old name must fail loudly."""
    with pytest.raises(Exception) as exc:
        asyncio.run(server.mcp_server.call_tool("push_to_dropbox", {
            "project_code": "p", "local_path": "/tmp/deck.pptx"}))
    assert "local_path" in str(exc.value)


# ── fetch_project_source ──────────────────────────────────────────────


def test_fetch_returns_the_bytes_not_a_server_path(server, pt, caller, monkeypatch):
    seen = []
    monkeypatch.setattr(server._engine_project_sources, "fetch_source",
                        _fake_fetch(b"PPTX-BYTES", seen=seen))
    monkeypatch.setattr(pt, "_dropbox_temporary_link",
                        lambda fid: {"download_url": f"https://dl.invalid/{fid}"})
    out = pt.fetch_project_source("ibx-5153", "Deck")
    assert "local_path" not in out
    assert base64.b64decode(out["content_base64"]) == b"PPTX-BYTES"
    assert out["sha256"] == hashlib.sha256(b"PPTX-BYTES").hexdigest()
    assert out["inline"] is True and out["size"] == 10
    assert out["download_url"] == "https://dl.invalid/id:abc"
    assert out["source_path"] == "/Clients/Acme/01"
    # the CALLER's client reached the engine, with the company for account docs
    assert seen == [(caller.client, "p1", "Deck", "co1")]
    assert caller.audit[-1][0] == "fetch_project_source"


def test_fetch_over_the_inline_cap_returns_size_and_no_bytes(server, pt, caller, monkeypatch):
    monkeypatch.setattr(server._engine_project_sources, "fetch_source",
                        _fake_fetch(b"x" * 100, provider="drive"))
    out = pt.fetch_project_source("ibx-5153", "Deck", inline_max_bytes=10)
    assert out["inline"] is False and "content_base64" not in out
    assert "inline_max_bytes" in out["inline_note"]
    assert "download_url" not in out  # Drive: no temporary link


def test_fetch_download_failure_names_the_missing_credentials(server, pt, caller, monkeypatch):
    monkeypatch.setattr(server._engine_project_sources, "fetch_source",
                        lambda *a, **k: {"error": "download failed: No Dropbox credentials found"})
    out = pt.fetch_project_source("ibx-5153", "Deck")
    assert "DROPBOX_REFRESH_TOKEN" in out["error"]
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in out["error"]
    assert caller.audit[-1][1]["result"] == "error"


def test_fetch_is_refused_to_a_non_team_caller(server, pt, caller, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member",
                        lambda: (False, "tree access denied: not a team member"))
    ran = []
    monkeypatch.setattr(server._engine_project_sources, "fetch_source",
                        lambda *a, **k: ran.append(1))
    out = pt.fetch_project_source("ibx-5153", "Deck")
    assert "denied" in out["error"] and not ran
    assert caller.audit, "a refusal is still audited"


def test_fetch_unknown_code(pt, caller):
    assert "no project" in pt.fetch_project_source("ghost", "Deck")["error"]


# ── compare_project_sources ───────────────────────────────────────────


def test_compare_refuses_a_local_path(pt, caller):
    out = pt.compare_project_sources("ibx-5153", "/Users/me/deck.pptx", "Deck v2")
    assert "cannot read your disk" in out["error"]


def test_compare_fetches_both_titles_and_diffs_them(server, pt, caller, monkeypatch):
    seen = []
    monkeypatch.setattr(server._engine_project_sources, "fetch_source",
                        _fake_fetch(b"x", seen=seen))
    got = {}

    def fake_compare(a, b):
        got["paths"] = (Path(a).name, Path(b).name)
        return {"overall_similarity": 0.9}

    monkeypatch.setattr("cp_engine.source_compare.compare_files", fake_compare)
    out = pt.compare_project_sources("ibx-5153", "Deck v1", "Deck v2")
    assert out["overall_similarity"] == 0.9
    assert got["paths"] == ("Deck v1.pptx", "Deck v2.pptx")
    assert [s[2] for s in seen] == ["Deck v1", "Deck v2"]


def test_compare_names_the_side_that_failed(server, pt, caller, monkeypatch):
    def fetch(client, pid, title, dest, company_id=None):
        if title == "B":
            return {"error": "no source titled 'B' in project"}
        return _fake_fetch(b"x")(client, pid, title, dest, company_id)

    monkeypatch.setattr(server._engine_project_sources, "fetch_source", fetch)
    assert pt.compare_project_sources("ibx-5153", "A", "B")["error"].startswith("doc_b:")


# ── pull_document_comments ────────────────────────────────────────────


def test_comments_delegate_with_the_callers_client(server, pt, caller, monkeypatch):
    seen = []

    def fake(client, pid, title, dest, company_id=None):
        seen.append((client, pid, title, company_id))
        return {"title": title, "provider": "drive", "comment_count": 2,
                "comments": [{"author": "Scott"}, {"author": "Cricket"}]}

    monkeypatch.setattr(server._engine_project_sources, "pull_document_comments", fake)
    out = pt.pull_document_comments("ibx-5153", "Our AI Story")
    assert out["comment_count"] == 2
    # the company id is passed so account-scoped docs resolve, as in fetch
    assert [s[:3] for s in seen] == [(caller.client, "p1", "Our AI Story")]
    assert seen[0][3] is not None
    assert caller.audit[-1][2] == 2


def test_unreadable_comments_name_the_credential(server, pt, caller, monkeypatch):
    monkeypatch.setattr(server._engine_project_sources, "pull_document_comments",
                        lambda *a, **k: {"error": "could not read Drive comments for 'X' "
                                             "(ValueError: no creds)"})
    out = pt.pull_document_comments("ibx-5153", "X")
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in out["error"]


# ── push_to_dropbox ───────────────────────────────────────────────────


class _FakeDbx:
    def __init__(self):
        self.commits = []

    def files_get_temporary_upload_link(self, commit_info):
        self.commits.append(commit_info)
        return SimpleNamespace(link="https://up.invalid/token")


class _FakeConnector:
    instances: list = []

    def __init__(self):
        self.dbx = _FakeDbx()
        _FakeConnector.instances.append(self)


@pytest.fixture
def dropbox(monkeypatch, caller):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "t")
    _FakeConnector.instances.clear()
    import cloud_storage.dropbox_connector as dc

    monkeypatch.setattr(dc, "DropboxConnector", _FakeConnector)
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: SimpleNamespace(mc_dropbox_folder_id="/Clients/Acme/IBX"),
    )
    return _FakeConnector


def test_push_without_dropbox_credentials_is_a_named_error(pt, caller):
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", base64.b64encode(b"x").decode())
    assert "DROPBOX_REFRESH_TOKEN" in out["error"]


def test_push_refuses_a_filename_with_folders(pt, dropbox):
    assert "bare file name" in pt.push_to_dropbox("ibx-5153", "../x.pptx")["error"]


def test_push_uploads_decoded_bytes_into_the_spine_dir_by_default(server, pt, dropbox, monkeypatch):
    got = {}

    def fake_push(connector, folder_id, local_path, dest_name=None, overwrite=False,
                  dest_path=None):
        got.update(folder_id=folder_id, name=Path(local_path).name,
                   data=Path(local_path).read_bytes(), dest_name=dest_name,
                   overwrite=overwrite, dest_path=dest_path)
        return {"dropbox_path": f"/Clients/Acme/IBX/{dest_name}", "name": dest_name,
                "size": 4, "overwrote": False}

    monkeypatch.setattr(server._engine_project_sources, "push_to_dropbox", fake_push)
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", base64.b64encode(b"DECK").decode())
    assert got == {"folder_id": "/Clients/Acme/IBX", "name": "deck.pptx", "data": b"DECK",
                   "dest_name": "03 Assets/06 Spine/deck.pptx", "overwrite": False,
                   "dest_path": None}
    assert out["dropbox_path"].endswith("03 Assets/06 Spine/deck.pptx")


def test_push_with_dest_path_uses_the_bare_name(server, pt, dropbox, monkeypatch):
    got = {}
    monkeypatch.setattr(server._engine_project_sources, "push_to_dropbox",
                        lambda c, f, lp, dest_name=None, overwrite=False, dest_path=None:
                        got.update(dest_name=dest_name, dest_path=dest_path) or
                        {"dropbox_path": "x", "name": dest_name, "size": 1, "overwrote": False})
    pt.push_to_dropbox("ibx-5153", "r.docx", base64.b64encode(b"R").decode(),
                       dest_path="/Clients/Acme/01 Client Assets")
    assert got == {"dest_name": "r.docx", "dest_path": "/Clients/Acme/01 Client Assets"}


def test_push_rejects_invalid_base64(pt, dropbox):
    assert "not valid base64" in pt.push_to_dropbox("ibx-5153", "d.pdf", "@@@")["error"]


def test_push_without_content_returns_a_no_clobber_upload_link(pt, dropbox):
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx")
    assert out["upload_url"] == "https://up.invalid/token"
    assert out["dropbox_path"] == "/Clients/Acme/IBX/03 Assets/06 Spine/deck.pptx"
    commit = dropbox.instances[-1].dbx.commits[-1]
    assert commit.path == out["dropbox_path"]
    assert commit.mode.is_add() and commit.autorename is False
    assert "curl" in out["how"]


def test_push_upload_link_honours_overwrite(pt, dropbox):
    pt.push_to_dropbox("ibx-5153", "deck.pptx", overwrite=True)
    assert dropbox.instances[-1].dbx.commits[-1].mode.is_overwrite()


def test_push_needs_a_folder(pt, dropbox, monkeypatch):
    monkeypatch.setattr("cp_engine.asset_ingest.resolve_project_folders_by_id",
                        lambda client, pid: SimpleNamespace(mc_dropbox_folder_id=None))
    assert "no Dropbox folder" in pt.push_to_dropbox("ibx-5153", "d.pdf")["error"]


# ── preflight ─────────────────────────────────────────────────────────


def _spine_row(eid, framing):
    from cp_engine.mc2_db import SPINE_LIST_COLUMNS

    row = {c.strip(): None for c in SPINE_LIST_COLUMNS.split(",")}
    row.update(est_item_id=eid, framing=framing, layer="Note", binding="unbound",
               status="live", serves=[], body="b", archived=False, scope="project",
               project_id="p1", version_label="v1", version_date="2026-09-01")
    return row


class _SpineClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        rows = self.rows if name == "spine_substance" else []

        class _T:
            def select(self, cols):
                assert "*" not in cols
                return self

            def eq(self, c, v): return self
            def in_(self, c, v): return self
            def order(self, *a, **k): return self

            def execute(self):
                return SimpleNamespace(data=[dict(r) for r in rows])
        return _T()


def test_preflight_reads_spine_framings_under_the_callers_client(server, pt, caller, monkeypatch):
    """#332, ported: spine titles are `framing` (rows carry no `title`), read
    through the REAL `list_spine` on the caller's client."""
    rows = [_spine_row("_authored/brief", "Creative brief — 2027 brand video set"),
            _spine_row("_authored/deck", "Launch deck :30 and :15 cutdowns")]
    monkeypatch.setattr(server, "user_client", lambda: _SpineClient(rows))
    monkeypatch.setattr("cp_engine.project_sources.list_sources", lambda *a, **k: [])
    import cp_engine.preflight as pf

    captured = {}
    real = pf.run_preflight
    monkeypatch.setattr(pf, "run_preflight",
                        lambda *a, **kw: captured.update(kw) or real(*a, **kw))
    out = pt.preflight("sap-5198", "rfp")
    assert captured["spine_titles"] == [r["framing"] for r in rows]
    assert "spine (2 elements)" in out["sources_read"]


def test_preflight_without_the_tree_says_so(pt, caller):
    """No TENANT_REPO here: the verdict must not pass off an unread cp.md as
    an unauthored one."""
    out = pt.preflight("sap-5198", "rfp")
    assert any("tenant tree unavailable" in w for w in out["warnings"]), out


def test_preflight_reads_cp_md_and_sprints_off_the_tree(server, pt, caller, monkeypatch, tmp_path):
    proj = tmp_path / "1p" / "sap" / "sap-5198-ad-videos"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text("# cp\n", encoding="utf-8")
    (tmp_path / "sprints" / "2026-W40").mkdir(parents=True)
    (tmp_path / "sprints" / "2026-W40" / "sap-5198-ad-videos.md").write_text(
        "### Inbound\n- six :30 spots\n", encoding="utf-8")
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    monkeypatch.setattr(server, "tree_root", lambda: tmp_path)
    monkeypatch.setattr("cp_engine.spine.find_spine_dir", lambda root, code: proj)
    import cp_engine.preflight as pf

    monkeypatch.setattr(pf, "read_mc2_titles", lambda *a: ([], []))
    captured = {}
    real = pf.run_preflight
    monkeypatch.setattr(pf, "run_preflight",
                        lambda *a, **kw: captured.update(kw) or real(*a, **kw))
    out = pt.preflight("sap-5198", "rfp")
    assert captured["cp_md_text"] == "# cp\n"
    assert [label for label, _ in captured["sprint_texts"]] == ["sprint 2026-W40"]
    assert "warnings" not in out


def test_preflight_carries_an_mc2_failure_as_a_warning(server, pt, caller, monkeypatch):
    def boom(client, code):
        raise RuntimeError("MC-2 unreachable")

    monkeypatch.setattr(server, "resolve_project_id", boom)
    out = pt.preflight("sap-5198", "rfp")
    assert "error" not in out
    assert any("MC-2 unreachable" in w for w in out["warnings"])


def test_preflight_rejects_an_unknown_kind(pt, caller):
    assert pt.preflight("sap-5198", "poem")["expected"] == ["rfp", "sow", "brief", "estimate"]


# ── vendor registry ───────────────────────────────────────────────────


class _VendorClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        rows = list(self.tables.get(name, []))
        outer = self

        class _Q:
            def select(self, cols):
                assert "*" not in cols
                return self

            def eq(self, c, v):
                nonlocal rows
                rows = [r for r in rows if r.get(c) == v]
                return self

            def in_(self, c, vals):
                nonlocal rows
                rows = [r for r in rows if r.get(c) in vals]
                return self

            def order(self, c):
                rows.sort(key=lambda r: r.get(c) or "")
                return self

            def execute(self):
                return SimpleNamespace(data=rows)
        assert outer
        return _Q()


VENDORS = [
    {"id": "v1", "name": "Bravo Films", "status": "active", "city": "LA",
     "capability_tags": ["Live action", "animation"], "contact_email": "hi@bravo.example",
     "email_confidence": "confirmed", "watch_outs": "Q4 feature in production"},
    {"id": "v2", "name": "Alpha Post", "status": "active", "city": "NYC",
     "capability_tags": ["edit"], "contact_email": None, "email_confidence": "unresearched"},
    {"id": "v3", "name": "Gone Co", "status": "archived", "capability_tags": ["animation"]},
]


def test_list_vendors_filters_by_capability_and_status(server, pt, caller, monkeypatch):
    monkeypatch.setattr(server, "user_client", lambda: _VendorClient({"vendors": VENDORS}))
    out = pt.list_vendors()
    assert [v["name"] for v in out["vendors"]] == ["Alpha Post", "Bravo Films"]
    assert [v["name"] for v in pt.list_vendors(capability="ANIM")["vendors"]] == ["Bravo Films"]
    assert pt.list_vendors(capability="anim", include_archived=True)["count"] == 2


def test_list_vendors_is_team_gated(server, pt, caller, monkeypatch):
    """The table's policy is `USING (true)` for any authenticated account; a
    DCR-registered stranger must not get partner contact data from here."""
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access denied"))
    out = pt.list_vendors()
    assert "denied" in out["error"] and "vendors" not in out


def test_list_rfp_respondents_renders_through_the_engine(server, pt, caller, monkeypatch):
    monkeypatch.setattr(server, "user_client", lambda: _VendorClient({
        "vendors": VENDORS,
        "rfp_respondents": [
            {"project_id": "p1", "vendor_name": "Bravo Films", "status": "sent", "vendor_id": "v1"},
            {"project_id": "p1", "vendor_name": "Alpha Post", "status": "not_sent", "vendor_id": "v2"},
            {"project_id": "other", "vendor_name": "Gone Co", "status": "sent", "vendor_id": "v3"},
        ],
    }))
    out = pt.list_rfp_respondents("sap-5198")
    assert out["counts"] == {"sent": 1, "not_sent": 1}
    by_name = {r["vendor_name"]: r for r in out["respondents"]}
    assert by_name["Bravo Films"]["watch_outs"] == "Q4 feature in production"
    assert "not researched" in by_name["Alpha Post"]["contact"]
    assert "No confirmed address" in out["rendered"]
