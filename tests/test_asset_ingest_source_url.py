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

    # Realistic team-only-capable signature: `team_only` is a named parameter, so
    # _source_url's capability check passes and it calls through.
    def fake_link(path, team_only=False):
        calls["path"] = path
        calls["team_only"] = team_only
        return f"https://www.dropbox.com/s/abc?dl=0::{path}"

    dbx = SimpleNamespace(get_shareable_link=fake_link)
    url = _source_url(_dropbox_ref(), drive_connector=None, dropbox_connector=dbx)
    assert "dropbox.com" in url
    # The dropbox path (path_display) is what gets passed to the connector.
    assert calls["path"] == "/Remote Projects/IBX/x.pptx"


def test_dropbox_link_requests_team_only():
    """Client material must never get a public link — team_only=True is passed.

    The connector's signature genuinely declares `team_only` (not absorbed by
    **kwargs), so this both exercises the capability check AND verifies the
    kwarg is passed as True when supported.
    """
    calls = {}

    def fake_link(path, team_only=False):
        calls["team_only"] = team_only
        return "https://www.dropbox.com/s/abc?dl=0"

    dbx = SimpleNamespace(get_shareable_link=fake_link)
    _source_url(_dropbox_ref(), drive_connector=None, dropbox_connector=dbx)
    assert calls["team_only"] is True


def test_dropbox_connector_lacking_team_only_returns_none():
    """FAIL CLOSED: a connector that can't enforce team-only yields no link.

    If get_shareable_link's signature lacks a `team_only` parameter (an old or
    intermediate connector deploy), _source_url must NOT call it / must NOT trust
    a link it returns — it returns None rather than risk a public client link.
    """
    called = {"hit": False}

    def gsl(path):  # NO team_only param → not team-only-capable
        called["hit"] = True
        return "https://www.dropbox.com/s/abc?dl=0"  # would be public

    dbx = SimpleNamespace(get_shareable_link=gsl)
    assert _source_url(_dropbox_ref(), drive_connector=None,
                       dropbox_connector=dbx) is None
    # We must not even call a connector that can't prove team-only enforcement.
    assert called["hit"] is False


def test_dropbox_no_connector_returns_none():
    assert _source_url(_dropbox_ref(), drive_connector=None,
                       dropbox_connector=None) is None


def test_dropbox_no_path_returns_none():
    ref = FileRef(source="dropbox", id="id:abc", name="x.pptx",
                  mime_type=None, size=None, modified=None, path=None)
    dbx = SimpleNamespace(get_shareable_link=lambda p, **kw: "https://x")
    assert _source_url(ref, drive_connector=None, dropbox_connector=dbx) is None


def test_dropbox_link_failure_returns_none_not_raise():
    # team_only declared so the capability check passes and we reach the call,
    # exercising the exception → None path (not the capability-check None path).
    def boom(path, team_only=False):
        raise RuntimeError("dropbox down")

    dbx = SimpleNamespace(get_shareable_link=boom)
    assert _source_url(_dropbox_ref(), drive_connector=None,
                       dropbox_connector=dbx) is None


def test_unknown_source_returns_none():
    ref = FileRef(source="other", id="x", name="x", mime_type=None,
                  size=None, modified=None, path=None)
    assert _source_url(ref, drive_connector=None, dropbox_connector=None) is None
