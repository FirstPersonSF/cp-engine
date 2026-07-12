# tests/test_spine_element_sources.py — #66 add/remove_element_source
import cp_engine.project_sources as ps
import cp_engine.mcp_server as srv


def _asset(aid, title):
    return {"id": aid, "title": title, "source_type": "drive",
            "created_at": "2026-07-01", "file_hash": None}


def _version(vid, status="live", sources=None):
    return {"id": vid, "est_item_id": "_authored/sow", "framing": "SOW",
            "status": status, "archived": False, "scope": None,
            "company_id": None, "project_id": "pid",
            "sources": sources or []}


class _Client:
    """Records spine_substance updates; ignores everything else."""

    def __init__(self):
        self.updates = []

    def table(self, name):
        client = self

        class _T:
            def update(self, patch): self._patch = patch; return self
            def select(self, c): return self
            def eq(self, c, v):
                if hasattr(self, "_patch"):
                    client.updates.append((v, self._patch))
                return self
            def order(self, *a, **k): return self
            def execute(self): return type("R", (), {"data": []})()
        return _T()


def _run(versions, assets, title, *, add=True, monkeypatch=None):
    monkeypatch.setattr(
        ps, "resolve_element_versions",
        lambda client, pid, key, *, columns, company_id=None:
        ("_authored/sow", versions))
    monkeypatch.setattr(ps, "list_sources", lambda c, p, cid: assets)
    client = _Client()
    out = ps.modify_element_sources(client, "pid", "sow", title, add=add)
    return out, client


def test_add_attaches_typed_link_to_every_version(monkeypatch):
    versions = [_version("v1", status="superseded"), _version("v2")]
    out, client = _run(versions, [_asset("a1", "SOW-5174-final-sow.md")],
                       "SOW-5174-final-sow.md", monkeypatch=monkeypatch)
    link = {"type": "rag_asset", "id": "a1", "title": "SOW-5174-final-sow.md"}
    assert out["attached"] is True
    assert out["source"] == link
    assert out["sources"] == [link]
    assert [u[0] for u in client.updates] == ["v1", "v2"]
    assert all(u[1] == {"sources": [link]} for u in client.updates)


def test_add_substring_resolves_unique_source(monkeypatch):
    out, _ = _run([_version("v1")], [_asset("a1", "SOW-5174-final-sow.md"),
                                     _asset("a2", "Kickoff deck")],
                  "final-sow", monkeypatch=monkeypatch)
    assert out["source"]["id"] == "a1"


def test_add_already_attached_is_noop(monkeypatch):
    link = {"type": "rag_asset", "id": "a1", "title": "SOW"}
    out, client = _run([_version("v1", sources=[link])],
                       [_asset("a1", "SOW")], "SOW", monkeypatch=monkeypatch)
    assert out["already"] is True
    assert client.updates == []


def test_add_ambiguous_title_returns_note(monkeypatch):
    out, client = _run([_version("v1")],
                       [_asset("a1", "Interview Wu"), _asset("a2", "Interview Dion")],
                       "Interview", monkeypatch=monkeypatch)
    assert "ambiguous" in out["note"]
    assert client.updates == []


def test_add_exact_title_beats_substring_ambiguity(monkeypatch):
    out, _ = _run([_version("v1")],
                  [_asset("a1", "Brief"), _asset("a2", "Brief v2 notes")],
                  "Brief", monkeypatch=monkeypatch)
    assert out["source"]["id"] == "a1"


def test_add_unknown_source_returns_note(monkeypatch):
    out, client = _run([_version("v1")], [], "Nope", monkeypatch=monkeypatch)
    assert "no active source" in out["note"]
    assert client.updates == []


def test_remove_strips_link_from_every_version(monkeypatch):
    link = {"type": "rag_asset", "id": "a1", "title": "SOW"}
    other = {"type": "rag_asset", "id": "a2", "title": "Other"}
    versions = [_version("v1", status="superseded", sources=[link]),
                _version("v2", sources=[link, other])]
    out, client = _run(versions, [_asset("a1", "SOW")], "SOW",
                       add=False, monkeypatch=monkeypatch)
    assert out["removed"] is True
    assert out["sources"] == [other]
    assert dict(client.updates)["v1"] == {"sources": []}
    assert dict(client.updates)["v2"] == {"sources": [other]}


def test_remove_not_attached_returns_note(monkeypatch):
    out, client = _run([_version("v1")], [_asset("a1", "SOW")], "SOW",
                       add=False, monkeypatch=monkeypatch)
    assert "not attached" in out["note"]
    assert client.updates == []


def test_unresolvable_element_returns_note(monkeypatch):
    monkeypatch.setattr(
        ps, "resolve_element_versions",
        lambda client, pid, key, *, columns, company_id=None: (None, []))
    out = ps.modify_element_sources(_Client(), "pid", "ghost", "SOW", add=True)
    assert "no single live element" in out["note"]


# --- MCP tool boundary -------------------------------------------------------

def test_add_element_source_delegates(monkeypatch):
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))
    captured = {}

    def fake_modify(client, pid, key, title, *, add, company_id=None):
        captured["args"] = (client, pid, key, title, add, company_id)
        return {"est_item_id": key, "attached": True}

    monkeypatch.setattr("cp_engine.project_sources.modify_element_sources",
                        fake_modify)
    out = srv.add_element_source("sap-5174", "_authored/sow", "SOW final")
    assert captured["args"] == (fake_client, "pid", "_authored/sow",
                                "SOW final", True, "cid")
    assert out["attached"] is True


def test_remove_element_source_delegates(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    captured = {}

    def fake_modify(client, pid, key, title, *, add, company_id=None):
        captured["add"] = add
        return {"removed": True}

    monkeypatch.setattr("cp_engine.project_sources.modify_element_sources",
                        fake_modify)
    out = srv.remove_element_source("sap-5174", "sow", "SOW final")
    assert captured["add"] is False
    assert out["removed"] is True


def test_tools_never_raise(monkeypatch):
    def _boom(code):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(srv, "_resolve", _boom)
    assert "error" in srv.add_element_source("x", "k", "t")
    assert "error" in srv.remove_element_source("x", "k", "t")


def test_unresolvable_project_is_error(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    assert "not found" in srv.add_element_source("ghost", "k", "t")["error"]
