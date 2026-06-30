"""Tests for `embed_meeting_summary` (meetings-as-sources).

Embedding a tagged meeting's summary into rag_assets ties together:
  - skip guards (no summary / already embedded / no recording_id) that do NO
    ingest/stamp work,
  - the stamp↔ingest seam (ONE stable file_path keyed on recording_id passed to
    BOTH ingest and stamp, then verified exactly-one-row before ok:True),
  - the `meta={'kind':'meeting_summary'}` discriminator on the stamped row,
  - the `fathom_meetings.summary_embedded_at` write-back.

`ingest` and `stamp` are injected so the real ingest pipeline / Voyage / Supabase
are never touched. A recorder fake client captures the summary_embedded_at UPDATE.
"""
from __future__ import annotations

from pathlib import Path

from cp_engine.meetings import embed_meeting_summary


class _Recorder:
    """A fake ingest/stamp that records every call's kwargs/args."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {}

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
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

        class _Resp:
            data = [{"id": "ignored"}]

        return _Resp()


class _FakeClient:
    """Captures fathom_meetings UPDATE calls; no real Supabase touched."""

    def __init__(self):
        self.updates = []

    def table(self, name):
        assert name == "fathom_meetings"
        return _FakeUpdateChain(self)


def _row(**overrides):
    base = {
        "recording_id": 12345,
        "summary": "We agreed to ship the thing.",
        "summary_embedded_at": None,
        "title": "Kickoff",
    }
    base.update(overrides)
    return base


def test_happy_path(tmp_path):
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"], "title": "Kickoff"})

    result = embed_meeting_summary(
        client,
        _row(),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )

    assert result["ok"] is True
    assert result["asset_id"] == "a1"

    # ingest called exactly once, with title and the stable path.
    assert len(ingest.calls) == 1
    ingest_path = ingest.calls[0]["args"][0]
    assert "meeting-embed" in ingest_path
    assert "12345" in ingest_path
    assert ingest_path.endswith("summary.md")
    assert ingest.calls[0]["args"][2] == "Kickoff"
    # The summary text was actually written there.
    assert Path(ingest_path).read_text() == "We agreed to ship the thing."

    # stamp got the IDENTICAL file_path + the right provenance.
    assert len(stamp.calls) == 1
    stamp_kw = stamp.calls[0]["kwargs"]
    assert stamp_kw["file_path"] == ingest_path
    assert stamp_kw["source_file_id"] == "12345"
    assert stamp_kw["meta"] == {"kind": "meeting_summary"}

    # summary_embedded_at written back, located by recording_id.
    assert len(client.updates) == 1
    upd = client.updates[0]
    assert "summary_embedded_at" in upd["payload"]
    assert ("recording_id", 12345) in upd["filters"]


def test_skip_empty_summary(tmp_path):
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = embed_meeting_summary(
        client,
        _row(summary="   "),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )
    assert result["ok"] is False
    assert "no summary" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []
    assert client.updates == []


def test_skip_already_embedded(tmp_path):
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = embed_meeting_summary(
        client,
        _row(summary_embedded_at="2026-06-29T00:00:00+00:00"),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )
    assert result["ok"] is False
    assert "already embedded" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []
    assert client.updates == []


def test_force_reembeds_when_already_embedded(tmp_path):
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"]})
    result = embed_meeting_summary(
        client,
        _row(summary_embedded_at="2026-06-29T00:00:00+00:00"),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
        force=True,
    )
    assert result["ok"] is True
    assert len(ingest.calls) == 1


def test_guard_missing_recording_id(tmp_path):
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = embed_meeting_summary(
        client,
        _row(recording_id=None),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )
    assert result["ok"] is False
    assert "recording_id" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []
    assert client.updates == []


def test_stamp_zero_match_is_failure(tmp_path):
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": False, "ids": []})
    result = embed_meeting_summary(
        client,
        _row(),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )
    assert result["ok"] is False
    assert "no row" in result["reason"]
    # No write-back when stamp failed.
    assert client.updates == []


def test_stamp_multi_row_is_failure(tmp_path):
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1", "a2"]})
    result = embed_meeting_summary(
        client,
        _row(),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )
    assert result["ok"] is False
    assert "2 rows" in result["reason"]
    assert client.updates == []


def test_ingest_and_stamp_share_identical_path(tmp_path):
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"]})
    embed_meeting_summary(
        client,
        _row(),
        "p1",
        ingest=ingest,
        stamp=stamp,
        supabase_url="u",
        supabase_key="k",
    )
    assert ingest.calls[0]["args"][0] == stamp.calls[0]["kwargs"]["file_path"]
