"""Tests for asset_ingest.download_file (Task C4 — download step only).

Connectors are mocked. We assert the two spec'd behaviors:

  - Drive: the connector's `download_file` may re-suffix the destination (Google
    Docs exported to .docx); the glue MUST return the connector's RETURNED path,
    not the path it passed in. The downstream parser dispatches on extension.
  - Dropbox: there is no `download_file` on the connector (gap-fill), so the glue
    calls `dbx.files_download(path=...)` directly and writes the bytes itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cp_engine.asset_ingest import FileRef, download_file


def _drive_ref(name: str) -> FileRef:
    return FileRef(
        source="drive",
        id="drive-id-1",
        name=name,
        mime_type="application/vnd.google-apps.document",
        size=None,
        modified="2026-06-13T00:00:00Z",
        path=None,
    )


def _dropbox_ref(name: str, path: str) -> FileRef:
    return FileRef(
        source="dropbox",
        id="id:abc",
        name=name,
        mime_type=None,
        size=5,
        modified="2026-06-13T00:00:00Z",
        path=path,
    )


class _FakeDriveConnector:
    """Simulates GoogleDriveConnector.download_file returning the actual path.

    `returned_path_factory(dest)` lets a test decide what the connector writes
    and returns — modelling the Google-Docs-export suffix behavior.
    """

    def __init__(self, returned_path_factory):
        self._factory = returned_path_factory
        self.calls = []

    def download_file(self, file_id, destination_path):
        self.calls.append((file_id, destination_path))
        actual = self._factory(destination_path)
        actual.write_bytes(b"exported-content")
        return actual


def test_download_drive_uses_connector_and_returns_actual_path(tmp_path):
    # Google Doc: connector exports to .docx, returning a DIFFERENT path.
    connector = _FakeDriveConnector(lambda dest: dest.with_suffix(".docx"))
    ref = _drive_ref("Our AI Story")  # no office extension on the name

    result = download_file(ref, tmp_path, drive_connector=connector)

    assert result.suffix == ".docx"
    assert result.exists()
    assert result != tmp_path / "Our AI Story"  # NOT the path we passed in
    # connector was called with the file id and a dest under tmp_path
    assert len(connector.calls) == 1
    file_id, dest = connector.calls[0]
    assert file_id == "drive-id-1"
    assert dest.parent == tmp_path


def test_download_drive_binary_keeps_path(tmp_path):
    # Regular binary (a PDF): connector returns the same dest it was given.
    connector = _FakeDriveConnector(lambda dest: dest)
    ref = _drive_ref("report.pdf")

    result = download_file(ref, tmp_path, drive_connector=connector)

    assert result == tmp_path / "report.pdf"
    assert result.exists()


def test_download_dropbox_sdk_gapfill_writes_bytes(tmp_path):
    captured = {}

    def _files_download(path):
        captured["path"] = path
        return (SimpleNamespace(), SimpleNamespace(content=b"hello"))

    connector = SimpleNamespace(dbx=SimpleNamespace(files_download=_files_download))
    ref = _dropbox_ref("notes.txt", "/Clients/Acme/notes.txt")

    result = download_file(ref, tmp_path, dropbox_connector=connector)

    assert result == tmp_path / "notes.txt"
    assert result.exists()
    assert result.read_bytes() == b"hello"
    assert captured["path"] == "/Clients/Acme/notes.txt"


def test_download_unknown_source_raises(tmp_path):
    ref = FileRef(
        source="weird",
        id="x",
        name="x.bin",
        mime_type=None,
        size=None,
        modified=None,
        path=None,
    )
    with pytest.raises(ValueError):
        download_file(ref, tmp_path)
