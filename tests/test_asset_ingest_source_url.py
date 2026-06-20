"""Tests for asset_ingest._source_url — durable web link per source."""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.asset_ingest import FileRef, _source_url


def _drive_ref():
    return FileRef(source="drive", id="DRIVEID1", name="x.pptx",
                   mime_type=None, size=None, modified=None, path=None)


def _dropbox_ref():
    return FileRef(source="dropbox", id="id:abc", name="x.pptx",
                   mime_type=None, size=None, modified=None,
                   path="/Remote Projects/IBX/x.pptx")


def test_drive_url_built_from_id_no_connector_call():
    assert _source_url(_drive_ref(), drive_connector=None, dropbox_connector=None) \
        == "https://drive.google.com/file/d/DRIVEID1/view"


def test_dropbox_url_uses_get_shareable_link():
    calls = {}

    def fake_link(path, **kw):
        calls["path"] = path
        calls["kw"] = kw
        return f"https://www.dropbox.com/s/abc?dl=0::{path}"

    dbx = SimpleNamespace(get_shareable_link=fake_link)
    url = _source_url(_dropbox_ref(), drive_connector=None, dropbox_connector=dbx)
    assert "dropbox.com" in url
    # The dropbox path (path_display) is what gets passed to the connector.
    assert calls["path"] == "/Remote Projects/IBX/x.pptx"


def test_dropbox_link_requests_team_only():
    """Client material must never get a public link — team_only=True is passed."""
    calls = {}

    def fake_link(path, **kw):
        calls["kw"] = kw
        return "https://www.dropbox.com/s/abc?dl=0"

    dbx = SimpleNamespace(get_shareable_link=fake_link)
    _source_url(_dropbox_ref(), drive_connector=None, dropbox_connector=dbx)
    assert calls["kw"].get("team_only") is True


def test_dropbox_no_connector_returns_none():
    assert _source_url(_dropbox_ref(), drive_connector=None,
                       dropbox_connector=None) is None


def test_dropbox_no_path_returns_none():
    ref = FileRef(source="dropbox", id="id:abc", name="x.pptx",
                  mime_type=None, size=None, modified=None, path=None)
    dbx = SimpleNamespace(get_shareable_link=lambda p, **kw: "https://x")
    assert _source_url(ref, drive_connector=None, dropbox_connector=dbx) is None


def test_dropbox_link_failure_returns_none_not_raise():
    def boom(path, **kw):
        raise RuntimeError("dropbox down")

    dbx = SimpleNamespace(get_shareable_link=boom)
    assert _source_url(_dropbox_ref(), drive_connector=None,
                       dropbox_connector=dbx) is None


def test_unknown_source_returns_none():
    ref = FileRef(source="other", id="x", name="x", mime_type=None,
                  size=None, modified=None, path=None)
    assert _source_url(ref, drive_connector=None, dropbox_connector=None) is None
