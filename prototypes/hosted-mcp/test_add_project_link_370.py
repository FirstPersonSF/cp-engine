"""`add_project_link`: work behind a login recorded as a source (#370).

WHAT BROKE. A Figma schedule (sap-5198), a claude.ai Artifact (ibx-5153) and
a repo (cnc-5239) existed only in chat and Exec Summary prose: nothing in the
source store, nothing in the spine, nothing `seal_sweep` could see.

WHAT THESE PIN.
1. The verb never fetches: it writes a link card to the project's Dropbox
   (the same path `ingest_url` uses) and queues the ingest of exactly that
   file upstream under the caller's identity.
2. Idempotent: the same URL returns the existing card — from the source store,
   or from Dropbox while its ingest is pending — and pushes nothing.
3. A title already used for a different URL is refused; bad input pushes
   nothing.
4. `kind="deliverable"` goes through `create_spine_element` on the
   Deliverables layer, attaching the card when it is already in the store.

Dropbox, MC-2 and mc-2 are all mocked.

    python -m pytest prototypes/hosted-mcp/test_add_project_link_370.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIGMA = "https://www.figma.com/design/tIynwOVzMnLzXRVRMD2LBx/SAP-5198-Schedule-v01"
ARTIFACT = "https://claude.ai/artifact/D1u7eAZgWptFUAf8vq2QZ1"
REPO = "https://github.com/Canonic-OS/canonic-site"
ROOT = "/Clients/Acme/IBX"
LINKS = f"{ROOT}/03 Assets/06 Spine"


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
                google_drive_folder_id=None, mc_dropbox_folder_id=ROOT,
                enable_google_drive=False, enable_dropbox=True)
    base.update(kw)
    return ProjectFolders(**base)


class _Dbx:
    """A Dropbox holding `files` {path: text}; lists one folder level."""

    def __init__(self, files):
        self.files = files
        self.downloads = []

    def files_list_folder(self, folder):
        names = [p.rsplit("/", 1)[1] for p in self.files if p.rsplit("/", 1)[0] == folder]
        if not names and not any(p.startswith(folder + "/") for p in self.files):
            raise RuntimeError("ApiError('path', LookupError('not_found'))")
        return SimpleNamespace(entries=[SimpleNamespace(name=n) for n in names],
                               has_more=False, cursor=None)

    def files_download(self, path):
        self.downloads.append(path)
        return None, SimpleNamespace(content=self.files[path].encode())


@pytest.fixture
def env(server, monkeypatch):
    client = SimpleNamespace(name="caller-client")
    audit = _Audit()
    state = SimpleNamespace(folders=_folders(), pushes=[], hops=[], audit=audit,
                            store=[], created=[], dbx=_Dbx({}),
                            hop_result={"ok": True, "run_id": "ibx-5153/r1",
                                        "status": "running"},
                            create_result=None)
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@firstperson.is")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p1")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "upstream_code", lambda code, scope=None: "ibx-5153-campaign")
    monkeypatch.setattr(server, "_level_for",
                        lambda code: {"code": code, "label": "job", "parent": None,
                                      "indexed": True})
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "t")
    import cloud_storage.dropbox_connector as dc

    monkeypatch.setattr(dc, "DropboxConnector", lambda: SimpleNamespace(dbx=state.dbx))
    monkeypatch.setattr("cp_engine.asset_ingest.resolve_project_folders_by_id",
                        lambda c, pid: state.folders)
    monkeypatch.setattr(server.ported_tools, "_store_link_cards",
                        lambda c, pid: list(state.store))

    def fake_push(connector, folder_id, local_path, dest_name=None, overwrite=False,
                  dest_path=None):
        data = Path(local_path).read_text(encoding="utf-8")
        path = f"{ROOT}/{dest_name}"
        state.pushes.append({"name": Path(local_path).name, "dest_name": dest_name,
                             "data": data, "overwrite": overwrite})
        state.dbx.files[path] = data
        return {"dropbox_path": path, "name": Path(dest_name).name, "size": len(data),
                "overwrote": False}

    monkeypatch.setattr(server._engine_project_sources, "push_to_dropbox", fake_push)
    monkeypatch.setattr(server._engine_project_sources, "dropbox_destination",
                        lambda conn, fid, name, dest_path=None: f"{ROOT}/{name}")

    def fake_hop(code, mc_project_id, file_ids):
        state.hops.append({"code": code, "mc_project_id": mc_project_id,
                           "file_ids": list(file_ids)})
        return dict(state.hop_result)

    monkeypatch.setattr(server, "call_mc2_ingest_assets", fake_hop)

    def fake_create(project_code, framing, body, layer="note", slug=None,
                    important=False, note=None, sources=None, replaces_key=None):
        state.created.append(dict(project_code=project_code, framing=framing, body=body,
                                  layer=layer, slug=slug, note=note, sources=sources))
        if state.create_result is not None:
            return dict(state.create_result)
        return {"row_id": f"ibx-5153-campaign/_authored/{slug}/v1",
                "element_id": f"_authored/{slug}", "layer": "Deliverables"}

    monkeypatch.setattr(server, "create_spine_element", fake_create)
    return state


# ── registration ─────────────────────────────────────────────────────


def test_add_project_link_is_a_registered_audited_strict_main_only_writer(server):
    tools = {t.name: t for t in server.mcp_server._tool_manager.list_tools()}
    assert "add_project_link" in tools
    assert getattr(tools["add_project_link"].fn, "__cp_audit_guaranteed__", False)
    assert tools["add_project_link"].parameters.get("additionalProperties") is False
    assert "add_project_link" in server.MAIN_ONLY_TOOLS
    assert "add_project_link" not in {
        t.name for t in server.read_server._tool_manager.list_tools()}


# ── a new link ───────────────────────────────────────────────────────


def test_a_new_link_is_carded_pushed_and_queued_without_a_fetch(pt, env, monkeypatch):
    from cp_engine import url_source

    def no_fetch(*a, **k):
        raise AssertionError("add_project_link must never fetch the URL")

    monkeypatch.setattr(url_source, "fetch_url", no_fetch)
    out = pt.add_project_link("sap-5198", FIGMA, "SAP-5198 Schedule v01",
                              note="Tara's schedule")
    assert "error" not in out, out
    assert out["existing"] is False and out["provider"] == "figma"
    assert out["title"] == "Link · SAP-5198 Schedule v01.md"
    push = env.pushes[-1]
    assert push["dest_name"] == "03 Assets/06 Spine/Link · SAP-5198 Schedule v01.md"
    assert push["overwrite"] is False
    assert f"CP-LINK-URL: {FIGMA}" in push["data"]
    assert "- Added by: drew@firstperson.is" in push["data"]
    assert "- Note: Tara's schedule" in push["data"]
    assert env.hops == [{"code": "ibx-5153-campaign", "mc_project_id": "p1",
                         "file_ids": [out["dropbox_path"]]}]
    assert out["ingest"]["queued"] is True
    assert out["level"]["code"] == "sap-5198"
    assert "deliverable" not in out and env.created == []
    assert env.audit[-1][0] == "add_project_link" and env.audit[-1][2] == 1


def test_the_card_the_verb_writes_is_one_the_ingest_reads_the_url_from(pt, env, tmp_path):
    """Across the boundary: the bytes pushed, under the name pushed, give
    the ingest the work's URL."""
    from cp_engine.asset_ingest import FileRef, _link_card_url

    out = pt.add_project_link("cnc-5239", REPO, "canonic-site repo")
    push = env.pushes[-1]
    local = tmp_path / push["name"]
    local.write_text(push["data"], encoding="utf-8")
    ref = FileRef(source="dropbox", id="x", name=out["title"], mime_type=None,
                  size=None, modified=None, path=out["dropbox_path"])
    assert _link_card_url(ref, local) == REPO


