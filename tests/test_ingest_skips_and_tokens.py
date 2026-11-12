"""Ingest fixes from the 2026-11-11 archive backfill: Google-native Drive
files get a change token, unchanged ARCHIVED files are not re-read, files
with no parser are skipped before download, and the pipeline's skip reasons
are counted as skips, never failures."""
from __future__ import annotations

from types import SimpleNamespace

import cp_engine.asset_ingest as ai
from cp_engine.asset_ingest import FileRef


def test_google_native_file_gets_a_modified_time_token():
    t = ai._drive_change_token({"id": "g1", "mimeType": "application/vnd.google-apps.document",
                                "modifiedTime": "2026-11-01T00:00:00Z"})
    assert t == "gnative:g1:2026-11-01T00:00:00Z"
    # A binary file keeps its checksum; one without a checksum stays None.
    assert ai._drive_change_token({"id": "b", "md5Checksum": "abc"}) == "abc"
    assert ai._drive_change_token({"id": "b", "mimeType": "application/pdf",
                                   "modifiedTime": "2026"}) is None


class _Q:
    def __init__(self, rows):
        self.rows, self.f = rows, {}

    def select(self, _c):
        return self

    def eq(self, c, v):
        self.f[c] = v
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return SimpleNamespace(data=[r for r in self.rows
                                     if all(r.get(k) == v for k, v in self.f.items())])


class _C:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _n):
        return _Q(self.rows)


def _ref(token):
    return FileRef(source="dropbox", id="id:1", name="Page_10.png", mime_type=None,
                   size=1, modified=None, change_token=token)


def test_unchanged_archived_file_is_not_reread():
    rows = [{"project_id": "p", "source_provider": "dropbox", "source_file_id": "id:1",
             "status": "archived", "meta": {"change_token": "t1"}}]
    assert ai._unchanged_since_last_ingest(_C(rows), "p", _ref("t1")) is True
    assert ai._unchanged_since_last_ingest(_C(rows), "p", _ref("t2")) is False


def test_has_parser_is_fail_open():
    assert ai._has_parser(SimpleNamespace(_select_parser=lambda n: None), "x.prproj") is False
    assert ai._has_parser(SimpleNamespace(_select_parser=lambda n: object()), "x.pdf") is True
    assert ai._has_parser(SimpleNamespace(), "x.prproj") is True  # fake without it

    def boom(_n):
        raise RuntimeError
    assert ai._has_parser(SimpleNamespace(_select_parser=boom), "x") is True


# ── the loop: skip reasons are counted, unsupported never downloads ──

from tests.test_asset_ingest_skip_loop import (  # noqa: E402
    _FakeClient, _patch_download_records, _patch_resolve_and_list, _ref as _loop_ref,
)


class _ReasonPipeline:
    """No parser for .prproj; .wav comes back skipped/empty; .docx created."""

    def __init__(self):
        self.calls = []

    def _select_parser(self, name):
        return None if str(name).endswith(".prproj") else object()

    def ingest_file(self, file_path, title, url=None):
        self.calls.append(title)
        if file_path.endswith(".wav"):
            return SimpleNamespace(success=True, file_path=file_path, action="skipped",
                                   skip_reason="empty", error="no speech in audio")
        return SimpleNamespace(success=True, file_path=file_path, action="created",
                               skip_reason=None, error=None)


def test_skip_reasons_count_as_skips_not_failures(monkeypatch, tmp_path):
    files = [_loop_ref("Template.prproj", token=None), _loop_ref("music.wav", token=None),
             _loop_ref("brief.docx", token=None)]
    _patch_resolve_and_list(monkeypatch, files)
    downloaded: list[str] = []
    _patch_download_records(monkeypatch, downloaded)
    monkeypatch.setattr(ai, "_stamp_scope", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_flag_confidentiality", lambda *a, **k: None)
    run = ai.ingest_project_assets("acme-1", client=_FakeClient(), pipeline=_ReasonPipeline(),
                                   tmp_root=tmp_path)
    assert (run.skipped_unsupported, run.skipped_empty, run.created, run.failed) == (1, 1, 1, 0)
    assert "Template.prproj" not in downloaded  # never downloaded
