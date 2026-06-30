"""Tests for the NON-FATAL meeting-link side-step in _perform_auto_ingest.

The link step is purely additive: after the per-project sprint-file loop, the
webhook calls `_link_meeting_safe` ONCE per meeting to resolve/link the meeting
to its project and embed its summary into RAG. The #1 invariant is that a
failure here NEVER aborts the primary ingest — so `_link_meeting_safe` must
never raise.

CRITICAL: `_link_meeting_safe` resolves company_id from the MEETING'S OWN
resolved project (the first resolvable `project_tags` entry), NOT from a caller
`code`. On a cross-company fan-out (sprint-planning / account meetings span
projects under different companies) resolving by `code` would, on a retag,
write the WRONG company onto the meeting's rag_assets rows. The
`test_company_id_is_meetings_project_not_caller_code` test pins this hazard.

We test the helper in ISOLATION with injected fakes (monkeypatching the seams
on `cp_engine.meetings` + `main`), not by running the whole heavy
`_perform_auto_ingest`. Mirrors the mocking style of
test_webhook_spine_promote_transcript.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # noqa: E402


class _Folders:
    """Minimal stand-in for ProjectFolders — the helper only reads .company_id."""

    def __init__(self, company_id, **_ignored):
        self.company_id = company_id


_MEETING = {"recording_id": "rec-123", "project_tags": ["IBX 5153 AI Campaign"]}


def _patch_resolve_to(monkeypatch, *, project_id, company_id):
    """Patch resolve_meeting_project → project_id and folders → company_id."""
    monkeypatch.setattr(
        "cp_engine.meetings.resolve_meeting_project",
        lambda client, tags, **kw: (project_id, (tags or [None])[0]))
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: _Folders(company_id) if pid else None)


def test_link_meeting_safe_happy_resolves_by_meeting_and_calls_link(monkeypatch):
    captured = {}

    def fake_resolve_proj(client, tags, **kw):
        captured["tags"] = tags
        return "proj-uuid-1", "IBX 5153 AI Campaign"

    monkeypatch.setattr(
        "cp_engine.meetings.resolve_meeting_project", fake_resolve_proj)

    def fake_folders(client, project_id):
        captured["folders_pid"] = project_id
        return _Folders("co-uuid-9")

    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id", fake_folders)

    def fake_link(client, meeting, *, rescope, is_engagement,
                  supabase_url, supabase_key):
        captured["link_meeting"] = meeting
        captured["rescope"] = rescope
        captured["is_engagement"] = is_engagement
        captured["supabase_url"] = supabase_url
        captured["supabase_key"] = supabase_key
        return {"ok": True, "linked": True, "project_id": "proj-uuid-1"}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)

    result = webhook_main._link_meeting_safe(
        object(), _MEETING, "https://db", "service-key")

    assert result == {"ok": True, "linked": True, "project_id": "proj-uuid-1"}
    # Resolved from the MEETING's tags, not any code.
    assert captured["tags"] == _MEETING["project_tags"]
    assert captured["folders_pid"] == "proj-uuid-1"
    assert captured["link_meeting"] is _MEETING
    assert captured["supabase_url"] == "https://db"
    assert captured["supabase_key"] == "service-key"
    assert callable(captured["rescope"])
    # Engagement (company present) → linked in engagement mode.
    assert captured["is_engagement"] is True


def test_rescope_callable_threads_company_id(monkeypatch):
    captured = {}

    _patch_resolve_to(monkeypatch, project_id="proj-uuid-1", company_id="co-uuid-9")

    def fake_link(client, meeting, *, rescope, is_engagement,
                  supabase_url, supabase_key):
        captured["rescope"] = rescope
        return {"ok": True}

    def fake_rescope_meeting(c, row, new_pid, *, new_company_id=None):
        captured["new_pid"] = new_pid
        captured["new_company_id"] = new_company_id
        return {"ok": True, "rescoped": True}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)
    monkeypatch.setattr("cp_engine.meetings.rescope_meeting", fake_rescope_meeting)

    webhook_main._link_meeting_safe(
        object(), _MEETING, "https://db", "service-key")

    # Invoke the captured rescope callable the way link_meeting would.
    out = captured["rescope"]("client", _MEETING, "new-proj")
    assert out == {"ok": True, "rescoped": True}
    assert captured["new_pid"] == "new-proj"
    assert captured["new_company_id"] == "co-uuid-9"


def test_company_id_is_meetings_project_not_caller_code(monkeypatch):
    """REGRESSION: cross-company hazard. The meeting's tags resolve to project P
    in company A. Even though the surrounding auto-ingest loop fans out across a
    code in company B, the company_id threaded into the rescope cascade MUST be
    A's (the meeting's actual project's company), never B's. _link_meeting_safe
    takes no `code` — it resolves entirely from the meeting — so the only company
    it can ever thread is the meeting's own. We assert that here."""
    captured = {}

    # The meeting resolves to project P / company A.
    monkeypatch.setattr(
        "cp_engine.meetings.resolve_meeting_project",
        lambda client, tags, **kw: ("project-P", "tag-P"))

    def fake_folders(client, project_id):
        # Only the meeting's project (P → company A) is ever looked up.
        assert project_id == "project-P"
        return _Folders("company-A")

    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id", fake_folders)

    def fake_link(client, meeting, *, rescope, is_engagement,
                  supabase_url, supabase_key):
        captured["rescope"] = rescope
        return {"ok": True, "linked": True}

    def fake_rescope_meeting(c, row, new_pid, *, new_company_id=None):
        captured["new_company_id"] = new_company_id
        return {"ok": True}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)
    monkeypatch.setattr("cp_engine.meetings.rescope_meeting", fake_rescope_meeting)

    # Signature takes no code — the hazard is structurally impossible.
    webhook_main._link_meeting_safe(object(), _MEETING, "https://db", "k")

    captured["rescope"]("c", _MEETING, "project-P")
    assert captured["new_company_id"] == "company-A"
    assert captured["new_company_id"] != "company-B"


