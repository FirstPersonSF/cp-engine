# tests/test_spine_double_live.py — #113: one element must produce exactly ONE
# live row in every spine read, even when the DATA carries two status='live'
# version rows for the same est_item_id (the sap-5174 e94d0a03 shape: authored
# v7 live + distilled v6 re-flipped live by the substance mirror).
import cp_engine.project_sources as ps
from cp_engine.substance import version_number


def _client(rows):
    class _T:
        def select(self, c): return self
        def eq(self, c, v): return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": rows})()
    class _C:
        def table(self, n): return _T()
    return _C()


def _row(eid, framing, version_label, version_date, scope=None, body="b"):
    return {"est_item_id": eid, "framing": framing, "layer": "Activity",
            "binding": "live", "status": "live", "serves": [], "sources": [],
            "body": body, "important": False, "note": None, "scope": scope,
            "version_label": version_label, "version_date": version_date}


# The live failing shape (sap-5174, element e94d0a03): two live version rows.
_V6 = _row("e94d0a03", "Interview with Paul Wu", "v6", "2026-07-09",
           body="stale v6 body")
_V7 = _row("e94d0a03", "1:1 Stakeholder Interviews", "v7", "2026-07-13",
           body="current v7 body")


def test_version_number_parses_and_tolerates_junk():
    assert version_number("v7") == 7
    assert version_number("v10") == 10
    assert version_number(None) == -1
    assert version_number("") == -1
    assert version_number("synthesis") == -1
    assert version_number("vNaN") == -1


def test_list_spine_emits_one_row_per_element_latest_wins(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = ps.list_spine(_client([dict(_V6), dict(_V7)]), "pid")
    assert len(out) == 1
    assert out[0]["framing"] == "1:1 Stakeholder Interviews"
    assert out[0]["version_label"] == "v7"


def test_list_spine_latest_wins_regardless_of_fetch_order(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = ps.list_spine(_client([dict(_V7), dict(_V6)]), "pid")
    assert [r["version_label"] for r in out] == ["v7"]


def test_list_spine_v10_beats_v9_numerically(monkeypatch):
    # String comparison would rank "v9" > "v10"; ordering must be numeric.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("e1", "nine", "v9", "2026-07-01"),
            _row("e1", "ten", "v10", "2026-07-02")]
    out = ps.list_spine(_client(rows), "pid")
    assert [r["version_label"] for r in out] == ["v10"]


def test_list_spine_distinct_elements_untouched(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_row("e1", "one", "v1", "2026-07-01"),
            _row("e2", "two", "v1", "2026-07-01")]
    out = ps.list_spine(_client(rows), "pid")
    assert {r["est_item_id"] for r in out} == {"e1", "e2"}


def test_dedupe_same_id_different_scope_not_merged():
    # A project-scoped and an account-scoped row happening to share an
    # est_item_id are DIFFERENT arms of the scope ladder — never collapse them.
    # (Exercised on the helper: _fetch_scoped delivers the two arms separately.)
    rows = [_row("e1", "mine", "v1", "2026-07-01", scope="project"),
            _row("e1", "shared", "v2", "2026-07-02", scope="account")]
    assert len(ps._one_live_per_element(rows)) == 2


def test_pull_spine_resolves_to_latest_live_body(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    result = ps.pull_spine(_client([dict(_V6), dict(_V7)]), "pid", "e94d0a03")
    assert result.get("error") is None
    assert result["version_label"] == "v7"
    assert result["body"] == "current v7 body"


def test_pull_spine_stale_duplicate_framing_no_longer_resolves(monkeypatch):
    # Before dedup, the stale v6 row's framing ("Interview with Paul Wu") was
    # still title-matchable and returned the superseded body. After dedup only
    # the current framing resolves; the stale one is a clean no-match.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    stale = ps.pull_spine(_client([dict(_V6), dict(_V7)]), "pid", "Paul Wu")
    assert "no spine element" in (stale.get("error") or "")
    result = ps.pull_spine(_client([dict(_V6), dict(_V7)]), "pid",
                           "Stakeholder Interviews")
    assert result.get("error") is None
    assert result["version_label"] == "v7"


def test_resolve_live_element_prefers_latest(monkeypatch):
    v6 = {**_V6, "id": "p/e94d0a03/v6"}
    v7 = {**_V7, "id": "p/e94d0a03/v7"}
    row = ps.resolve_live_element(_client([v6, v7]), "pid", "e94d0a03")
    assert row is not None
    assert row["id"] == "p/e94d0a03/v7"