# ── idempotency ──────────────────────────────────────────────────────


def test_a_url_already_in_the_store_returns_that_card(pt, env):
    env.store = [{"id": "a1", "title": "Link · Schedule.md", "url": FIGMA + "/"}]
    out = pt.add_project_link("sap-5198", FIGMA, "A different title")
    assert out["existing"] is True and out["title"] == "Link · Schedule.md"
    assert out["asset_id"] == "a1" and out["where"] == "source store"
    assert env.pushes == [] and env.hops == []


def test_a_url_whose_ingest_is_pending_returns_the_dropbox_card(pt, env):
    first = pt.add_project_link("sap-5198", FIGMA, "Schedule")
    env.hops.clear()
    again = pt.add_project_link("sap-5198", FIGMA, "Schedule, renamed")
    assert again["existing"] is True and again["title"] == first["title"]
    assert again["where"] == "Dropbox (ingest pending)"
    assert len(env.pushes) == 1  # nothing new written
    # re-queued, so a first queue that never ran is not a dead end
    assert env.hops and env.hops[0]["file_ids"] == [first["dropbox_path"]]


def test_a_title_used_for_a_different_url_is_refused(pt, env):
    pt.add_project_link("ibx-5153", ARTIFACT, "Frame review")
    out = pt.add_project_link("ibx-5153", "https://claude.ai/artifact/OTHER", "Frame review")
    assert "already records" in out["error"] and ARTIFACT in out["error"]
    assert len(env.pushes) == 1