def test_link_meeting_safe_is_non_fatal_when_link_raises(monkeypatch, caplog):
    _patch_resolve_to(monkeypatch, project_id="proj-uuid-1", company_id="co")

    def boom(*a, **k):
        raise RuntimeError("link blew up")

    monkeypatch.setattr("cp_engine.meetings.link_meeting", boom)

    with caplog.at_level("WARNING"):
        result = webhook_main._link_meeting_safe(
            object(), _MEETING, "https://db", "service-key")

    assert result is None
    assert any("meeting-link failed" in r.message for r in caplog.records)


def test_link_meeting_safe_non_fatal_when_resolve_raises(monkeypatch):
    def boom(client, tags, **kw):
        raise RuntimeError("resolve blew up")

    monkeypatch.setattr("cp_engine.meetings.resolve_meeting_project", boom)

    # Must not propagate even if resolve itself fails.
    assert webhook_main._link_meeting_safe(
        object(), _MEETING, "https://db", "k") is None


def test_link_step_defers_initiative_meeting(monkeypatch):
    """v1 DEFER: a meeting resolving to an INITIATIVE (folders/company None) must
    be passed to link_meeting in deferred mode (is_engagement=False) so NO
    FK-violating project_id write is attempted. The initiative id is not in
    `projects`, so writing fathom_meetings.project_id would violate the FK
    (migration 084). _link_meeting_safe resolves company_id (None for an
    initiative) and threads is_engagement=(company_id is not None)=False."""
    captured = {}

    monkeypatch.setattr(
        "cp_engine.meetings.resolve_meeting_project",
        lambda client, tags, **kw: ("init-uuid-1", "Mission Control"))
    # An initiative has no folders / no company.
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: None)

    def fake_link(client, meeting, *, rescope, is_engagement,
                  supabase_url, supabase_key):
        captured["is_engagement"] = is_engagement
        return {"ok": True, "linked": False,
                "reason": "initiative meetings are deferred in v1"}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)

    result = webhook_main._link_meeting_safe(
        object(), _MEETING, "https://db", "service-key")

    # link_meeting was called in DEFERRED mode — no engagement.
    assert captured["is_engagement"] is False
    assert result["linked"] is False
    assert "initiative" in result["reason"].lower()


