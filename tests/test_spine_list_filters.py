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
    out = _list(rows, layer="Note")  # filtered → NULL can't match
    assert [r for r in out if r.get("est_item_id")] == []


# --- layer normalization (singular/plural + substring) ---------------------


def test_layer_filter_singular_matches_plural_label(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("s", layer="Stakeholders"), _row("d", layer="Decisions"),
            _row("n", layer="Note")]
    assert [r["est_item_id"] for r in _list(rows, layer="stakeholder")] == ["s"]
    assert [r["est_item_id"] for r in _list(rows, layer="Decision")] == ["d"]


def test_layer_filter_plural_matches_singular_label(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("d", layer="Decision")]
    assert [r["est_item_id"] for r in _list(rows, layer="Decisions")] == ["d"]


def test_layer_filter_substring_matches_compound_label(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("f", layer="Client feedback"),
            _row("m", layer="Source material"), _row("n", layer="Note")]
    assert [r["est_item_id"] for r in _list(rows, layer="feedback")] == ["f"]
    assert [r["est_item_id"] for r in _list(rows, layer="sources")] == ["m"]


def test_empty_layer_filter_result_carries_hint(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("s", layer="Stakeholders"), _row("d", layer="Decisions"),
            _row("x", layer=None)]
    out = _list(rows, layer="Agreement")
    assert len(out) == 1
    assert "Agreement" in out[0]["note"]
    assert out[0]["hint"] == ["Decisions", "Stakeholders"]  # NULL layer dropped


def test_layer_hint_not_emitted_when_other_filter_empties(monkeypatch):
    # The layer term DID match rows; scope emptied the combination. Blaming
    # the layer filter (with a layer-vocabulary hint) would steer the caller
    # at the wrong filter — this stays a plain [].
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("d", layer="Decisions", scope=None)]  # project-scoped
    assert _list(rows, layer="decision", scope="account") == []


def test_degenerate_layer_term_is_no_match_with_hint(monkeypatch):
    # layer="s" folds to "" — an empty substring would match EVERY label.
    # It must instead match nothing and surface the vocabulary hint.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("d", layer="Decisions"), _row("n", layer="Note")]
    out = _list(rows, layer="s")
    assert [r for r in out if r.get("est_item_id")] == []
    assert out[0]["hint"] == ["Decisions", "Note"]


def test_empty_scope_filter_stays_a_plain_empty_list(monkeypatch):
    # The hint is a LAYER-filter affordance only; other filters keep the
    # original silent-[] contract.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("p", scope=None)]
    assert _list(rows, scope="account") == []


# --- compact mode ----------------------------------------------------------

_COMPACT_KEYS = {"est_item_id", "framing", "layer", "binding", "body_len",
                 "important", "has_note", "scope", "version_label"}


def test_compact_row_shape(monkeypatch):
    # compact never touches the done-map. Record any call with a flag —
    # list_spine swallows exceptions around this fetch, so a raising stub
    # could NOT be loud; the flag (plus the shape assertion, since a fetch
    # implies the 13-key full path) is the real guard.
    calls = []
    monkeypatch.setattr(ps, "fetch_project_done_map",
                        lambda c, p: calls.append(p) or {})
    row = _row("a", layer="Decisions")
    row["note"] = "watch this"
    out = ps.list_spine(_client([row]), "pid", compact=True)
    assert set(out[0]) == _COMPACT_KEYS
    assert out[0]["has_note"] is True
    assert out[0]["body_len"] == 1  # body "b"
    assert calls == []  # done-map never fetched on the compact path


def test_compact_has_note_false_when_no_note(monkeypatch):
    out = ps.list_spine(_client([_row("a")]), "pid", compact=True)
    assert out[0]["has_note"] is False


def test_compact_important_still_sorts_first(monkeypatch):
    rows = [_row("plain"), _row("starred", important=True)]
    out = ps.list_spine(_client(rows), "pid", compact=True)
    assert out[0]["est_item_id"] == "starred"


def test_compact_composes_with_layer_filter(monkeypatch):
    rows = [_row("d", layer="Decisions"), _row("n", layer="Note")]
    out = ps.list_spine(_client(rows), "pid", layer="decision", compact=True)
    assert [r["est_item_id"] for r in out] == ["d"]
    assert set(out[0]) == _COMPACT_KEYS


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
