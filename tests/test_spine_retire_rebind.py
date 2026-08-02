# tests/test_spine_retire_rebind.py — issue #47: archived rows are hidden from
# every spine read path.
#
# The retitle/rebind/retire MCP WRAPPERS are all gone from stdio: `set_spine_element`
# ported to the hosted MCP server in cp-engine #143 batch 2, and
# `retire_spine_element(s)` in batch 4 (docs/hosted-mcp-team-setup.md). Their
# wrapper tests — and the `_patch_resolve`/`_element_updates` helpers that only
# they used — went with them. What remains is the read-path invariant those
# verbs exist to produce, which is pure `project_sources` and stays here: a
# retired (archived) element must be invisible to list / pull / resolve.
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
