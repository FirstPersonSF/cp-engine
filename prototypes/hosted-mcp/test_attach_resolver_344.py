"""Hosted `add_element_source` resolves what the caller can see (cp-engine #344).

Found running #270 on 2026-09-30:

  1. `IBX 5192 Deck Review` came back "ambiguous" against its case twin
     `IBX 5192 Deck review` — the "exact" rung compared case-insensitively.
  2. An asset id was not accepted as `source_title`, so a title collision had
     no escape hatch.
  3. `Our AI Story - Jun 2026.docx`, set to scope='account', listed on the
     sibling ibx-5192 (#324) but did not resolve for attach there — the
     resolver read only the workstream's own `project_id` rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_source_visibility import _Q  # noqa: E402  (same fake query builder)

DECK_A = "50d9ea14-a865-41c8-b772-7e4d202b8098"
DECK_B = "83ed2421-1111-4111-8111-111111111111"
AI_STORY = "607688d3-3a4b-40f1-a2a5-fcc26a057878"
OTHER_CO = "99999999-2222-4222-8222-222222222222"


class _DB:
    def __init__(self):
        self.tables = {
            "projects": [
                {"id": "p-5192", "company_id": "co-ibx"},
                {"id": "p-5153", "company_id": "co-ibx"},
            ],
            "rag_assets": [
                {"id": DECK_A, "project_id": "p-5192", "company_id": "co-ibx",
                 "scope": "project", "title": "IBX 5192 Deck Review", "status": "active"},
                {"id": DECK_B, "project_id": "p-5192", "company_id": "co-ibx",
                 "scope": "project", "title": "IBX 5192 Deck review", "status": "active"},
                {"id": AI_STORY, "project_id": "p-5153", "company_id": "co-ibx",
                 "scope": "account", "title": "Our AI Story - Jun 2026.docx",
                 "status": "active"},
                {"id": OTHER_CO, "project_id": "p-x", "company_id": "co-other",
                 "scope": "account", "title": "Other Company Story.docx",
                 "status": "active"},
            ],
        }

    def table(self, name):
        return _Q(self.tables.get(name, []))


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def db(server, monkeypatch):
    monkeypatch.setattr(server, "_owner_columns", lambda _c: ("project_id",))
    return _DB()


def _pick(server, db, want, scope_id="p-5192"):
    return server._resolve_active_asset(db, scope_id, want)


def test_case_exact_title_wins(server, db):
    asset, note = _pick(server, db, "IBX 5192 Deck Review")
    assert note is None, note
    assert asset["id"] == DECK_A


def test_case_insensitive_only_is_still_ambiguous(server, db):
    asset, note = _pick(server, db, "ibx 5192 deck REVIEW")
    assert asset is None
    assert "ambiguous" in note["note"]
    assert {c["id"] for c in note["candidates"]} == {DECK_A, DECK_B}


def test_asset_uuid_is_accepted(server, db):
    asset, note = _pick(server, db, DECK_B)
    assert note is None, note
    assert asset["id"] == DECK_B


def test_account_scoped_doc_resolves_for_a_sibling(server, db):
    asset, note = _pick(server, db, "Our AI Story - Jun 2026.docx")
    assert note is None, note
    assert asset["id"] == AI_STORY
    assert asset["scope"] == "account"


def test_another_companys_account_doc_never_resolves(server, db):
    for want in ("Other Company Story.docx", OTHER_CO):
        asset, note = _pick(server, db, want)
        assert asset is None and "no active source" in note["note"], (want, note)


def test_add_element_source_attaches_the_resolved_id(server, db, monkeypatch):
    """Through the verb, not just the helper: the entry handed to the write
    carries the account doc's id."""
    captured = {}
    monkeypatch.setattr(server, "user_client", lambda: db)
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda _c, code: {"id": "p-5192", "kind": "project",
                                          "project_code": "ibx-5192-x"})
    monkeypatch.setattr(server, "resolve_element_versions",
                        lambda _c, pid, key: ("_authored/ib", [{"status": "live",
                                                                "sources": []}], None))

    def fake_modify(client, code, key, entry, **kw):
        captured["entry"] = entry
        return {"attached": True, "source": entry}

    monkeypatch.setattr(server, "_modify_element_sources", fake_modify)
    fn = getattr(server.add_element_source, "fn", server.add_element_source)
    out = fn("ibx-5192", "inputs", "Our AI Story - Jun 2026.docx")
    assert captured.get("entry", {}).get("id") == AI_STORY, out
