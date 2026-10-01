"""Hosted source reads see what the store holds (cp-engine #324).

The hosted server keeps its own copies of the source verbs (the vendored
`cp_engine` is constants-and-lints only), so the #324 fixes are pinned here
separately from the stdio ones in tests/test_source_visibility_324.py:

  - `list_project_sources` carries `status_note` / `description` (the stdio
    list always did; the hosted one dropped them, so an embargoed source
    read as ordinary), names the company's account-scoped docs, and flags a
    zero-chunk source `empty: True`;
  - `pull_project_source` says a zero-chunk source EXISTS and is empty;
  - `spine_lint` runs the dangling `Source reviewed:` check from the clone.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _Q:
    def __init__(self, rows):
        self._rows, self._f, self._lo, self._hi = rows, [], 0, None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r, c=col: r.get(c) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        return self

    def execute(self):
        rows = [dict(r) for r in self._rows if all(f(r) for f in self._f)]
        return SimpleNamespace(data=rows[self._lo:(self._hi + 1 if self._hi is not None else None)])


class _DB:
    def __init__(self):
        self.tables = {
            "projects": [
                {"id": "p-job", "company_id": "co-1", "mc_status": "Open"},
                {"id": "p-acct", "company_id": "co-1", "mc_status": "Open"},
            ],
            "rag_assets": [
                {"id": "r", "project_id": "p-job", "company_id": "co-1", "scope": "project",
                 "title": "Readable.docx", "status": "active", "created_at": "2026-09-02",
                 "status_note": "auto-detected at ingest: body marked confidential"},
                {"id": "e", "project_id": "p-job", "company_id": "co-1", "scope": "project",
                 "title": "Scanned Brief.pdf", "status": "active", "created_at": "2026-09-01"},
                {"id": "acct", "project_id": "p-acct", "company_id": "co-1", "scope": "account",
                 "title": "Company Deck.pdf", "status": "active", "created_at": "2026-08-01"},
                {"id": "other", "project_id": "p-x", "company_id": "co-2", "scope": "account",
                 "title": "Other Company.pdf", "status": "active", "created_at": "2026-08-01"},
            ],
            "asset_chunks": [
                {"id": "c1", "asset_id": "r", "text": "real text", "start_seconds": None},
                {"id": "c2", "asset_id": "acct", "text": "company text", "start_seconds": None},
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
    db = _DB()
    monkeypatch.setattr(server, "user_client", lambda: db)
    monkeypatch.setattr(server, "caller_subject", lambda: "u")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "resolve_project_id", lambda _c, code: code)
    monkeypatch.setattr(server, "_owner_columns", lambda _c: ("project_id",))
    return db


def test_listing_carries_the_trust_caveat_the_account_docs_and_the_empty_flag(server, db):
    out = server.list_project_sources("p-job")
    by_title = {s["title"]: s for s in out["sources"]}
    assert set(by_title) == {"Readable.docx", "Scanned Brief.pdf", "Company Deck.pdf"}
    assert by_title["Readable.docx"]["status_note"].startswith("auto-detected")
    assert by_title["Company Deck.pdf"]["scope"] == "account"
    assert by_title["Scanned Brief.pdf"]["empty"] is True
    assert by_title["Scanned Brief.pdf"]["chunk_count"] == 0
    assert "empty" not in by_title["Readable.docx"]
    assert "empty" not in by_title["Company Deck.pdf"]


def test_a_failed_chunk_check_flags_nothing(server, db, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("42501")

    monkeypatch.setattr(server, "_asset_ids_with_chunks", _boom)
    out = server.list_project_sources("p-job")
    assert all("empty" not in s for s in out["sources"])


def test_pull_of_a_zero_chunk_source_says_it_exists(server, db):
    out = server.pull_project_source("e")
    assert "error" not in out
    assert out["chunk_count"] == 0 and out["empty"] is True
    full = server.pull_project_source("r")
    assert "empty" not in full and full["status_note"].startswith("auto-detected")


def test_the_workstream_walk_stops_at_a_child(server, tmp_path):
    (tmp_path / "cp.md").write_text("parent")
    (tmp_path / "review.md").write_text("**Source reviewed:** `Gone.md`")
    child = tmp_path / "child"
    child.mkdir()
    (child / "cp.md").write_text("child")
    (child / "c.md").write_text("x")
    docs, names = server._workstream_docs(tmp_path)
    assert set(docs) == {"cp.md", "review.md"} and "c.md" not in names


def test_spine_lint_flags_a_dangling_source_reviewed(server, db, monkeypatch, tmp_path):
    ws = tmp_path / "1p" / "infoblox" / "p-job"
    ws.mkdir(parents=True)
    (ws / "cp.md").write_text("# cp")
    (ws / "review-v01.md").write_text(
        "**Source reviewed:** `Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md`\n"
        "**Sources reviewed:** `Readable.docx`\n")
    monkeypatch.setattr(server, "tree_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_project_codes_for_lint", lambda _c, code: [code])
    db.tables["spine_substance"] = []
    out = server.spine_lint("p-job")
    assert out["source_reviewed_checked"] is True
    assert len(out["warnings"]) == 1
    assert "Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md" in out["warnings"][0]


def test_listing_hides_superseded_and_archived_by_the_engine_rule(server, db, monkeypatch):
    """Architecture plan step 1c (H4): the supersede filter is
    `project_sources.drop_superseded_assets`. A predecessor whose successor is
    in the set is hidden, an archived row is hidden, and `superseded_hidden`
    still counts predecessor ids."""
    from cp_engine import project_sources

    db.tables["rag_assets"] += [
        {"id": "old", "project_id": "p-job", "company_id": "co-1", "scope": "project",
         "title": "Brief v1.docx", "status": "active", "created_at": "2026-07-01"},
        {"id": "new", "project_id": "p-job", "company_id": "co-1", "scope": "project",
         "title": "Brief v2.docx", "status": "active", "created_at": "2026-07-02",
         "prev_asset_id": "old"},
        {"id": "gone", "project_id": "p-job", "company_id": "co-1", "scope": "project",
         "title": "Gone.docx", "status": "archived", "created_at": "2026-07-03"},
    ]
    calls = []
    real = project_sources.drop_superseded_assets
    monkeypatch.setattr(server._engine_project_sources, "drop_superseded_assets",
                        lambda rows: calls.append(len(rows)) or real(rows))
    out = server.list_project_sources("p-job")
    titles = {s["title"] for s in out["sources"]}
    assert "Brief v2.docx" in titles
    assert "Brief v1.docx" not in titles and "Gone.docx" not in titles
    assert out["superseded_hidden"] == 1
    assert calls, "the listing did not use the engine's supersede rule"


def test_the_324_source_store_helpers_are_the_engines(server, db, tmp_path):
    """Architecture plan step 1c: the chunk check, the workstream doc walk and
    the ingested-title set are the engine's, not hosted twins."""
    from cp_engine import mc2_db, project_sources, spine

    assert server._asset_ids_with_chunks is mc2_db.asset_ids_with_chunks
    assert server._workstream_docs is spine.workstream_docs
    titles = server._ingested_source_titles(db, "p-job")
    assert titles == project_sources.ingested_source_titles(db, "p-job", "co-1")
    assert "Company Deck.pdf" in titles and "Other Company.pdf" not in titles

    class Boom:
        def table(self, _name):
            raise RuntimeError("no")

    assert server._ingested_source_titles(Boom(), "p-job") is None
