"""`ingest_url`, and `push_to_dropbox` saying what it did NOT do (#350).

WHAT BROKE.
- 08-20: no URL path into the source store. The workaround (fetch → markdown
  → push → a separate ingest) stored four form-gated landing-page abstracts
  as if they were the guides.
- 08-21: `push_to_dropbox` returned a clean success, and `add_element_source`
  then could not resolve the file, because an upload is not an ingest.

WHAT THESE PIN.
1. `ingest_url` fetches, writes the page to the project's Dropbox, and queues
   the ingest of exactly that file UPSTREAM under the caller's identity
   (mc-2 `POST /api/projects/{code}/ingest-assets` with `file_ids`) — this
   server holds no service key, so it never writes `rag_assets` itself.
2. A gated page says so in the RESULT and carries the `status_note` the
   ingest will write; a page the project's ingest cannot reach (folder
   allowlist, Dropbox disabled) is pushed but NOT reported as queued.
3. `push_to_dropbox` says "uploaded — not yet ingested" unless it was asked
   to queue the ingest (`ingest=True`), and then it queues that one file.
4. The hop itself: the caller's token, the right path, a refusal mapped.

Network, Dropbox and mc-2 are all mocked.

    python -m pytest prototypes/hosted-mcp/test_ingest_url_350.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

GATED_HTML = b"""<html><head><title>The 2026 Expense Guide</title></head><body><main>
<h1>The 2026 Expense Management Guide</h1>
<p>Five trends shaping expense management. Download the guide to read more.</p>
<form class="mktoForm"><input type="email" name="Email"><button>Get it</button></form>
<a href="/assets/guide.pdf">PDF</a></main></body></html>"""


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


def _folders(**kw):
    from cp_engine.asset_ingest import ProjectFolders

    base = dict(project_id="p1", company_id="co1", company_kind="client",
                google_drive_folder_id=None, mc_dropbox_folder_id="/Clients/Acme/IBX",
                enable_google_drive=False, enable_dropbox=True)
    base.update(kw)
    return ProjectFolders(**base)


@pytest.fixture
def env(server, monkeypatch):
    """A team member, a Dropbox that records uploads, an mc-2 that records hops."""
    client = SimpleNamespace(name="caller-client")
    audit = _Audit()
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p1")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "upstream_code", lambda code, scope=None: "ibx-5153-campaign")
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "t")
    import cloud_storage.dropbox_connector as dc

    # `in_dropbox`: path -> "file" | "folder"; anything else is not_found.
    in_dropbox: dict[str, str] = {}

    def fake_metadata(path):
        kind = in_dropbox.get(path)
        if kind is None:
            raise RuntimeError("ApiError('path', LookupError('not_found'))")
        return SimpleNamespace(name=path.rsplit("/", 1)[-1],
                               **({"size": 7} if kind == "file" else {}))

    monkeypatch.setattr(dc, "DropboxConnector",
                        lambda: SimpleNamespace(dbx=SimpleNamespace(
                            files_get_temporary_upload_link=lambda commit_info:
                            SimpleNamespace(link="https://up.invalid/t"),
                            files_get_metadata=fake_metadata)))
    state = SimpleNamespace(folders=_folders(), pushes=[], hops=[], audit=audit,
                            in_dropbox=in_dropbox,
                            hop_result={"ok": True, "run_id": "ibx-5153-campaign/r1",
                                        "status": "running"})
    monkeypatch.setattr("cp_engine.asset_ingest.resolve_project_folders_by_id",
                        lambda c, pid: state.folders)

    def fake_push(connector, folder_id, local_path, dest_name=None, overwrite=False,
                  dest_path=None):
        state.pushes.append({"name": Path(local_path).name, "dest_name": dest_name,
                             "data": Path(local_path).read_bytes(),
                             "dest_path": dest_path})
        root = dest_path or "/Clients/Acme/IBX"
        return {"dropbox_path": f"{root}/{dest_name}", "name": Path(dest_name).name,
                "size": 1, "overwrote": False}

    monkeypatch.setattr(server._engine_project_sources, "push_to_dropbox", fake_push)
    monkeypatch.setattr(server._engine_project_sources, "dropbox_destination",
                        lambda conn, fid, name, dest_path=None:
                        f"{dest_path or '/Clients/Acme/IBX'}/{name}")

    def fake_hop(code, mc_project_id, file_ids):
        state.hops.append({"code": code, "mc_project_id": mc_project_id,
                           "file_ids": list(file_ids)})
        return dict(state.hop_result)

    monkeypatch.setattr(server, "call_mc2_ingest_assets", fake_hop)
    return state


def _serve(monkeypatch, content=GATED_HTML, ctype="text/html", final="https://acme.example/guide"):
    from cp_engine import url_source

    def fake_fetch(url, **kw):
        return url_source.Fetched(url=final, content_type=ctype, data=content)

    monkeypatch.setattr(url_source, "fetch_url", fake_fetch)


# ── registration ─────────────────────────────────────────────────────


def test_ingest_url_is_a_registered_audited_strict_main_only_writer(server):
    tools = {t.name: t for t in server.mcp_server._tool_manager.list_tools()}
    assert "ingest_url" in tools
    assert getattr(tools["ingest_url"].fn, "__cp_audit_guaranteed__", False)
    assert tools["ingest_url"].parameters.get("additionalProperties") is False
    assert "ingest_url" in server.MAIN_ONLY_TOOLS
    assert "ingest_url" not in {t.name for t in server.read_server._tool_manager.list_tools()}


# ── ingest_url ───────────────────────────────────────────────────────


def test_a_gated_page_is_pushed_queued_and_flagged(pt, env, monkeypatch):
    _serve(monkeypatch)
    out = pt.ingest_url("ibx-5153", "https://acme.example/guide")
    assert "error" not in out, out
    assert out["gated"] is True
    assert out["status_note"].startswith("form-gated — landing-page abstract")
    assert out["pdf_links"] == ["https://acme.example/assets/guide.pdf"]
    push = env.pushes[-1]
    assert push["name"].endswith(".md") and push["dest_name"].startswith("03 Assets/")
    assert b"CP-INGEST-NOTE: form-gated" in push["data"]
    # the ingest of exactly this file, queued upstream under the caller
    assert env.hops == [{"code": "ibx-5153-campaign", "mc_project_id": "p1",
                         "file_ids": [out["dropbox_path"]]}]
    assert out["ingest"]["queued"] is True and out["ingest"]["run_id"]
    assert out["title"] == push["name"]  # what list_project_sources will show


def test_an_ungated_page_has_no_note(pt, env, monkeypatch):
    body = (b"<html><body><article><h1>Essay</h1><p>" + b"word " * 2000
            + b"</p></article></body></html>")
    _serve(monkeypatch, content=body)
    out = pt.ingest_url("ibx-5153", "https://acme.example/essay")
    assert out["gated"] is False and out.get("status_note") is None
    assert b"CP-INGEST-NOTE" not in env.pushes[-1]["data"]


def test_a_pdf_url_is_pushed_as_the_pdf(pt, env, monkeypatch):
    _serve(monkeypatch, content=b"%PDF-1.7", ctype="application/pdf",
           final="https://acme.example/guide.pdf")
    out = pt.ingest_url("ibx-5153", "https://acme.example/guide.pdf")
    assert env.pushes[-1]["data"] == b"%PDF-1.7" and out["title"].endswith(".pdf")


def test_a_fetch_failure_pushes_and_queues_nothing(pt, env, monkeypatch):
    from cp_engine import url_source

    def boom(url, **kw):
        raise url_source.UrlFetchError("refused: http://10.0.0.1/ resolves to a private address")

    monkeypatch.setattr(url_source, "fetch_url", boom)
    out = pt.ingest_url("ibx-5153", "http://10.0.0.1/")
    assert "private" in out["error"] and env.pushes == [] and env.hops == []


def test_a_folder_the_ingest_cannot_reach_is_not_reported_queued(pt, env, monkeypatch):
    env.folders = _folders(asset_ingest_folders=("Client Decks",))
    _serve(monkeypatch)
    out = pt.ingest_url("ibx-5153", "https://acme.example/guide")
    assert env.pushes and env.hops == []
    assert out["ingest"]["queued"] is False
    assert "Client Decks" in out["ingest"]["reason"]


def test_a_refused_hop_is_reported_not_swallowed(pt, env, monkeypatch):
    env.hop_result = {"ok": False, "reason": "mc-2 refused the caller's token: expired"}
    _serve(monkeypatch)
    out = pt.ingest_url("ibx-5153", "https://acme.example/guide")
    assert out["ingest"]["queued"] is False and "expired" in out["ingest"]["reason"]
    assert out["gated"] is True  # the detection still reaches the caller


def test_ingest_url_is_team_gated(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "no tree access"))
    _serve(monkeypatch)
    assert "error" in pt.ingest_url("ibx-5153", "https://acme.example/guide")
    assert env.pushes == []


# ── push_to_dropbox: an upload is not an ingest ──────────────────────


def test_a_plain_push_says_it_is_not_yet_ingested(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "brief.docx", base64.b64encode(b"B").decode())
    assert out["ingested"] is False
    assert "not yet ingested" in out["next"] and "ingest=True" in out["next"]
    assert env.hops == []


def test_push_with_ingest_queues_that_file(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "brief.docx", base64.b64encode(b"B").decode(),
                             ingest=True)
    assert env.hops == [{"code": "ibx-5153-campaign", "mc_project_id": "p1",
                         "file_ids": [out["dropbox_path"]]}]
    assert out["ingest"]["queued"] is True


def test_an_upload_link_says_the_file_will_not_be_ingested_by_itself(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx")
    assert out["upload_url"] and "not yet ingested" in out["next"]
    assert env.hops == []


def test_push_with_ingest_says_queued(pt, env):
    """#410: `ingested` tells the truth when a run was queued."""
    out = pt.push_to_dropbox("ibx-5153", "brief.docx", base64.b64encode(b"B").decode(),
                             ingest=True)
    assert out["ingested"] == "queued"


