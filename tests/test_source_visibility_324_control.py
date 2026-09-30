"""Behaviour-level controls for #324 — written against APIs that exist on
BOTH the unfixed and the fixed code, so running this file against
origin/main FAILS on behaviour (not on an import), and passes here.

Uses the shared filtering fake in tests/_source_store_fake.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cp_engine.asset_ingest import FileRef, _row_to_folders, _stamp_scope
from cp_engine.project_sources import list_sources, pull_source

from tests._source_store_fake import _DB, _asset, _chunk


def _db():
    return _DB(rag_assets=[_asset("e", "Scanned Brief.pdf")], asset_chunks=[])


def test_control_zero_chunk_pull_is_not_reported_missing():
    out = pull_source(_db(), "p-job", "co-1", "Scanned Brief.pdf")
    assert out.get("empty") is True, out


def test_control_zero_chunk_listing_is_flagged():
    (entry,) = list_sources(_db(), "p-job", "co-1")
    assert entry.get("empty") is True, entry


def test_control_account_node_ingest_is_account_scoped():
    folders = _row_to_folders({"id": "p-acct", "company_id": "co-1", "deal_stage": None,
                               "parent_id": None, "companies": {"kind": "client"}})
    db = _DB(rag_assets=[_asset("x", "Deck.pdf", project="p-acct", file_path="/t/x")])
    ref = FileRef(source="drive", id="D", name="Deck.pdf", mime_type=None, size=None,
                  modified=None)
    _stamp_scope(db, folders, "/t/x", ref)
    assert db.tables["rag_assets"][0]["scope"] == "account"


def test_control_marked_body_sets_status_note(monkeypatch, tmp_path):
    import cp_engine.asset_ingest as ai

    folders = _row_to_folders({"id": "p-job", "company_id": "co-1", "deal_stage": "Won",
                               "parent_id": "p-acct", "companies": {"kind": "client"},
                               "google_drive_folder_id": "F", "enable_google_drive": True})
    db = _DB()
    ref = FileRef(source="drive", id="D", name="Storybook.pdf", mime_type=None,
                  size=None, modified=None)
    monkeypatch.setattr(ai, "resolve_project_folders", lambda c, code: folders)
    monkeypatch.setattr(ai, "list_files", lambda *a, **k: ([ref], []))
    monkeypatch.setattr(ai, "_unchanged_since_last_ingest", lambda *a, **k: False)
    monkeypatch.setattr(ai, "_existing_dup_at_other_path", lambda *a, **k: False)
    monkeypatch.setattr(ai, "_supersede_same_title", lambda *a, **k: 0)
    monkeypatch.setattr(ai, "_existing_source_titles", lambda *a, **k: set())

    def _download(r, d, *_a):
        p = Path(d) / r.name
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(ai, "download_file", _download)

    class _Pipeline:
        def ingest_file(self, file_path, title, url):
            db.tables.setdefault("rag_assets", []).append(
                _asset("new", title, project="p-job", file_path=file_path))
            db.tables.setdefault("asset_chunks", []).append(
                _chunk("new", "AMEX GBT STORYBOOK SAP and External Parties under NDA Only"))
            return SimpleNamespace(action="created")

    ai.ingest_project_assets("ggl-5168", client=db, pipeline=_Pipeline(), tmp_root=tmp_path)
    assert db.tables["rag_assets"][0].get("status_note"), db.tables["rag_assets"][0]