def test_link_step_passes_is_engagement_true_for_engagement(monkeypatch):
    """An engagement (folders/company present) → is_engagement=True is threaded so
    link_meeting links + embeds exactly as before."""
    captured = {}

    _patch_resolve_to(monkeypatch, project_id="proj-uuid-1", company_id="co-uuid-9")

    def fake_link(client, meeting, *, rescope, is_engagement,
                  supabase_url, supabase_key):
        captured["is_engagement"] = is_engagement
        return {"ok": True, "linked": True, "project_id": "proj-uuid-1"}

    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)

    webhook_main._link_meeting_safe(object(), _MEETING, "https://db", "k")
    assert captured["is_engagement"] is True


class _FakeFetchChain:
    """Records the `.select(...)` column string for a fathom_meetings fetch."""

    def __init__(self, recorder, row):
        self._recorder = recorder
        self._row = row

    def select(self, cols):
        self._recorder["select_cols"] = cols
        return self

    def eq(self, *a, **k):
        return self

    def single(self):
        return self

    def execute(self):
        class _Resp:
            pass

        resp = _Resp()
        resp.data = self._row
        return resp


class _FakeFetchClient:
    def __init__(self, recorder, row):
        self._recorder = recorder
        self._row = row

    def table(self, name):
        assert name == "fathom_meetings"
        return _FakeFetchChain(self._recorder, self._row)


def test_fetch_meeting_selects_link_and_embed_columns(monkeypatch):
    """CONTRACT (fetch↔consume seam): `_fetch_meeting` must SELECT every column
    the downstream link/embed path reads off the same row. `_link_meeting_safe`
    → resolve_meeting_project(meeting["project_tags"]) → link_meeting reads
    project_id + recording_id; embed_meeting_summary reads recording_id +
    summary_embedded_at. If the select omits any of these, EVERY production
    webhook meeting resolves to no project and silently no-ops. Pins the seam
    so it can't reopen."""
    recorder: dict = {}
    fake_row = {"id": "uuid-1", "recording_id": 42,
                "project_tags": ["IBX 5153 AI Campaign"], "project_id": None,
                "summary_embedded_at": None}

    monkeypatch.setenv("SUPABASE_URL", "https://db")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
    # `_fetch_meeting` does `from supabase import create_client` then
    # create_client(url, key) — patch the factory to hand back our fake client.
    import supabase as _supabase
    monkeypatch.setattr(
        _supabase, "create_client",
        lambda url, key: _FakeFetchClient(recorder, fake_row))

    result = webhook_main._fetch_meeting("uuid-1")
    assert result is fake_row

    cols = recorder["select_cols"]
    # Columns the link/embed path consumes — the seam this test guards.
    for needed in ("recording_id", "project_tags", "project_id",
                   "summary_embedded_at"):
        assert needed in cols, f"_fetch_meeting select missing {needed!r}: {cols}"
    # Pre-existing consumers (artifact write + action_items merge) preserved.
    for kept in ("summary", "action_items", "title"):
        assert kept in cols, f"_fetch_meeting select dropped {kept!r}: {cols}"


def test_link_step_skips_untagged_meeting(monkeypatch):
    """Untagged / unresolvable meeting: no folder lookup, no link call — pure skip."""
    calls = {"folders": 0, "link": 0}

    monkeypatch.setattr(
        "cp_engine.meetings.resolve_meeting_project",
        lambda client, tags, **kw: (None, None))

    def fake_folders(client, pid):
        calls["folders"] += 1
        return _Folders("co")

    def fake_link(*a, **k):
        calls["link"] += 1
        return {"ok": True}

    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id", fake_folders)
    monkeypatch.setattr("cp_engine.meetings.link_meeting", fake_link)

    result = webhook_main._link_meeting_safe(
        object(), _MEETING, "https://db", "k")

    assert result == {"ok": True, "linked": False,
                      "reason": "no resolvable project tag"}
    assert calls["folders"] == 0  # no company lookup on the skip path
    assert calls["link"] == 0     # no link call
