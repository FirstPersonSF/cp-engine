# tests/test_spine_element_provenance.py — #104 add/remove_element_provenance
# (attach ANOTHER spine element as provenance; survives the source's retirement).
import cp_engine.project_sources as ps
import cp_engine.mcp_server as srv


def _version(vid, status="live", sources=None, eid="_authored/synthesis"):
    return {"id": vid, "est_item_id": eid, "framing": "Synthesis card",
            "status": status, "archived": False, "scope": None,
            "company_id": None, "project_id": "pid",
            "sources": sources or []}


def _src_row(eid, framing, archived=False):
    return {"est_item_id": eid, "framing": framing, "archived": archived,
            "status": "superseded" if archived else "live", "scope": None,
            "project_id": "pid"}


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


def _run(versions, src_row, *, add=True, monkeypatch):
    monkeypatch.setattr(
        ps, "resolve_element_versions",
        lambda client, pid, key, *, columns, company_id=None:
        ("_authored/synthesis", versions))
    monkeypatch.setattr(ps, "_resolve_source_element",
                        lambda client, pid, key, cid: src_row)
    client = _Client()
    out = ps.modify_element_provenance(client, "pid", "synthesis",
                                       "email-mehul", add=add)
    return out, client


def test_attach_retired_element_survives(monkeypatch):
    """The headline case: fold a RETIRED raw email into a live synthesis card.
    The link carries retired:true and is written to every version."""
    versions = [_version("v1", status="superseded"), _version("v2")]
    src = _src_row("_authored/email-mehul-6-18", "Email from Mehul", archived=True)
    out, client = _run(versions, src, monkeypatch=monkeypatch)
    link = {"type": "spine_element", "id": "_authored/email-mehul-6-18",
            "title": "Email from Mehul", "retired": True}
    assert out["attached"] is True
    assert out["source"] == link
    assert out["sources"] == [link]
    assert [u[0] for u in client.updates] == ["v1", "v2"]


def test_attach_live_element_marks_not_retired(monkeypatch):
    src = _src_row("_authored/note-x", "A live note", archived=False)
    out, _ = _run([_version("v1")], src, monkeypatch=monkeypatch)
    assert out["source"]["retired"] is False


def test_element_source_coexists_with_rag_asset_same_id(monkeypatch):
    """(type, id) keying: a spine_element link with the same id as an existing
    rag_asset must NOT be treated as already-attached — both survive."""
    rag = {"type": "rag_asset", "id": "shared", "title": "A doc"}
    src = _src_row("shared", "An element that shares an id")
    out, client = _run([_version("v1", sources=[rag])], src,
                       monkeypatch=monkeypatch)
    assert out["attached"] is True
    written = dict(client.updates)["v1"]["sources"]
    assert rag in written
    assert {"type": "spine_element", "id": "shared",
            "title": "An element that shares an id", "retired": False} in written


def test_attach_already_present_is_noop(monkeypatch):
    link = {"type": "spine_element", "id": "_authored/email-mehul-6-18",
            "title": "Email from Mehul", "retired": True}
    src = _src_row("_authored/email-mehul-6-18", "Email from Mehul", archived=True)
    out, client = _run([_version("v1", sources=[link])], src,
                       monkeypatch=monkeypatch)
    assert out["already"] is True
    assert client.updates == []


def test_self_provenance_rejected(monkeypatch):
    src = _src_row("_authored/synthesis", "Synthesis card")  # same eid as target
    out, client = _run([_version("v1")], src, monkeypatch=monkeypatch)
    assert "own provenance" in out["note"]
    assert client.updates == []


def test_unknown_source_element_returns_note(monkeypatch):
    out, client = _run([_version("v1")], None, monkeypatch=monkeypatch)
    assert "no single element matching source" in out["note"]
    assert client.updates == []


def test_unresolvable_target_returns_note(monkeypatch):
    monkeypatch.setattr(
        ps, "resolve_element_versions",
        lambda client, pid, key, *, columns, company_id=None: (None, []))
    out = ps.modify_element_provenance(_Client(), "pid", "ghost",
                                       "email-mehul", add=True)
    assert "no single live element" in out["note"]


def test_remove_strips_element_link(monkeypatch):
    link = {"type": "spine_element", "id": "_authored/email-mehul-6-18",
            "title": "Email from Mehul", "retired": True}
    other = {"type": "rag_asset", "id": "a2", "title": "Other"}
    versions = [_version("v1", status="superseded", sources=[link, other]),
                _version("v2", status="live", sources=[link, other])]
    src = _src_row("_authored/email-mehul-6-18", "Email from Mehul", archived=True)
    out, client = _run(versions, src, add=False, monkeypatch=monkeypatch)
    assert out["removed"] is True
    assert out["sources"] == [other]


# --- MCP tool boundary -------------------------------------------------------
# The add/remove_element_provenance TOOLS moved to the hosted MCP server (#143),
# so the stdio-wrapper delegation tests are gone with them. The implementation
# (`modify_element_provenance` + `_resolve_source_element`) stays in
# project_sources and keeps its own tests above.


def test_stdio_no_longer_registers_the_provenance_tools():
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert "add_element_provenance" not in names
    assert "remove_element_provenance" not in names
