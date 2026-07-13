# tests/test_spine_list_filters.py — #67 filters + #68 staleness columns
import cp_engine.project_sources as ps


def _client(rows):
    class _T:
        def select(self, c): self._c = c; return self
        def eq(self, c, v): return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": rows})()
    class _C:
        def table(self, n): return _T()
    return _C()


def _row(eid, layer="Note", binding="live", scope=None, important=False,
         version_label="v1", version_date="2026-07-01"):
    return {"est_item_id": eid, "framing": eid, "layer": layer,
            "binding": binding, "status": "live", "serves": [], "body": "b",
            "important": important, "note": None, "scope": scope,
            "version_label": version_label, "version_date": version_date}


def _list(rows, **kw):
    return ps.list_spine(_client(rows), "pid", **kw)


def test_no_filters_returns_everything(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = _list([_row("a"), _row("b", layer="Decision")])
    assert {r["est_item_id"] for r in out} == {"a", "b"}


def test_layer_filter_comma_list_case_insensitive(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("n", layer="Note"), _row("d", layer="Decision"),
            _row("s", layer="Source")]
    out = _list(rows, layer="note, DECISION")
    assert {r["est_item_id"] for r in out} == {"n", "d"}


def test_scope_filter_defaults_null_rows_to_project(monkeypatch):
    # NULL-scope rows are pre-migration project rows: they must pass a
    # scope="project" filter. (Account rows only enter via the company arm of
    # _fetch_scoped, so the account side is exercised through _in_filter.)
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("p", scope=None)]
    assert [r["est_item_id"] for r in _list(rows, scope="project")] == ["p"]
    assert _list(rows, scope="account") == []


def test_in_filter_semantics():
    assert ps._in_filter("Note", None)
    assert ps._in_filter(None, None)
    assert ps._in_filter("Note", "note, decision")
    assert not ps._in_filter("Source", "note,decision")
    assert not ps._in_filter(None, "note")
    assert ps._in_filter("account", " ,")  # blank comma-list = no filter


def test_binding_filter(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("l", binding="live"), _row("u", binding="unbound")]
    assert [r["est_item_id"] for r in _list(rows, binding="unbound")] == ["u"]


def test_filters_compose(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("hit", layer="Note", binding="unbound"),
            _row("miss1", layer="Note", binding="live"),
            _row("miss2", layer="Decision", binding="unbound")]
    out = _list(rows, layer="Note", binding="unbound")
    assert [r["est_item_id"] for r in out] == ["hit"]


def test_null_layer_row_only_passes_without_layer_filter(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("x", layer=None)]
    assert _list(rows)  # no filter → present
    assert _list(rows, layer="Note") == []  # filtered → NULL can't match


def test_rows_carry_version_label_and_date(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = _list([_row("a", version_label="v3", version_date="2026-06-18")])
    assert out[0]["version_label"] == "v3"
    assert out[0]["version_date"] == "2026-06-18"


def test_important_still_sorts_first_after_filtering(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("plain"), _row("starred", important=True)]
    out = _list(rows)
    assert out[0]["est_item_id"] == "starred"
