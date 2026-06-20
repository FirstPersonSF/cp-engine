"""fetch_project_source MCP tool — wiring + never-throws contract.

Mirrors the calling convention of test_mcp_server.py: the tool FUNCTIONS are
called directly (no stdio transport, no real Supabase). `_resolve` returns
`(client, project_id, company_id)` or `None`; the tool wires the resolved
project_id into the pure `fetch_source` and returns its result unchanged.
"""
from __future__ import annotations

import cp_engine.mcp_server as srv


def test_fetch_tool_returns_error_dict_on_resolve_failure(monkeypatch):
    """A raising _resolve (bad config / no creds) yields a structured error."""
    def boom(code):
        raise RuntimeError("no tenant")

    monkeypatch.setattr(srv, "_resolve", boom)
    out = srv.fetch_project_source("ibx-x", "Deck.pptx")
    assert isinstance(out, dict) and "error" in out  # never raises
    assert "no tenant" in out["error"]


def test_fetch_tool_unresolved_returns_error(monkeypatch):
    """An unresolvable code → structured 'not found' error, not a crash."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.fetch_project_source("nope", "Deck.pptx")
    assert "not found" in out["error"]


def test_fetch_tool_calls_fetch_source_with_resolved_ids(monkeypatch):
    """Passes the resolved project_id + title into fetch_source, returns its result."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_fetch_source(client, project_id, doc_title, dest_dir):
        captured["args"] = (client, project_id, doc_title)
        captured["dest_dir"] = dest_dir
        return {"local_path": "/tmp/x/Deck.pptx", "title": doc_title,
                "provider": "dropbox", "url": "https://..."}

    monkeypatch.setattr("cp_engine.project_sources.fetch_source", fake_fetch_source)

    out = srv.fetch_project_source("IBX-5153", "Deck.pptx")

    assert captured["args"] == (fake_client, "pid", "Deck.pptx")
    # a real temp dir was created and handed to the pure fn
    assert isinstance(captured["dest_dir"], str) and captured["dest_dir"]
    assert out == {"local_path": "/tmp/x/Deck.pptx", "title": "Deck.pptx",
                   "provider": "dropbox", "url": "https://..."}


def test_fetch_tool_pure_fn_raises_returns_error(monkeypatch):
    """A raising pure fn (download error) is caught and returned structured."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    def boom(client, project_id, doc_title, dest_dir):
        raise RuntimeError("download failed")

    monkeypatch.setattr("cp_engine.project_sources.fetch_source", boom)

    out = srv.fetch_project_source("IBX-5153", "Deck.pptx")
    assert "error" in out
    assert "download failed" in out["error"]
