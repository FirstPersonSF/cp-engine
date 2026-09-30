# tests/test_list_spine_absorbed.py — stdio `list_spine_elements` is
# lifecycle-aware, with the hosted verb's `include_absorbed` semantics (#330).
#
# Before: stdio read no lifecycle edges at all. An element sealed into a
# shipped deliverable (`absorbed_by`) is HISTORICAL — hosted hides it by
# default and says how many it hid — but stdio listed it in the working set
# unmarked, and with both servers strict (#318) a caller passing
# `include_absorbed` to stdio failed outright. These drive the REAL verb and the
# REAL `list_spine`, against a fake Supabase-shaped client that serves both
# `spine_substance` and `spine_relations`.
import pytest

import cp_engine.mcp_server as srv
import cp_engine.project_sources as ps


def _row(eid, *, pid="p1", scope="project", cid=None, layer="Note"):
    return {"est_item_id": eid, "framing": eid, "layer": layer,
            "binding": "unbound", "status": "live", "serves": [], "body": "b",
            "important": False, "note": None, "archived": False,
            "scope": scope, "company_id": cid, "project_id": pid,
            "version_label": "v1", "version_date": "2026-09-01"}


class _Client:
    """Answers `spine_substance` by project/account arm and `spine_relations`
    from `edges`, filtered on the `in_("project_id", …)` the read passes."""

    def __init__(self, project_rows, account_rows=(), edges=(), edges_fail=False):
        self.project_rows = list(project_rows)
        self.account_rows = list(account_rows)
        self.edges = list(edges)
        self.edges_fail = edges_fail
        self.edge_project_ids = None

    def table(self, name):
        outer = self

        class _T:
            def __init__(self):
                self.eqs, self.ins = {}, {}
            def select(self, c): return self
            def eq(self, c, v): self.eqs[c] = v; return self
            def in_(self, c, v): self.ins[c] = list(v); return self
            def order(self, *a, **k): return self
            def execute(self):
                if name == "spine_relations":
                    if outer.edges_fail:
                        raise RuntimeError("permission denied for spine_relations")
                    outer.edge_project_ids = self.ins.get("project_id")
                    data = [e for e in outer.edges
                            if e["project_id"] in self.ins["project_id"]
                            and e["kind"] in self.ins["kind"]
                            and e["status"] == self.eqs.get("status")]
                elif self.eqs.get("scope") == "account":
                    data = [r for r in outer.account_rows
                            if r["company_id"] == self.eqs.get("company_id")]
                else:
                    data = [r for r in outer.project_rows
                            if r["project_id"] == self.eqs.get("project_id")]
                return type("R", (), {"data": [dict(r) for r in data]})()
        return _T()


def _edge(frm, to, *, pid="p1", kind="absorbed_by", status="active"):
    return {"kind": kind, "from_item_id": frm, "to_item_id": to,
            "project_id": pid, "status": status}


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    monkeypatch.setattr(srv, "_with_project_status", lambda rows, *a: rows)

    def serve(client):
        monkeypatch.setattr(srv, "_resolve", lambda code: (client, "p1", "co"))
        return client
    return serve


def _ids(rows):
    return [r["est_item_id"] for r in rows if "est_item_id" in r]


def _notes(rows):
    return [r for r in rows if "est_item_id" not in r]


def _sealed_client():
    return _Client(
        project_rows=[_row("_authored/raw-feedback"), _row("_authored/deck-v2",
                                                           layer="Deliverables")],
        edges=[_edge("_authored/raw-feedback", "_authored/deck-v2"),
               _edge("_authored/old", "_authored/deck-v1", status="retired")],
    )


@pytest.mark.parametrize("compact", [False, True])
def test_absorbed_element_hidden_by_default_and_counted(served, compact):
    """The default is the working set: the sealed element is gone, and a
    trailing note row says how many were hidden and how to see them."""
    served(_sealed_client())
    out = srv.list_spine_elements("ibx-5192", compact=compact)
    assert _ids(out) == ["_authored/deck-v2"]
    (note,) = _notes(out)
    assert note["absorbed_hidden"] == 1
    assert "include_absorbed=true" in note["note"]


@pytest.mark.parametrize("compact", [False, True])
def test_include_absorbed_lists_and_annotates(served, compact):
    """Retrospective mode lists the sealed element, marked with the
    deliverable that absorbed it; unsealed rows carry no marker."""
    served(_sealed_client())
    out = srv.list_spine_elements("ibx-5192", compact=compact, include_absorbed=True)
    by_id = {r["est_item_id"]: r for r in out if "est_item_id" in r}
    assert set(by_id) == {"_authored/raw-feedback", "_authored/deck-v2"}
    assert by_id["_authored/raw-feedback"]["absorbed_by"] == "_authored/deck-v2"
    assert "absorbed_by" not in by_id["_authored/deck-v2"]
    assert _notes(out) == []


def test_nothing_sealed_keeps_the_exact_old_shape(served):
    """No edges → no note row: a live project's list is unchanged."""
    served(_Client(project_rows=[_row("_authored/a")]))
    assert _ids(srv.list_spine_elements("ibx-5192")) == ["_authored/a"]
    assert _notes(srv.list_spine_elements("ibx-5192")) == []


def test_failed_edge_read_lists_everything_and_says_so(served):
    """An empty edge map un-hides every sealed element while looking exactly
    like "nothing was ever sealed" — so a failed read must be reported."""
    served(_Client(project_rows=[_row("_authored/a")], edges_fail=True))
    out = srv.list_spine_elements("ibx-5192")
    assert _ids(out) == ["_authored/a"]
    (note,) = _notes(out)
    assert note["annotations_available"] is False
    assert "permission denied" in note["annotations_error"]


def test_account_row_edges_read_under_its_home_project(served):
    """An account-scoped row keeps its HOME project's id; its seal lives
    there, so the edge read must cover every project the rows come from."""
    promoted = _row("_authored/fred", pid="p9", scope="account", cid="co",
                    layer="Stakeholders")
    client = served(_Client(project_rows=[_row("_authored/a")],
                            account_rows=[promoted],
                            edges=[_edge("_authored/fred", "_authored/x", pid="p9")]))
    out = srv.list_spine_elements("ibx-5192")
    assert set(client.edge_project_ids) == {"p1", "p9"}
    assert _ids(out) == ["_authored/a"]
    assert _notes(out)[0]["absorbed_hidden"] == 1


def test_positional_call_from_before_the_flag_still_binds():
    """`include_absorbed` is appended LAST, so an existing positional call
    (project_code, layer, scope, binding, compact, tier) binds unchanged."""
    import inspect

    params = list(inspect.signature(srv.list_spine_elements).parameters)
    assert params[-1] == "include_absorbed"
    assert params[:6] == ["project_code", "layer", "scope", "binding",
                          "compact", "tier"]


def test_in_process_callers_read_no_edges():
    """`list_spine` without the flag (preflight's in-process call) keeps its
    pre-#330 behaviour: no edge read, no note row."""
    client = _sealed_client()
    out = ps.list_spine(client, "p1")
    assert client.edge_project_ids is None
    assert set(_ids(out)) == {"_authored/raw-feedback", "_authored/deck-v2"}
