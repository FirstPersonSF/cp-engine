"""Behaviour-level controls for #341 — a re-ingest must not lose human curation.

Written against APIs that exist on BOTH the unfixed and the fixed code (only
`ingest_project_assets` and the shared filtering fake), so running this file
against origin/main FAILS on behaviour, not on an import.

Both re-ingest shapes are driven through the real loop:
  - 'versioned' — the pipeline's same-path new_version inserts the new row with
    `prev_asset_id` already set and retires the old one;
  - 'created'   — a same-title re-ingest at a fresh path; the real
    `_supersede_same_title` chains it.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cp_engine.asset_ingest import FileRef, ProjectFolders

from tests._source_store_fake import _DB, _asset, _chunk

HUMAN_NOTE = "embargoed until the 10-14 launch — Janet 09-01; never quote externally"
HUMAN_DESC = "Janet's approved launch brief — the canonical positioning for Q4"
# A prior row ingested after #341 whose description a person wrote: tracked,
# and no summariser fingerprint.
TRACKED_META = {"curation_tracked": True}

_FOLDERS = ProjectFolders(
    project_id="p-job", company_id="co-1", company_kind="client",
    google_drive_folder_id="F", mc_dropbox_folder_id=None,
    enable_google_drive=True, enable_dropbox=False,
)
_REF = FileRef(source="drive", id="D1", name="Launch Brief.pdf",
               mime_type=None, size=None, modified=None)


def _run(monkeypatch, tmp_path, db, pipeline):
    import cp_engine.asset_ingest as ai

    monkeypatch.setattr(ai, "resolve_project_folders", lambda c, code: _FOLDERS)
    monkeypatch.setattr(ai, "list_files", lambda *a, **k: ([_REF], []))
    monkeypatch.setattr(ai, "_unchanged_since_last_ingest", lambda *a, **k: False)
    monkeypatch.setattr(ai, "_existing_dup_at_other_path", lambda *a, **k: False)
    monkeypatch.setattr(ai, "_existing_source_titles", lambda *a, **k: set())

    def _download(ref, d, *_a):
        p = Path(d) / ref.name
        p.write_bytes(b"new content")
        return p

    monkeypatch.setattr(ai, "download_file", _download)
    return ai.ingest_project_assets("ggl-5168", client=db, pipeline=pipeline,
                                    tmp_root=tmp_path)


def _prior(**kw):
    return _asset("old", "Launch Brief.pdf", project="p-job", file_path="/old/path",
                  file_hash="oldhash", created_at="2026-09-01", **kw)


def _new_row(db):
    (row,) = [r for r in db.tables["rag_assets"] if r["status"] == "active"]
    return row


class _Versioning:
    """The pipeline's same-path new_version: new row chained, old retired."""

    def __init__(self, db, body="a public case study"):
        self.db, self.body = db, body

    def ingest_file(self, file_path, title, url):
        for r in self.db.tables["rag_assets"]:
            if r["id"] == "old":
                r["status"] = "superseded"
        self.db.tables["rag_assets"].append(
            _asset("new", title, project="p-job", file_path=file_path,
                   prev_asset_id="old", file_hash="newhash", meta={"chunks": 3}))
        self.db.tables.setdefault("asset_chunks", []).append(_chunk("new", self.body))
        return SimpleNamespace(action="versioned")


class _Creating(_Versioning):
    """A same-title re-ingest at a fresh path: 'created', no chain yet."""

    def ingest_file(self, file_path, title, url):
        self.db.tables["rag_assets"].append(
            _asset("new", title, project="p-job", file_path=file_path,
                   file_hash=None, meta={"chunks": 3}))
        self.db.tables.setdefault("asset_chunks", []).append(_chunk("new", self.body))
        return SimpleNamespace(action="created")


def test_control_versioned_reingest_keeps_the_human_note(monkeypatch, tmp_path):
    db = _DB(rag_assets=[_prior(status_note=HUMAN_NOTE)])
    result = _run(monkeypatch, tmp_path, db, _Versioning(db))
    assert result.failures == []
    assert _new_row(db).get("status_note") == HUMAN_NOTE, _new_row(db)


def test_control_created_reingest_keeps_the_human_note(monkeypatch, tmp_path):
    import cp_engine.asset_ingest as ai

    db = _DB(rag_assets=[_prior(status_note=HUMAN_NOTE)])
    # The loop hashes the downloaded file; the fake's new row learns it so the
    # real supersede sees "same title, different content".
    real_hash = ai._content_hash

    class _C(_Creating):
        def ingest_file(self, file_path, title, url):
            out = super().ingest_file(file_path, title, url)
            _new_row_any(self.db)["file_hash"] = real_hash(file_path)
            return out

    result = _run(monkeypatch, tmp_path, db, _C(db))
    assert result.superseded == 1, result
    new = _new_row(db)
    assert new.get("prev_asset_id") == "old"
    assert new.get("status_note") == HUMAN_NOTE, new


def _new_row_any(db):
    return next(r for r in db.tables["rag_assets"] if r["id"] == "new")


def test_control_a_marked_body_does_not_displace_the_human_note(monkeypatch, tmp_path):
    """The #324 detector runs on the new row too. Its note must not win."""
    db = _DB(rag_assets=[_prior(status_note=HUMAN_NOTE)])
    result = _run(monkeypatch, tmp_path, db,
                  _Versioning(db, body="INFOBLOX | CAB 2026 | CONFIDENTIAL 12"))
    assert result.flagged_confidential == 0
    assert _new_row(db).get("status_note") == HUMAN_NOTE, _new_row(db)


def test_control_versioned_reingest_keeps_a_hand_written_description(monkeypatch, tmp_path):
    db = _DB(rag_assets=[_prior(description=HUMAN_DESC, meta=dict(TRACKED_META))])
    _run(monkeypatch, tmp_path, db, _Versioning(db))
    assert _new_row(db).get("description") == HUMAN_DESC, _new_row(db)
