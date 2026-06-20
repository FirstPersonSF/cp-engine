"""fetch_source: row coords -> FileRef -> download_file -> local path."""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
from cp_engine.project_sources import fetch_source


class _Q:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return SimpleNamespace(data=self._rows)

class _Client:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _Q(self._rows)


def test_fetch_source_downloads_and_returns_path(tmp_path, monkeypatch):
    rows = [{"title": "Deck.pptx", "source_provider": "dropbox",
             "source_file_id": "id:abc", "source_path": "/Remote/Deck.pptx",
             "url": "https://dropbox.com/s/abc"}]
    captured = {}
    def fake_download(file_ref, dest_dir, *a, **k):
        captured["ref"] = file_ref
        p = Path(dest_dir) / "Deck.pptx"; p.write_bytes(b"PK"); return p
    monkeypatch.setattr("cp_engine.project_sources.download_file", fake_download, raising=False)

    out = fetch_source(_Client(rows), "proj-1", "Deck.pptx", tmp_path)
    assert out["title"] == "Deck.pptx"
    assert out["provider"] == "dropbox"
    assert out["url"] == "https://dropbox.com/s/abc"
    assert Path(out["local_path"]).read_bytes() == b"PK"
    assert captured["ref"].id == "id:abc"
    assert captured["ref"].path == "/Remote/Deck.pptx"
    assert captured["ref"].source == "dropbox"


def test_fetch_source_missing_coords_returns_error(tmp_path):
    rows = [{"title": "Deck.pptx", "source_provider": None,
             "source_file_id": None, "source_path": None, "url": None}]
    out = fetch_source(_Client(rows), "proj-1", "Deck.pptx", tmp_path)
    assert "error" in out and "local_path" not in out


def test_fetch_source_not_found_returns_error(tmp_path):
    out = fetch_source(_Client([]), "proj-1", "Nope.pptx", tmp_path)
    assert "error" in out


def test_fetch_source_lookup_failure_returns_error(tmp_path):
    class _BoomClient:
        def table(self, name):
            raise RuntimeError("postgrest down")
    out = fetch_source(_BoomClient(), "proj-1", "Deck.pptx", tmp_path)
    assert "error" in out