# ── push_to_dropbox: the upload-link path can finish with an ingest (#410) ──

DECK = "/Clients/Acme/IBX/03 Assets/06 Spine/deck.pptx"


def test_the_upload_link_names_the_call_that_queues_the_ingest(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx")
    assert "ingest=True" in out["next"] and "no content_base64" in out["next"]


def test_a_second_call_with_ingest_queues_the_uploaded_file(pt, env):
    first = pt.push_to_dropbox("ibx-5153", "deck.pptx", ingest=True)
    assert first["upload_url"] and first["ingested"] is False and env.hops == []
    env.in_dropbox[DECK] = "file"  # the caller POSTed it
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", ingest=True)
    assert "error" not in out, out
    assert "upload_url" not in out
    assert out["dropbox_path"] == DECK
    assert env.hops == [{"code": "ibx-5153-campaign", "mc_project_id": "p1",
                         "file_ids": [DECK]}]
    assert out["ingested"] == "queued" and out["ingest"]["run_id"]
    assert env.pushes == []  # nothing re-uploaded
    assert env.audit[-1][1]["kind"] == "ingest_existing"


def test_ingest_without_a_file_there_says_so_and_queues_nothing(pt, env):
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", ingest=True)
    assert out["ingested"] is False and env.hops == []
    assert out["ingest"]["queued"] is False
    assert "nothing at" in out["ingest"]["reason"] and DECK in out["ingest"]["reason"]
    assert out["upload_url"]


def test_overwrite_with_ingest_returns_the_link_not_a_queue(pt, env):
    env.in_dropbox[DECK] = "file"
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", ingest=True, overwrite=True)
    assert out["upload_url"] and out["ingested"] is False and env.hops == []


def test_ingest_of_an_existing_file_keeps_the_reach_guard(pt, env):
    env.folders = _folders(asset_ingest_folders=("Client Decks",))
    env.in_dropbox[DECK] = "file"
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", ingest=True)
    assert env.hops == [] and out["ingested"] is False
    assert "Client Decks" in out["ingest"]["reason"]


def test_ingest_of_a_folder_is_refused(pt, env):
    env.in_dropbox[DECK] = "folder"
    out = pt.push_to_dropbox("ibx-5153", "deck.pptx", ingest=True)
    assert "is a folder" in out["error"] and env.hops == []


def test_ingest_of_an_existing_file_at_dest_path(pt, env):
    path = "/Clients/Acme/IBX/02 Drafts/brief.docx"
    env.in_dropbox[path] = "file"
    out = pt.push_to_dropbox("ibx-5153", "brief.docx", ingest=True,
                             dest_path="/Clients/Acme/IBX/02 Drafts")
    assert out["ingested"] == "queued"
    assert env.hops[-1]["file_ids"] == [path]


# ── the hop ──────────────────────────────────────────────────────────


def test_the_hop_forwards_the_callers_token_and_the_file(server, monkeypatch):
    import httpx

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json)
        return httpx.Response(202, json={"run_id": "c/r1", "status": "running"})

    monkeypatch.setattr(server, "caller_jwt", lambda: "JWT")
    monkeypatch.setattr(server.httpx, "post", fake_post)
    out = server.call_mc2_ingest_assets("ibx-5153-campaign", "p1", ["/C/a.md"])
    assert out == {"ok": True, "run_id": "c/r1", "status": "running"}
    assert seen["url"].endswith("/api/projects/ibx-5153-campaign/ingest-assets")
    assert seen["headers"] == {"Authorization": "Bearer JWT"}
    assert seen["json"] == {"mc_project_id": "p1", "file_ids": ["/C/a.md"]}


def test_the_hop_maps_a_refusal(server, monkeypatch):
    import httpx

    monkeypatch.setattr(server, "caller_jwt", lambda: "JWT")
    monkeypatch.setattr(server.httpx, "post",
                        lambda *a, **k: httpx.Response(401, json={"detail": "bad token"}))
    out = server.call_mc2_ingest_assets("c", "p1", ["/C/a.md"])
    assert out["ok"] is False and "bad token" in out["reason"]


def test_ingest_url_missing_folder_names_where_to_fix_it(pt, env):
    """#373: same fix-it message as `push_to_dropbox`."""
    env.folders = _folders(mc_dropbox_folder_id=None)
    out = pt.ingest_url("ibx-5153", "https://acme.example/guide")
    assert "Add it under ibx-5153's Integrations in MC-2" in out["error"]
    assert "service=dropbox" in out["error"] and not env.pushes
