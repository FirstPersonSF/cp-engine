"""Tests for the `promote_spine_transcript` MCP tool.

The tool is WIRING ONLY: resolve the project + the live element, then delegate
to `cp_engine.spine_promote.promote_transcript` (the pure orchestrator) and nest
its result under "promotion". It is the standalone, idempotent retry door for a
failed embed — calling it again simply re-runs `promote_transcript`.

These tests monkeypatch every collaborator so no config / network / Supabase is
touched:
  - `srv._resolve` → a fake `(client, pid, company_id)` (or None for "not found")
  - `cp_engine.project_sources.resolve_live_element` → the matched row (or None)
  - `cp_engine.spine_promote.promote_transcript` → a recording stub
  - `srv._tenant_root` and the cred loader → cheap fakes
"""

from __future__ import annotations

import cp_engine.mcp_server as srv


def _patch_creds(monkeypatch):
    """Stub the tenant-root + supabase cred source so the tool never loads config."""
    monkeypatch.setattr(srv, "_tenant_root", lambda: "/tenant")
    monkeypatch.setattr(
        "cp_engine.config.load", lambda root: object(), raising=False
    )
    monkeypatch.setattr(
        "cp_engine.sync_mc2._load_supabase_creds",
        lambda config: ("https://u", "key"),
        raising=False,
    )
    monkeypatch.setattr(
        "cp_engine.sync_mc2._load_ingest_creds",
        lambda config: None,
        raising=False,
    )


def test_promote_happy(monkeypatch):
    """Resolved element → promote_transcript runs; result nested under 'promotion'."""
    client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    row = {"est_item_id": "_authored/email-from-janet", "rel_path": "meetings/x.txt"}
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda c, pid, key: row,
    )
    calls = []

    def fake_promote(c, tenant_root, project_code, pid, company_id, element_row,
                     *, supabase_url, supabase_key):
        calls.append((c, tenant_root, project_code, pid, company_id, element_row,
                      supabase_url, supabase_key))
        return {"ok": True, "ids": ["a1"]}

    monkeypatch.setattr("cp_engine.spine_promote.promote_transcript", fake_promote)
    _patch_creds(monkeypatch)

    out = srv.promote_spine_transcript("ibx-5192", "email")

    assert out == {
        "est_item_id": "_authored/email-from-janet",
        "promotion": {"ok": True, "ids": ["a1"]},
    }
    # promote_transcript got the resolved client, project_id, company_id, and row.
    assert len(calls) == 1
    c, tenant_root, code, pid, company_id, element_row, url, key = calls[0]
    assert c is client
    assert tenant_root == "/tenant"
    assert code == "ibx-5192"
    assert pid == "pid"
    assert company_id == "cid"
    assert element_row is row
    assert (url, key) == ("https://u", "key")


def test_promote_initiative_deferred(monkeypatch):
    """company_id=None (initiative) flows through; promote_transcript's guard surfaces."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", None))
    row = {"est_item_id": "storyos-elem", "rel_path": "meetings/x.txt"}
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda c, pid, key: row,
    )

    def fake_promote(c, tenant_root, project_code, pid, company_id, element_row,
                     *, supabase_url, supabase_key):
        # The tool passes company_id through; the orchestrator guards.
        assert company_id is None
        return {"ok": False, "reason": "initiative promotion not yet supported"}

    monkeypatch.setattr("cp_engine.spine_promote.promote_transcript", fake_promote)
    _patch_creds(monkeypatch)

    out = srv.promote_spine_transcript("storyos", "storyos-elem")

    assert out["est_item_id"] == "storyos-elem"
    assert out["promotion"] == {
        "ok": False,
        "reason": "initiative promotion not yet supported",
    }


def test_promote_no_match(monkeypatch):
    """resolve_live_element returns None → {note}; promote_transcript NOT called."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda c, pid, key: None,
    )
    called = []
    monkeypatch.setattr(
        "cp_engine.spine_promote.promote_transcript",
        lambda *a, **k: called.append(1),
    )
    _patch_creds(monkeypatch)

    out = srv.promote_spine_transcript("ibx-5192", "nope")

    assert "note" in out
    assert "nope" in out["note"]
    assert called == []


def test_promote_project_not_found(monkeypatch):
    """_resolve returns None → structured {error}."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.promote_spine_transcript("nope", "key")
    assert "error" in out
    assert "not found" in out["error"]


def test_promote_never_throws(monkeypatch):
    """If promote_transcript raises, the tool's except returns {error}, not a raise."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda c, pid, key: {"est_item_id": "e", "rel_path": "r"},
    )

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("cp_engine.spine_promote.promote_transcript", boom)
    _patch_creds(monkeypatch)

    out = srv.promote_spine_transcript("ibx-5192", "key")

    assert "error" in out
    assert "kaboom" in out["error"]
