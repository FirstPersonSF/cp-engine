"""Tests for `promote_meeting_transcript` + `flatten_transcript`
(meetings-as-sources).

Promoting a tagged meeting's full transcript into rag_assets mirrors
`embed_meeting_summary` but with the `meta={'kind':'meeting_transcript'}`
discriminator and an engagement-only CONTRACT A guard (initiatives deferred).

`flatten_transcript` turns the JSONB segment array into a single text blob and
is unit-tested directly. `ingest`/`stamp` are injected so the real pipeline /
Voyage / Supabase are never touched; a recorder fake client captures the
`transcript_promoted_at` UPDATE.
"""
from __future__ import annotations

from pathlib import Path

from cp_engine.meetings import flatten_transcript, promote_meeting_transcript


class _Recorder:
    """A fake ingest/stamp that records every call's kwargs/args."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {}

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self._result


class _FakeUpdateChain:
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
    def __init__(self):
        self.updates = []

    def table(self, name):
        assert name == "fathom_meetings"
        return _FakeUpdateChain(self)


def _seg(text="Hello there.", name="Drew Fiero", ts="00:00:16"):
    seg = {"text": text}
    if name is not None:
        seg["speaker"] = {"display_name": name}
    if ts is not None:
        seg["timestamp"] = ts
    return seg


def _row(**overrides):
    base = {
        "recording_id": 12345,
        "transcript": [
            _seg("Oh, sorry.", "Drew Fiero", "00:00:16"),
            _seg("No worries.", "Tony", "00:00:18"),
        ],
        "transcript_promoted_at": None,
        "title": "Kickoff",
    }
    base.update(overrides)
    return base


# ---- flatten_transcript unit tests ----------------------------------------


def test_flatten_normal_multisegment():
    out = flatten_transcript(
        [
            _seg("Oh, sorry.", "Drew Fiero", "00:00:16"),
            _seg("No worries.", "Tony", "00:00:18"),
        ]
    )
    assert out == "[00:00:16] Drew Fiero: Oh, sorry.\n[00:00:18] Tony: No worries."


def test_flatten_missing_speaker():
    out = flatten_transcript([_seg("Hi.", name=None, ts="00:01")])
    assert out == "[00:01] Unknown: Hi."


def test_flatten_missing_speaker_display_name():
    out = flatten_transcript([{"text": "Hi.", "speaker": {}, "timestamp": "00:01"}])
    assert out == "[00:01] Unknown: Hi."


def test_flatten_missing_timestamp():
    out = flatten_transcript([_seg("Hi.", "Drew", ts=None)])
    assert out == "Drew: Hi."


def test_flatten_empty_and_none():
    assert flatten_transcript([]) == ""
    assert flatten_transcript(None) == ""


def test_flatten_skips_segment_without_text():
    out = flatten_transcript(
        [
            _seg("Kept.", "Drew", "00:01"),
            {"speaker": {"display_name": "Drew"}, "timestamp": "00:02"},
            _seg("", "Drew", "00:03"),
        ]
    )
    assert out == "[00:01] Drew: Kept."


# ---- promote_meeting_transcript --------------------------------------------


def test_contract_a_initiative_no_work():
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_meeting_transcript(
        client, _row(), "p1", None,
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "initiative" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []
    assert client.updates == []


def test_guard_missing_recording_id():
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_meeting_transcript(
        client, _row(recording_id=None), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "recording_id" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []
    assert client.updates == []


def test_guard_empty_transcript():
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_meeting_transcript(
        client, _row(transcript=[]), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "no transcript" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []


def test_guard_none_transcript():
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_meeting_transcript(
        client, _row(transcript=None), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "no transcript" in result["reason"]
    assert ingest.calls == []


def test_skip_already_promoted():
    client = _FakeClient()
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_meeting_transcript(
        client, _row(transcript_promoted_at="2026-06-29T00:00:00+00:00"),
        "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "already promoted" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []
    assert client.updates == []


def test_force_repromotes():
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"]})
    result = promote_meeting_transcript(
        client, _row(transcript_promoted_at="2026-06-29T00:00:00+00:00"),
        "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
        force=True,
    )
    assert result["ok"] is True
    assert len(ingest.calls) == 1


def test_happy_path():
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"], "title": "Kickoff"})

    result = promote_meeting_transcript(
        client, _row(), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )

    assert result["ok"] is True
    assert result["asset_id"] == "a1"

    assert len(ingest.calls) == 1
    ingest_path = ingest.calls[0]["args"][0]
    assert "meeting-promote" in ingest_path
    assert "12345" in ingest_path
    assert ingest_path.endswith("transcript.md")
    assert ingest.calls[0]["args"][2] == "Kickoff"
    # Flattened text actually written there.
    assert Path(ingest_path).read_text() == (
        "[00:00:16] Drew Fiero: Oh, sorry.\n[00:00:18] Tony: No worries."
    )

    assert len(stamp.calls) == 1
    stamp_kw = stamp.calls[0]["kwargs"]
    assert stamp_kw["file_path"] == ingest_path
    assert stamp_kw["source_file_id"] == "12345"
    assert stamp_kw["meta"] == {"kind": "meeting_transcript"}

    assert len(client.updates) == 1
    upd = client.updates[0]
    assert "transcript_promoted_at" in upd["payload"]
    assert ("recording_id", 12345) in upd["filters"]


def test_default_title_when_missing():
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"]})
    promote_meeting_transcript(
        client, _row(title=None), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert ingest.calls[0]["args"][2] == "Meeting transcript"


def test_stamp_zero_match_is_failure():
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": False, "ids": []})
    result = promote_meeting_transcript(
        client, _row(), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "no row" in result["reason"]
    assert client.updates == []


def test_stamp_multi_row_is_failure():
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1", "a2"]})
    result = promote_meeting_transcript(
        client, _row(), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert result["ok"] is False
    assert "2 rows" in result["reason"]
    assert client.updates == []


def test_ingest_and_stamp_share_identical_path():
    client = _FakeClient()
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"]})
    promote_meeting_transcript(
        client, _row(), "p1", "co1",
        ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
    )
    assert ingest.calls[0]["args"][0] == stamp.calls[0]["kwargs"]["file_path"]
