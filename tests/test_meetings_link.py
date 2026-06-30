"""Tests for `link_meeting` (meetings-as-sources).

`link_meeting` is the per-meeting orchestrator the webhook flow calls. It ties
together: resolve project tags → (optionally) assign a work item → write the
link columns on `fathom_meetings` → embed the summary → and, if this is a RETAG
(stored project changed), trigger a re-scope cascade.

Two collaborators are INJECTED SEAMS with simple defaults (assigner / rescope);
the real implementations are later tasks. The resolver is monkeypatched at the
module-level name `cp_engine.meetings.resolve_meeting_project` so we control
project resolution without a real client. `embed` is injected as a recorder so
the real ingest pipeline / Voyage / Supabase are never touched. A recorder fake
client captures the fathom_meetings UPDATE that writes the link columns.
"""
from __future__ import annotations

import cp_engine.meetings as meetings
from cp_engine.meetings import link_meeting


class _Recorder:
    """A fake embed/assigner/rescope that records every call's args/kwargs."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result if result is not None else {}
        self._raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeUpdateChain:
    """Records a `.table(...).update(...).eq(...)...execute()` chain."""

    def __init__(self, recorder):
        self._recorder = recorder
        self._payload = None
        self._filters = []

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def execute(self):
        self._recorder.updates.append(
            {"payload": self._payload, "filters": self._filters}
        )
        self._recorder.events.append("update")

        class _Resp:
            data = [{"id": "ignored"}]

        return _Resp()


class _FakeClient:
    """Captures fathom_meetings UPDATE calls; no real Supabase touched.

    `events` is a shared ordered log so tests can assert relative ordering of
    the link-column write against other side effects (e.g. the rescope cascade).
    """

    def __init__(self, events=None):
        self.updates = []
        self.events = events if events is not None else []

    def table(self, name):
        assert name == "fathom_meetings"
        return _FakeUpdateChain(self)


def _row(**overrides):
    base = {
        "recording_id": 12345,
        "project_tags": ["IBX 5153 AI Campaign"],
        "project_id": None,
        "summary": "We agreed to ship the thing.",
        "summary_embedded_at": None,
        "title": "Kickoff",
    }
    base.update(overrides)
    return base


def _patch_resolver(monkeypatch, project_id, matched_tag="IBX 5153 AI Campaign"):
    def _fake(client, project_tags, **kwargs):
        return project_id, matched_tag

    monkeypatch.setattr(meetings, "resolve_meeting_project", _fake)


def test_tagged_first_time_link(monkeypatch):
    _patch_resolver(monkeypatch, "p1")
    client = _FakeClient()
    embed = _Recorder({"ok": True, "asset_id": "a1"})
    rescope = _Recorder({"ok": True, "rescoped": False})

    result = link_meeting(
        client,
        _row(project_id=None),
        embed=embed,
        rescope=rescope,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is True
    assert result["linked"] is True
    assert result["project_id"] == "p1"
    assert result["matched_tag"] == "IBX 5153 AI Campaign"
    assert result["embed"] == {"ok": True, "asset_id": "a1"}

    # embed called exactly once with the resolved project_id.
    assert len(embed.calls) == 1
    assert embed.calls[0]["args"][2] == "p1"

    # NO rescope on a first-time link.
    assert rescope.calls == []

    # link columns written by recording_id.
    assert len(client.updates) == 1
    upd = client.updates[0]
    assert upd["payload"]["project_id"] == "p1"
    assert ("recording_id", 12345) in upd["filters"]


def test_untagged_unresolvable(monkeypatch):
    _patch_resolver(monkeypatch, None, matched_tag=None)
    client = _FakeClient()
    embed = _Recorder({"ok": True})
    rescope = _Recorder({"ok": True, "rescoped": False})

    result = link_meeting(
        client,
        _row(project_tags=[], project_id=None),
        embed=embed,
        rescope=rescope,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is True
    assert result["linked"] is False
    assert "no resolvable project tag" in result["reason"]
    # NO embed, NO link write, NO rescope.
    assert embed.calls == []
    assert rescope.calls == []
    assert client.updates == []


def test_retag_triggers_rescope(monkeypatch):
    _patch_resolver(monkeypatch, "p2")
    events = []
    client = _FakeClient(events=events)
    embed = _Recorder({"ok": True, "asset_id": "a1"})

    # rescope appends to the SAME ordered events log as the client's update.
    class _Rescope:
        def __init__(self):
            self.calls = []

        def __call__(self, *args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            events.append("rescope")
            return {"ok": True, "rescoped": True}

    rescope = _Rescope()

    row = _row(project_id="p1")  # stored p1, resolves to p2 → retag
    result = link_meeting(
        client,
        row,
        embed=embed,
        rescope=rescope,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is True
    assert result["linked"] is True
    # rescope called once with (client, meeting_row, new_project_id).
    assert len(rescope.calls) == 1
    assert rescope.calls[0]["args"] == (client, row, "p2")
    assert result["rescope"] == {"ok": True, "rescoped": True}
    # embed still happens.
    assert len(embed.calls) == 1
    # ORDERING: rescope cascade runs BEFORE the link-column write.
    assert events.index("rescope") < events.index("update")


def test_retag_assigns_new_project_work_item_never_stale(monkeypatch):
    """A retag (p1→p2) with a non-default assigner: the write payload carries
    the NEW project + the assigner's NEW work item — never a carried/stale old
    value. Locks in that assigner output (against new_project_id) unconditionally
    overwrites the link columns."""
    _patch_resolver(monkeypatch, "p2")
    client = _FakeClient()
    embed = _Recorder({"ok": True})
    assigner = _Recorder(("wi-new", 0.9))

    result = link_meeting(
        client,
        _row(project_id="p1", work_item_id="wi-old"),  # stale old link present
        assigner=assigner,
        embed=embed,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["project_id"] == "p2"
    assert result["work_item_id"] == "wi-new"
    upd = client.updates[0]
    assert upd["payload"]["project_id"] == "p2"
    assert upd["payload"]["work_item_id"] == "wi-new"
    assert upd["payload"]["work_item_confidence"] == 0.9


def test_guard_missing_recording_id_when_about_to_write(monkeypatch):
    """A tagged meeting with a falsy recording_id must fail BEFORE any write —
    otherwise `.eq("recording_id", None)` is a silent no-op / broad match.
    Mirrors the sibling guards in embed_meeting_summary / promote_transcript."""
    _patch_resolver(monkeypatch, "p1")
    client = _FakeClient()
    embed = _Recorder({"ok": True})
    rescope = _Recorder({"ok": True, "rescoped": False})

    result = link_meeting(
        client,
        _row(recording_id=None),  # tagged (resolves to p1) but no recording_id
        embed=embed,
        rescope=rescope,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is False
    assert "recording_id" in result["reason"]
    # No write attempted, no embed, no rescope.
    assert client.updates == []
    assert embed.calls == []
    assert rescope.calls == []


def test_same_project_relink_no_rescope(monkeypatch):
    _patch_resolver(monkeypatch, "p1")
    client = _FakeClient()
    embed = _Recorder({"ok": True})
    rescope = _Recorder({"ok": True, "rescoped": False})

    result = link_meeting(
        client,
        _row(project_id="p1"),  # stored == resolved → NOT a retag
        embed=embed,
        rescope=rescope,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is True
    assert result["linked"] is True
    # NO rescope (not a retag); embed still called.
    assert rescope.calls == []
    assert len(embed.calls) == 1


def test_work_item_assign(monkeypatch):
    _patch_resolver(monkeypatch, "p1")
    client = _FakeClient()
    embed = _Recorder({"ok": True})
    assigner = _Recorder(("wi-1", 0.9))

    result = link_meeting(
        client,
        _row(),
        assigner=assigner,
        embed=embed,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["work_item_id"] == "wi-1"
    assert result["confidence"] == 0.9
    upd = client.updates[0]
    assert upd["payload"]["work_item_id"] == "wi-1"
    assert upd["payload"]["work_item_confidence"] == 0.9


def test_default_assigner_none(monkeypatch):
    _patch_resolver(monkeypatch, "p1")
    client = _FakeClient()
    embed = _Recorder({"ok": True})

    result = link_meeting(
        client,
        _row(),
        embed=embed,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["work_item_id"] is None
    assert result["confidence"] is None
    upd = client.updates[0]
    assert upd["payload"]["work_item_id"] is None
    assert upd["payload"]["work_item_confidence"] is None


def test_never_raises_when_embed_raises(monkeypatch):
    _patch_resolver(monkeypatch, "p1")
    client = _FakeClient()
    embed = _Recorder(raises=RuntimeError("voyage exploded"))

    result = link_meeting(
        client,
        _row(),
        embed=embed,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is False
    assert "voyage exploded" in result["reason"]
