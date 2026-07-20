# tests/test_spine_retire_rebind.py — issue #47: retitle / rebind / retire
# verbs + archived rows hidden from every spine read path.
from cp_engine.project_sources import (
    list_spine,
    pull_spine,
    resolve_element_versions,
    resolve_live_element,
)


def _client(rows, captured):
    """Fake supabase client. Each update is captured as (patch, eqs) so a test
    can assert both WHAT was written and WHICH rows it targeted."""
    class _T:
        def __init__(self, n):
            captured.setdefault("table", n)
            self._patch = None
            self._eqs = []
        def select(self, c): captured["select"] = c; return self
        def eq(self, c, v): self._eqs.append((c, v)); return self
        def order(self, *a, **k): return self
        def update(self, d): self._patch = d; return self
        def delete(self): self._deleting = True; return self
        def execute(self):
            if getattr(self, "_deleting", False):
                captured.setdefault("deletes", []).append((self.__dict__.get, list(self._eqs)))
                return type("R", (), {"data": []})()
            if self._patch is not None:
                captured.setdefault("updates", []).append((self._patch, list(self._eqs)))
            return type("R", (), {"data": rows})()
    class _C:
        def table(self, n): return _T(n)
    return _C()


def _row(eid, *, status="live", archived=False, framing=None):
    return {"id": f"p/{eid}/{status}", "est_item_id": eid,
            "framing": framing or eid, "layer": "Note", "binding": "unbound",
            "status": status, "serves": [], "body": "b",
            "important": False, "note": None, "archived": archived}


# ── archived rows are invisible to every read path ──────────────────────────

def test_list_spine_hides_archived_rows():
    captured = {}
    rows = [_row("keep"), _row("retired", archived=True)]
    out = list_spine(_client(rows, captured), "pid")
    assert [r["est_item_id"] for r in out] == ["keep"]
    assert "archived" in captured["select"]


def test_list_spine_treats_null_archived_as_unarchived():
    captured = {}
    row = _row("legacy")
    row["archived"] = None          # pre-column rows carry NULL, not False
    out = list_spine(_client([row], captured), "pid")
    assert [r["est_item_id"] for r in out] == ["legacy"]


def test_pull_spine_archived_element_is_a_miss():
    captured = {}
    rows = [_row("retired", archived=True)]
    el = pull_spine(_client(rows, captured), "pid", "retired")
    assert "error" in el and el["body"] == ""


def test_resolve_live_element_skips_archived():
    captured = {}
    rows = [_row("retired", archived=True)]
    assert resolve_live_element(_client(rows, captured), "pid", "retired") is None


def test_resolve_element_versions_wont_match_archived_live_row():
    captured = {}
    rows = [_row("retired", archived=True), _row("retired", status="superseded", archived=True)]
    eid, versions = resolve_element_versions(
        _client(rows, captured), "pid", "retired", columns="id, est_item_id"
    )
    assert eid is None and versions == []


# ── set_spine_element: retitle (framing) + rebind (serves) ──────────────────

def _patch_resolve(monkeypatch, rows, captured):
    client = _client(rows, captured)
    monkeypatch.setattr("cp_engine.mcp_server._resolve",
                        lambda code: (client, "pid", "cid"))


def _element_updates(captured):
    """Updates targeted at the whole element (project_id + est_item_id)."""
    return [(p, eqs) for p, eqs in captured.get("updates", [])
            if ("project_id", "pid") in eqs]


def test_set_spine_element_framing_retitles_all_versions(monkeypatch):
    captured = {}
    _patch_resolve(monkeypatch, [_row("_authored/x", framing="old title")], captured)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", framing="new title")
    assert res["framing"] == "new title"
    [(patch, eqs)] = _element_updates(captured)
    assert patch["framing"] == "new title"
    assert ("est_item_id", "_authored/x") in eqs      # every version, one element
    assert not any("status" in dict(eqs) for _, eqs in _element_updates(captured))


def test_set_spine_element_serves_rebinds_and_derives_binding(monkeypatch):
    captured = {}
    _patch_resolve(monkeypatch, [_row("_authored/x")], captured)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", serves=["work-item-1"])
    assert res["serves"] == ["work-item-1"]
    assert res["binding"] == "live"
    [(patch, _)] = _element_updates(captured)
    assert patch["serves"] == ["work-item-1"]
    assert patch["binding"] == "live"


def test_set_spine_element_empty_serves_unbinds(monkeypatch):
    captured = {}
    _patch_resolve(monkeypatch, [_row("_authored/x")], captured)
    import cp_engine.mcp_server as m
    res = m.set_spine_element("p", "_authored/x", serves=[])
    assert res["binding"] == "unbound"
    [(patch, _)] = _element_updates(captured)
    assert patch["serves"] == [] and patch["binding"] == "unbound"


def test_set_spine_element_framing_and_layer_share_one_update(monkeypatch):
    captured = {}
    _patch_resolve(monkeypatch, [_row("_authored/x")], captured)
    import cp_engine.mcp_server as m
    m.set_spine_element("p", "_authored/x", framing="t", layer="synthesis")
    assert len(_element_updates(captured)) == 1      # one write for element-level facts


# ── retire_spine_element ─────────────────────────────────────────────────────

def test_retire_archives_all_versions_then_supersedes_live(monkeypatch):
    captured = {}
    _patch_resolve(monkeypatch, [_row("_authored/dupe")], captured)
    import cp_engine.mcp_server as m
    res = m.retire_spine_element("p", "_authored/dupe")
    assert res == {"est_item_id": "_authored/dupe", "retired": True,
                   "edges_removed": 0}
    # retire cascades to the element's edges (#96): two deletes on
    # spine_relations, one per direction (from_item_id + to_item_id).
    assert len(captured.get("deletes", [])) == 2
    updates = captured["updates"]
    archive = [(p, eqs) for p, eqs in updates if p == {"archived": True}]
    demote = [(p, eqs) for p, eqs in updates if p == {"status": "superseded"}]
    assert len(archive) == 1 and len(demote) == 1
    # archive targets EVERY version; demote targets only the live row(s)
    assert ("status", "live") not in archive[0][1]
    assert ("status", "live") in demote[0][1]
    # archive happens FIRST so a failure in between leaves the element hidden
    assert updates.index(archive[0]) < updates.index(demote[0])


def test_retire_no_match_returns_structured_note(monkeypatch):
    captured = {}
    _patch_resolve(monkeypatch, [], captured)
    import cp_engine.mcp_server as m
    res = m.retire_spine_element("p", "missing")
    assert "note" in res and "retired" not in res


def test_retire_unresolvable_project_returns_error(monkeypatch):
    monkeypatch.setattr("cp_engine.mcp_server._resolve", lambda code: None)
    import cp_engine.mcp_server as m
    res = m.retire_spine_element("nope", "x")
    assert "error" in res
