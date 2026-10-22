# tests/test_spine_supersession_352.py — the engine's `list_spine` and
# `pull_spine` say when an element has been REPLACED (#352 item 1).
#
# WHAT BROKE. On 08-26 a fresh agent drafted ibx-5153 work against a July
# synthesis that a later client redirect had withdrawn. A `supersedes` edge is
# the tenant's way to record "this replaced that", and five live ones exist —
# but neither listing nor pull read them, so the replaced element read exactly
# like current truth. Only `list_spine_relations`, asked explicitly, showed it.
#
# WHAT THESE PIN. An element with an incoming ACTIVE `supersedes` edge from a
# LIVE element carries `superseded_by: {key, framing, date}` (newest wins when
# two replace it); a pull also leads with a one-line `warning`. An active
# `contradicts` edge marks its target `contradicted_by: [...]`. Retired edges,
# dead successors and elements with no edge are untouched.
import pytest

import cp_engine.project_sources as ps
from tests.test_list_spine_absorbed import _Client, _edge, _row


def _client(**kw):
    rows = [_row("_authored/old-frame"), _row("_authored/redirect"),
            _row("_authored/later-redirect"), _row("_authored/plain"),
            _row("_authored/challenger")]
    rows[1]["framing"] = "Janet redirect 08-24"
    rows[2]["framing"] = "Branch-1 record 10-16"
    edges = [
        dict(_edge("_authored/redirect", "_authored/old-frame",
                   kind="supersedes"), created_at="2026-08-25T10:00:00Z",
             note="the redirect withdrew it"),
        dict(_edge("_authored/later-redirect", "_authored/old-frame",
                   kind="supersedes"), created_at="2026-10-16T09:00:00Z"),
        # A retired edge is history, not a signal.
        dict(_edge("_authored/redirect", "_authored/plain", kind="supersedes",
                   status="retired"), created_at="2026-08-25T10:00:00Z"),
        # A successor that is no longer live cannot be "read that first".
        dict(_edge("_authored/gone", "_authored/challenger",
                   kind="supersedes"), created_at="2026-08-01T00:00:00Z"),
        dict(_edge("_authored/challenger", "_authored/plain",
                   kind="contradicts"), created_at="2026-09-22T00:00:00Z",
             note="client margin comments"),
    ]
    edges += kw.get("extra", [])
    return _Client(project_rows=rows, edges=edges)


def _by_id(rows):
    return {r["est_item_id"]: r for r in rows if "est_item_id" in r}


def _list(client, **kw):
    mp = pytest.MonkeyPatch()
    mp.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    try:
        return ps.list_spine(client, "p1", "co", include_absorbed=False, **kw)
    finally:
        mp.undo()


def test_listing_marks_the_replaced_element_with_its_newest_successor():
    for compact in (False, True):
        by_id = _by_id(_list(_client(), compact=compact))
        mark = by_id["_authored/old-frame"]["superseded_by"]
        assert mark == {"key": "_authored/later-redirect",
                        "framing": "Branch-1 record 10-16",
                        "date": "2026-10-16"}
        # Retired edge, dead successor, no edge: no marker.
        assert "superseded_by" not in by_id["_authored/plain"]
        assert "superseded_by" not in by_id["_authored/challenger"]
        assert "superseded_by" not in by_id["_authored/redirect"]


def test_listing_marks_contradicted_targets():
    by_id = _by_id(_list(_client()))
    assert by_id["_authored/plain"]["contradicted_by"] == [
        {"key": "_authored/challenger", "framing": "_authored/challenger",
         "date": "2026-09-22"}]
    assert "contradicted_by" not in by_id["_authored/challenger"]


def test_listing_counts_superseded_in_the_note_row():
    notes = [r for r in _list(_client()) if "est_item_id" not in r]
    (note,) = notes
    assert note["superseded"] == 1
    assert "superseded" in note["note"]


def test_a_sealed_successor_says_where_it_went():
    """2 of the 5 live successors are sealed into a deliverable — hidden from
    the default listing. The pointer must still lead somewhere readable."""
    c = _client(extra=[_edge("_authored/later-redirect", "_authored/deck")])
    by_id = _by_id(_list(c))
    assert "_authored/later-redirect" not in by_id  # hidden: sealed
    mark = by_id["_authored/old-frame"]["superseded_by"]
    assert mark["key"] == "_authored/later-redirect"
    assert mark["absorbed_by"] == "_authored/deck"


def test_no_challenge_edges_keeps_the_exact_old_shape():
    out = _list(_Client(project_rows=[_row("_authored/a")]))
    assert "superseded_by" not in out[0] and "contradicted_by" not in out[0]
    assert [r for r in out if "est_item_id" not in r] == []


def test_pull_leads_with_the_warning(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = ps.pull_spine(_client(), "p1", "_authored/old-frame", "co")
    assert next(iter(out)) == "warning"
    assert out["warning"] == (
        "Superseded by 'Branch-1 record 10-16' (_authored/later-redirect) "
        "on 2026-10-16 — read that first.")
    assert out["superseded_by"]["key"] == "_authored/later-redirect"
    # Both successors are named on the pull; the listing keeps the newest.
    assert [s["key"] for s in out["superseded_by_all"]] == [
        "_authored/later-redirect", "_authored/redirect"]
    assert out["body"] == "b"


def test_pull_of_a_current_element_has_no_warning(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = ps.pull_spine(_client(), "p1", "_authored/redirect", "co")
    assert "warning" not in out and "superseded_by" not in out


def test_pull_reports_contradictions_with_their_note(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    out = ps.pull_spine(_client(), "p1", "_authored/plain", "co")
    assert "warning" not in out
    assert out["contradicted_by"] == [
        {"key": "_authored/challenger", "framing": "_authored/challenger",
         "date": "2026-09-22", "note": "client margin comments"}]


def test_a_failed_edge_read_says_the_signal_is_unknown(monkeypatch):
    """No warning must not pass for "current" when the edges were unreadable."""
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    c = _Client(project_rows=[_row("_authored/a")], edges_fail=True)
    out = ps.pull_spine(c, "p1", "_authored/a", "co")
    assert "warning" not in out
    assert "permission denied" in out["supersession_error"]
    assert out["body"] == "b"
