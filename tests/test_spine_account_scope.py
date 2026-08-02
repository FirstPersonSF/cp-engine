# tests/test_spine_account_scope.py — the scope ladder (mig 104): account-scoped
# elements read from every project of the company.
#
# The verbs that MOVE an element up or down the ladder — `promote_stakeholder`,
# `demote_stakeholder`, `set_element_account_scope` — were ported to the hosted
# MCP server and deleted from stdio (cp-engine #143 batch 4,
# docs/hosted-mcp-team-setup.md), where the engagements-only and sibling-twin
# guards live in the DB; their wrapper tests moved with them. What stays here is
# the READ half of the ladder, which is pure `project_sources`. The one surviving
# in-process caller of the move — `pull_element_from_project(account=True)` via
# the private `_set_account_scope` — is covered in
# tests/test_mcp_server.py::test_pull_element_from_project_account_tag.
from cp_engine.project_sources import (
    _fetch_scoped,
    list_spine,
    pull_spine,
    resolve_element_versions,
    resolve_live_element,
)

_COLS = "id, est_item_id, framing, status, scope, company_id, project_id"


class _TwoArmClient:
    """Fake client that answers the project arm and the account arm from two
    row sets, keyed on which .eq() filters the query carries."""

    def __init__(self, project_rows, account_rows):
        self._project_rows = project_rows
        self._account_rows = account_rows
        self.updates = []

    def table(self, n):
        outer = self

        class _T:
            def __init__(self):
                self._eqs = {}
                self._patch = None
            def select(self, c): return self
            def eq(self, c, v): self._eqs[c] = v; return self
            def order(self, *a, **k): return self
            def limit(self, n): return self
            def update(self, d): self._patch = d; return self
            def execute(self):
                if self._patch is not None:
                    outer.updates.append((self._patch, dict(self._eqs)))
                    return type("R", (), {"data": []})()
                if self._eqs.get("scope") == "account":
                    rows = [r for r in outer._account_rows
                            if r.get("company_id") == self._eqs.get("company_id")]
                else:
                    rows = [r for r in outer._project_rows
                            if r.get("project_id") == self._eqs.get("project_id")]
                if self._eqs.get("status"):
                    rows = [r for r in rows if r.get("status") == self._eqs["status"]]
                if self._eqs.get("est_item_id"):
                    rows = [r for r in rows
                            if r.get("est_item_id") == self._eqs["est_item_id"]]
                return type("R", (), {"data": [dict(r) for r in rows]})()
        return _T()


def _row(eid, *, pid="p1", scope="project", cid=None, status="live",
         framing=None, layer="Stakeholders"):
    return {"id": f"{pid}/{eid}/{status}", "est_item_id": eid,
            "framing": framing or eid, "layer": layer, "binding": "unbound",
            "status": status, "serves": [], "body": "b", "important": False,
            "note": None, "archived": False, "scope": scope,
            "company_id": cid, "project_id": pid}


def _sap():
    """One company, two projects: p1 owns a project dossier + a promoted one;
    p2 (the sibling) owns nothing."""
    promoted = _row("_authored/fred", pid="p1", scope="account", cid="sap")
    local = _row("_authored/olivia", pid="p1")
    return _TwoArmClient(project_rows=[promoted, local], account_rows=[promoted])


# ── read union ───────────────────────────────────────────────────────────────

def test_sibling_project_sees_account_elements():
    out = list_spine(_sap(), "p2", "sap")
    ids = {r["est_item_id"]: r["scope"] for r in out}
    assert ids == {"_authored/fred": "account"}       # local p1 element stays home


def test_originating_project_sees_promoted_element_exactly_once():
    out = list_spine(_sap(), "p1", "sap")
    ids = [r["est_item_id"] for r in out]
    assert sorted(ids) == ["_authored/fred", "_authored/olivia"]
    scopes = {r["est_item_id"]: r["scope"] for r in out}
    assert scopes["_authored/fred"] == "account"


def test_no_company_means_no_account_arm():
    out = list_spine(_sap(), "p2", None)              # initiative-shaped read
    assert out == []


def test_pull_resolves_account_element_from_sibling():
    el = pull_spine(_sap(), "p2", "_authored/fred", "sap")
    assert el.get("scope") == "account" and el.get("body") == "b"


def test_resolve_live_element_carries_provenance_project_id():
    row = resolve_live_element(_sap(), "p2", "fred", "sap")
    assert row and row["project_id"] == "p1"          # writes must target p1


def test_version_history_never_mixes_scope_arms():
    """A same-slug project element in the caller's project must not pollute an
    account element's version history (and vice versa)."""
    account_v1 = _row("_authored/fred", pid="p1", scope="account", cid="sap")
    account_v0 = _row("_authored/fred", pid="p1", scope="account", cid="sap",
                      status="superseded")
    local_twin = _row("_authored/fred", pid="p2")     # p2's own unrelated fred
    client = _TwoArmClient(project_rows=[local_twin],
                           account_rows=[account_v1, account_v0])
    eid, versions = resolve_element_versions(
        client, "p2", "_authored/fred", columns=_COLS, company_id="sap")
    assert eid == "_authored/fred"
    # exact est_item_id match prefers the caller's own project arm first
    assert all(r["scope"] == "project" for r in versions)
    assert len(versions) == 1


def test_fetch_scoped_defaults_missing_scope_to_project():
    legacy = _row("_authored/old", pid="p1")
    legacy["scope"] = None                            # pre-migration row
    client = _TwoArmClient(project_rows=[legacy], account_rows=[])
    rows = _fetch_scoped(client, "p1", "sap", _COLS)
    assert [r["est_item_id"] for r in rows] == ["_authored/old"]
