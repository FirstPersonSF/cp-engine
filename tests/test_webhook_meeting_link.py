"""Tests for the NON-FATAL meeting-link side-step in _perform_auto_ingest.

The link step is purely additive: after the per-project sprint-file ingest for
a `code`, the webhook also calls `_link_meeting_safe` to resolve/link the
meeting to its project and embed its summary into RAG. The #1 invariant is that
a failure here NEVER aborts the primary ingest — so `_link_meeting_safe` must
never raise.

We test the helper in ISOLATION with injected fakes (monkeypatching the seams
on `main`), not by running the whole heavy `_perform_auto_ingest`. Mirrors the
mocking style of test_webhook_spine_promote_transcript.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # noqa: E402


class _Folders:
    """Minimal stand-in for ProjectFolders — the helper only reads .company_id."""

    def __init__(self, company_id, **_ignored):
        self.company_id = company_id


_MEETING = {"recording_id": "rec-123", "project_tags": ["IBX 5153 AI Campaign"]}


def test_link_meeting_safe_happy_resolves_company_and_calls_link(monkeypatch):
    captured = {}

    def fake_resolve(client, code):
        captured["code"] = code
        return "proj-uuid-1"

    def fake_folders(client, project_id):
        captured["folders_pid"] = project_id
        return _Folders(
            company_id="co-uuid-9", project_id=project_id,
            drive_folder_id=None, dropbox_folder_id=None,
        )

    def fake_link(client, meeting, *, rescope, supabase_url, supabase_key):
        captured["link_meeting"] = meeting
        captured["rescope"] = rescope
        captured["supabase_url"] = supabase_url
        captured["supabase_key"] = supabase_key
        return {"ok": True, "linked": True, "project_id": "proj-uuid-1"}

    monkeypatch.setattr(webhook_main, "_resolve_project_id_for_promote", fake_resolve)
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id", fake_folders)
    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)

    result = webhook_main._link_meeting_safe(
        object(), _MEETING, "ibx-5153", "https://db", "service-key")

    assert result == {"ok": True, "linked": True, "project_id": "proj-uuid-1"}
    assert captured["code"] == "ibx-5153"
    assert captured["folders_pid"] == "proj-uuid-1"
    assert captured["link_meeting"] is _MEETING
    assert captured["supabase_url"] == "https://db"
    assert captured["supabase_key"] == "service-key"
    assert callable(captured["rescope"])


def test_rescope_callable_threads_company_id(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        webhook_main, "_resolve_project_id_for_promote",
        lambda client, code: "proj-uuid-1")
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: _Folders(
            company_id="co-uuid-9", project_id=pid,
            drive_folder_id=None, dropbox_folder_id=None))

    def fake_link(client, meeting, *, rescope, supabase_url, supabase_key):
        captured["rescope"] = rescope
        return {"ok": True}

    def fake_rescope_meeting(c, row, new_pid, *, new_company_id=None):
        captured["new_pid"] = new_pid
        captured["new_company_id"] = new_company_id
        return {"ok": True, "rescoped": True}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)
    monkeypatch.setattr("cp_engine.meetings.rescope_meeting", fake_rescope_meeting)

    webhook_main._link_meeting_safe(
        object(), _MEETING, "ibx-5153", "https://db", "service-key")

    # Invoke the captured rescope callable the way link_meeting would.
    out = captured["rescope"]("client", _MEETING, "new-proj")
    assert out == {"ok": True, "rescoped": True}
    assert captured["new_pid"] == "new-proj"
    assert captured["new_company_id"] == "co-uuid-9"


def test_link_meeting_safe_is_non_fatal_when_link_raises(monkeypatch, caplog):
    monkeypatch.setattr(
        webhook_main, "_resolve_project_id_for_promote",
        lambda client, code: "proj-uuid-1")
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: _Folders(
            company_id="co", project_id=pid,
            drive_folder_id=None, dropbox_folder_id=None))

    def boom(*a, **k):
        raise RuntimeError("link blew up")

    monkeypatch.setattr("cp_engine.meetings.link_meeting", boom)

    with caplog.at_level("WARNING"):
        result = webhook_main._link_meeting_safe(
            object(), _MEETING, "ibx-5153", "https://db", "service-key")

    assert result is None
    assert any("meeting-link failed" in r.message for r in caplog.records)


def test_link_meeting_safe_non_fatal_when_resolve_raises(monkeypatch):
    def boom(client, code):
        raise RuntimeError("resolve blew up")

    monkeypatch.setattr(webhook_main, "_resolve_project_id_for_promote", boom)

    # Must not propagate even if resolve itself fails.
    assert webhook_main._link_meeting_safe(
        object(), _MEETING, "ibx-5153", "https://db", "k") is None


def test_link_step_handles_unresolved_project(monkeypatch):
    """When the code can't resolve, company_id is None and link is still called
    (link_meeting resolves the meeting's own tags, not `code`)."""
    captured = {}

    monkeypatch.setattr(
        webhook_main, "_resolve_project_id_for_promote",
        lambda client, code: None)

    def fake_link(client, meeting, *, rescope, supabase_url, supabase_key):
        captured["called"] = True
        # Exercise the rescope callable to confirm company_id threads as None.
        captured["rescope"] = rescope
        return {"ok": True, "linked": False}

    rescope_calls = {}

    def fake_rescope_meeting(c, row, new_pid, *, new_company_id=None):
        rescope_calls["new_company_id"] = new_company_id
        return {"ok": True}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)
    monkeypatch.setattr("cp_engine.meetings.rescope_meeting", fake_rescope_meeting)

    result = webhook_main._link_meeting_safe(
        object(), _MEETING, "ibx-5153", "https://db", "k")

    assert result == {"ok": True, "linked": False}
    assert captured["called"]
    captured["rescope"]("c", _MEETING, "p")
    assert rescope_calls["new_company_id"] is None
