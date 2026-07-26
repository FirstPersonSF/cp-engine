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


def test_version_number_parses_drifted_labels():
    # DB labels drift by hand-edit (the sap-5171 header incident): leading int
    # after an optional case-insensitive "v" parses; only truly unparseable
    # labels rank -1 (and -1 LOSES to any parseable label; date tie-breaks).
    assert version_number("V2") == 2
    assert version_number("2") == 2
    assert version_number("v2.1") == 2
    assert version_number("v2 (note)") == 2
    assert version_number(" v3") == 3
    assert version_number("vNaN") == -1
    assert version_number("synthesis") == -1


def test_pull_columns_carry_version_date_for_tie_break():
    # pull_spine must tie-break identically to list_spine: equal/unparseable
    # version numbers fall back to version_date, which requires the column in
    # the pull shape (it was missing — fetch-order decided the tie).
    from cp_engine import mc2_db
    assert "version_date" in mc2_db.SPINE_PULL_COLUMNS
    assert "project_id" in mc2_db.SPINE_PULL_COLUMNS
    assert "project_id" in mc2_db.SPINE_LIST_COLUMNS


def test_pull_spine_equal_version_numbers_tie_break_on_date(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    older = _row("e1", "older", "v2", "2026-07-01", body="old")
    newer = _row("e1", "newer", "v2", "2026-07-10", body="new")
    for rows in ([older, newer], [newer, older]):
        out = ps.pull_spine(_client([dict(r) for r in rows]), "pid", "e1")
        assert out["body"] == "new"


# ---- account-scope identity: est_item_id is unique only PER PROJECT ----

def _acct_row(eid, framing, label, date, project_id, body="b"):
    return {**_row(eid, framing, label, date, scope="account", body=body),
            "project_id": project_id, "company_id": "co1"}


def test_two_account_elements_same_slug_different_origin_both_listed(monkeypatch):
    # Two sibling projects each promoted `_authored/janet-dossier`: DISTINCT
    # elements sharing an est_item_id slug — the dedup must NOT collapse them.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_acct_row("_authored/janet-dossier", "Janet (from p1)", "v1",
                      "2026-07-01", "p1"),
            _acct_row("_authored/janet-dossier", "Janet (from p2)", "v3",
                      "2026-07-02", "p2")]
    out = ps.list_spine(_client(rows), "pid", "co1")
    assert len(out) == 2
    assert {r["framing"] for r in out} == {"Janet (from p1)", "Janet (from p2)"}


def test_two_versions_of_same_account_element_deduped(monkeypatch):
    # Same origin project + same est_item_id = one element: double-live version
    # rows still collapse to the latest.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_acct_row("_authored/janet-dossier", "Janet v1", "v1",
                      "2026-07-01", "p1"),
            _acct_row("_authored/janet-dossier", "Janet v2", "v2",
                      "2026-07-02", "p1")]
    out = ps.list_spine(_client(rows), "pid", "co1")
    assert [r["version_label"] for r in out] == ["v2"]


def test_pull_spine_same_slug_distinct_account_elements_is_ambiguous(monkeypatch):
    # An exact est_item_id key matching two DISTINCT account elements is a
    # genuine ambiguity — report it, never first-fetched-wins.
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    rows = [_acct_row("_authored/janet-dossier", "Janet (from p1)", "v1",
                      "2026-07-01", "p1"),
            _acct_row("_authored/janet-dossier", "Janet (from p2)", "v3",
                      "2026-07-02", "p2")]
    out = ps.pull_spine(_client(rows), "pid", "_authored/janet-dossier", "co1")
    assert "ambiguous" in (out.get("error") or "")


def test_dedup_logs_when_it_drops_a_row(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="cp_engine.project_sources"):
        out = ps._one_live_per_element([dict(_V6), dict(_V7)])
    assert len(out) == 1
    assert any("multiple live version rows" in r.message for r in caplog.records)


# ---- write path (#113 review): resolve_element_versions must agree with the
# read path while an element is double-live, and must hand downstream
# carry-forward (spine_authoring.build_version_rows, which bases the new
# version on the FIRST live row) the CANONICAL live row first.

def _vrow(eid, framing, label, date, status, body="b"):
    r = _row(eid, framing, label, date, body=body)
    r["status"] = status
    r["id"] = f"proj/{eid}/{label}"
    return r


def test_resolve_versions_stale_framing_no_longer_resolves():
    rows = [_vrow("e94d0a03", "Interview with Paul Wu", "v6", "2026-07-09", "live"),
            _vrow("e94d0a03", "1:1 Stakeholder Interviews", "v7", "2026-07-13", "live")]
    eid, _ = ps.resolve_element_versions(
        _client(rows), "pid", "Paul Wu", columns="c")
    assert eid is None  # matches the read path's no-match (dedup applied)


def test_resolve_versions_orders_newest_first_canonical_live_leads():
    stale = _vrow("e94d0a03", "Interview with Paul Wu", "v6", "2026-07-09",
                  "live", body="stale")
    current = _vrow("e94d0a03", "1:1 Stakeholder Interviews", "v7",
                    "2026-07-13", "live", body="current")
    old = _vrow("e94d0a03", "old", "v5", "2026-07-01", "superseded")
    for fetch_order in ([stale, current, old], [old, stale, current],
                        [current, old, stale]):
        eid, versions = ps.resolve_element_versions(
            _client([dict(r) for r in fetch_order]), "pid", "e94d0a03",
            columns="c")
        assert eid == "e94d0a03"
        assert [v["version_label"] for v in versions] == ["v7", "v6", "v5"]
        # build_version_rows' base pick: first row with status == "live".
        base = next(v for v in versions if v["status"] == "live")
        assert base["body"] == "current"
