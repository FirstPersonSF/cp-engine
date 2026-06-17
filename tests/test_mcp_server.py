"""Tests for the local stdio MCP server (Task 4).

The tool layer is WIRING ONLY: resolve code -> ids, call the pure function,
return its result. These tests exercise the tool FUNCTIONS directly (no real
stdio transport, no real Supabase) and assert that:

  - the tools delegate to the pure functions with the resolved ids,
  - a None resolution degrades gracefully (no crash),
  - exactly the two expected tools are registered,
  - importing the module stays light (no config / supabase at import time).
"""
from __future__ import annotations

import sys

import cp_engine.mcp_server as srv


def test_list_project_sources_delegates(monkeypatch):
    """Resolves ids, then returns the pure fn's result unchanged."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_list_sources(client, project_id, company_id):
        captured["args"] = (client, project_id, company_id)
        return [{"id": "a1", "title": "Doc One", "source_type": "gdoc"}]

    monkeypatch.setattr(
        "cp_engine.project_sources.list_sources", fake_list_sources
    )

    out = srv.list_project_sources("IBX-5153")

    assert captured["args"] == (fake_client, "pid", "cid")
    assert out == [{"id": "a1", "title": "Doc One", "source_type": "gdoc"}]


def test_pull_project_source_delegates(monkeypatch):
    """Passes title + query through to the pure fn and returns its result."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_pull_source(client, project_id, company_id, doc_title, query=None):
        captured["args"] = (client, project_id, company_id, doc_title, query)
        return {"title": doc_title, "chunks": ["c1", "c2"]}

    monkeypatch.setattr("cp_engine.project_sources.pull_source", fake_pull_source)

    out = srv.pull_project_source("IBX-5153", "Storybook", query="risks")

    assert captured["args"] == (fake_client, "pid", "cid", "Storybook", "risks")
    assert out == {"title": "Storybook", "chunks": ["c1", "c2"]}


def test_list_project_sources_unresolved_returns_empty(monkeypatch):
    """An unresolvable code yields an empty list, not a crash."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    assert srv.list_project_sources("nope") == []


def test_pull_project_source_unresolved_returns_note(monkeypatch):
    """An unresolvable code yields a not-found note, not a crash."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.pull_project_source("nope", "SomeDoc")
    assert out["title"] == "SomeDoc"
    assert out["chunks"] == []
    assert "not found" in out["note"]


def test_exactly_two_tools_registered():
    """The server registers exactly the two project-source tools."""
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert names == {"list_project_sources", "pull_project_source"}


def test_module_import_is_light():
    """Importing the server must NOT pull in config / supabase at module load.

    The config + supabase imports live inside the tool bodies on purpose, so a
    fresh `import cp_engine.mcp_server` leaves them out of sys.modules.
    """
    for mod in ("cp_engine.mcp_server", "cp_engine.sync_mc2", "cp_engine.cli"):
        sys.modules.pop(mod, None)

    import cp_engine.mcp_server  # noqa: F401  (re-import after eviction)

    assert "cp_engine.mcp_server" in sys.modules
    assert "cp_engine.sync_mc2" not in sys.modules
    assert "cp_engine.cli" not in sys.modules
