"""Every hosted source-title path resolves with an ENGINE resolver.

Two hosted copies stood beside the engine's rules:

  * `_resolve_source_asset` (rename_project_source, set_source_status) — the
    curation resolver, a copy of `project_sources._resolve_source_asset` that
    had drifted to case-INSENSITIVE matching, so two titles differing only in
    case read as ambiguous and a mis-cased title renamed a document.
  * `add_spine_document`'s inline title match — exact-then-substring over
    the project's rows only, any status, no account arm; a collision the
    attach ladder resolves (case-exact) was reported ambiguous.

The curation path now calls the engine's resolver; `add_spine_document` uses
the attach resolver (`pick_source` over active + account sources), the one
`add_element_source` uses. These pin each against the engine on one corpus.

    python -m pytest prototypes/hosted-mcp/test_source_resolver_parity.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine import project_sources as ps  # noqa: E402

P, CO = "p1", "co1"
U1 = "aaaaaaaa-0000-0000-0000-000000000001"
U2 = "aaaaaaaa-0000-0000-0000-000000000002"


def _a(i, title, *, status="active", project=P, scope="project"):
    return {"id": i, "project_id": project, "title": title, "status": status,
            "created_at": f"2026-09-0{len(i) % 9 + 1}", "archived_at": None,
            "scope": scope, "company_id": CO, "prev_asset_id": None,
            "source_type": "doc"}


ROWS = [
    _a(U1, "IBX Deck Review"), _a(U2, "IBX Deck review"),
    _a("u3", "Weekly Sync"), _a("u4", "Weekly Sync"),
    _a("u5", "Solo Brief"), _a("u6", "Old SOW", status="archived"),
    _a("u7", "Account Playbook", project="other", scope="account"),
]
KEYS = ["IBX Deck Review", "ibx deck review", "IBX Deck review", "Weekly Sync",
        "weekly sync", "Solo Brief", "Old SOW", U1, "Deck", "Account Playbook",
        "Playbook", "nothing like it"]


class _Q:
    def __init__(self, rows):
        self._rows, self._f = rows, []

    def select(self, cols, *a, **k):
        assert "*" not in cols  # never SELECT *
        return self

    def eq(self, col, val):
        self._f.append(lambda r: r.get(col) == val)
        return self

    def or_(self, expr):
        col, _, val = expr.split(".", 2)
        self._f.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, _val):
        self._f.append(lambda r: r.get(col) is None)
        return self

    def in_(self, col, vals):
        self._f.append(lambda r: r.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self._rows if all(f(r) for f in self._f)])


class _DB:
    def table(self, name):
        if name == "rag_assets":
            return _Q(ROWS)
        if name == "projects":
            return _Q([{"id": P, "company_id": CO}])
        return _Q([])


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


def _id(res):
    if res is None:
        return None
    if "candidates" in res:
        return ("ambiguous", sorted(c["id"] for c in res["candidates"]))
    return res["id"]


@pytest.mark.parametrize("active_only", [True, False])
@pytest.mark.parametrize("key", KEYS)
def test_curation_resolver_is_the_engines(server, key, active_only):
    db = _DB()
    assert _id(server._resolve_source_asset(db, P, key, active_only=active_only)) == \
        _id(ps._resolve_source_asset(db, P, key, active_only=active_only))


def test_case_variants_are_two_documents_not_one_ambiguity(server):
    """The drift the parity found: hosted folded case, so the caller who typed
    one title exactly was told it was ambiguous."""
    assert server._resolve_source_asset(_DB(), P, "IBX Deck review")["id"] == U2


def test_a_failed_read_is_an_error_not_a_miss(server):
    class _Boom:
        def table(self, _n):
            raise RuntimeError("PGRST 503")

    out = server._resolve_source_asset(_Boom(), P, "Solo Brief")
    assert "PGRST 503" in out["error"]


@pytest.mark.parametrize("key", KEYS)
def test_add_spine_document_resolves_like_the_attach_verbs(server, monkeypatch, key):
    db = _DB()
    pulled: list[str] = []
    monkeypatch.setattr(server, "user_client", lambda: db)
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": P, "kind": "project", "project_code": code})
    monkeypatch.setattr(server, "pull_project_source",
                        lambda asset_id: pulled.append(asset_id) or {"text": "body"})
    monkeypatch.setattr(server, "create_spine_element", lambda **kw: {"created": True})

    out = server.add_spine_document("ggl-5168", "Label", source_title=key)

    pool = ps.drop_superseded_assets(
        [r for r in ROWS if r["status"] == "active"
         and (r["project_id"] == P or r["scope"] == "account")])
    want, note = ps.pick_source(pool, key)
    if want is not None:
        assert pulled == [want["id"]], out
    else:
        assert pulled == [] and "error" in out, out
        if note.get("candidates"):
            assert sorted(c["id"] for c in out["candidates"]) == \
                sorted(c["id"] for c in note["candidates"])