def test_an_unreadable_store_still_dedupes_through_dropbox(pt, env, server, monkeypatch):
    def boom(c, pid):
        raise RuntimeError("PostgREST 500")

    pt.add_project_link("sap-5198", FIGMA, "Schedule")
    monkeypatch.setattr(server.ported_tools, "_store_link_cards", boom)
    out = pt.add_project_link("sap-5198", FIGMA, "Schedule")
    assert out["existing"] is True
    assert any("source-store read failed" in w for w in out["warnings"])


# ── refusals push nothing ────────────────────────────────────────────


@pytest.mark.parametrize("url,title", [
    ("ftp://files.example/x", "T"),
    ("figma.com/design/x", "T"),
    (FIGMA, ""),
    (FIGMA, "   "),
])
def test_bad_input_is_refused_before_any_write(pt, env, url, title):
    out = pt.add_project_link("sap-5198", url, title)
    assert "error" in out
    assert env.pushes == [] and env.hops == []
    assert env.audit[-1][2] == 0


def test_a_bad_kind_is_refused(pt, env):
    out = pt.add_project_link("sap-5198", FIGMA, "T", kind="output")
    assert "kind must be one of" in out["error"] and env.pushes == []


def test_no_dropbox_folder_names_the_mc2_fix(pt, env):
    env.folders = _folders(mc_dropbox_folder_id=None)
    out = pt.add_project_link("ibx-5167", FIGMA, "T")
    assert "Integrations in MC-2" in out["error"] and "project_integrations" in out["error"]
    assert env.pushes == []


def test_an_unknown_project_is_refused(pt, env):
    assert "no project" in pt.add_project_link("ghost", FIGMA, "T")["error"]


def test_add_project_link_is_team_gated(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "no tree access"))
    assert "error" in pt.add_project_link("sap-5198", FIGMA, "T")
    assert env.pushes == []


def test_an_unreachable_ingest_folder_is_not_reported_queued(pt, env):
    env.folders = _folders(asset_ingest_folders=("Client Decks",))
    out = pt.add_project_link("sap-5198", FIGMA, "T")
    assert env.pushes and env.hops == []
    assert out["ingest"]["queued"] is False and "Client Decks" in out["ingest"]["reason"]


# ── kind="deliverable" ───────────────────────────────────────────────


def test_a_deliverable_link_creates_a_deliverables_element(pt, env):
    out = pt.add_project_link("ibx-5153", ARTIFACT, "Derek frame review",
                              kind="deliverable")
    made = env.created[-1]
    assert made["layer"] == "deliverable" and made["framing"] == "Derek frame review"
    assert made["slug"] == "link-derek-frame-review"
    assert ARTIFACT in made["body"] and made["sources"] is None  # not ingested yet
    d = out["deliverable"]
    assert d["created"] is True and d["element_id"] == "_authored/link-derek-frame-review"
    assert "add_element_source" in d["next"] and out["title"] in d["next"]
    assert env.audit[-1][1]["element"] == d["element_id"]


def test_a_deliverable_for_a_card_in_the_store_attaches_it(pt, env):
    env.store = [{"id": "a9", "title": "Link · Derek frame review.md", "url": ARTIFACT}]
    out = pt.add_project_link("ibx-5153", ARTIFACT, "anything", kind="deliverable")
    made = env.created[-1]
    assert made["framing"] == "Derek frame review"  # the card's title, not the new one
    assert made["sources"] == [{"type": "rag_asset", "id": "a9",
                                "title": "Link · Derek frame review.md"}]
    assert out["existing"] is True and "next" not in out["deliverable"]


def test_a_deliverable_that_exists_is_reported_not_errored(pt, env):
    env.create_result = {"error": "an element '_authored/link-x' already exists",
                         "existing_id": "ibx-5153-campaign/_authored/link-x/v1"}
    out = pt.add_project_link("ibx-5153", ARTIFACT, "x", kind="deliverable")
    assert "error" not in out
    assert out["deliverable"]["created"] is False
    assert out["deliverable"]["existing_id"].endswith("/v1")


def test_a_failed_element_insert_is_surfaced(pt, env):
    env.create_result = {"error": "insert failed: 42501"}
    out = pt.add_project_link("ibx-5153", ARTIFACT, "x", kind="deliverable")
    assert out["deliverable"]["created"] is False
    assert "42501" in out["deliverable"]["error"]
    assert out["ingest"]["queued"] is True  # the source half still landed


def test_the_real_create_spine_element_files_it_on_the_deliverables_layer(server):
    """The layer string the verb passes is one the real verb accepts, and
    lands where `seal_sweep` reads deliverables."""
    from cp_engine.seal_sweep import _is_deliverable

    layer, refused = server._checked_layer("deliverable")
    assert refused is None
    row = {"layer": layer, "est_item_id": "_authored/link-x", "version_label": "v1"}
    row["card_kind"] = server._stamp_card_kind(row)
    assert _is_deliverable(row)
