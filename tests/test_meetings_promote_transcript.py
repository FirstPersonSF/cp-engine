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
        # Read the file while the call holds it: it is removed afterwards.
        content = None
        if args and isinstance(args[0], str) and Path(args[0]).is_file():
            content = Path(args[0]).read_text()
        self.calls.append({"args": args, "kwargs": kwargs, "content": content})
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
    assert ingest.calls[0]["content"] == (
        "[00:00:16] Drew Fiero: Oh, sorry.\n[00:00:18] Tony: No worries."
    )
    # ...and removed once the stamp is done (#393).
    assert not Path(ingest_path).exists()

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


# ---- #393: concurrent promotions of one recording ---------------------------


def _concurrent_run(fn, rows):
    """Run `fn` on each row in its own thread, released together. The fake
    ingest reads the file, waits, and reads it again: on a shared, unlocked
    path the other thread's write lands in between."""
    import threading
    import time

    seen = {}
    errors = []
    start = threading.Barrier(len(rows))

    def ingest(path, project_id, title, **_kw):
        first = Path(path).read_text()
        time.sleep(0.2)
        second = Path(path).read_text()
        seen[title] = (path, first, second)
        return {"ok": True}

    def stamp(client, *, title, file_path, **_kw):
        # The stamp must see the same file the ingest read.
        assert Path(file_path).read_text() == seen[title][1]
        return {"stamped": True, "ids": [f"asset-{title}"]}

    results = {}

    def run(row):
        try:
            start.wait()
            results[row["title"]] = fn(
                _FakeClient(), row, "p1", "co1",
                ingest=ingest, stamp=stamp, supabase_url="u", supabase_key="k",
                force=True,
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(r,)) for r in rows]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    return results, seen


def test_concurrent_promotions_of_one_recording_do_not_overwrite():
    """#393: two promotions of the SAME recording at once. Each ingest must
    read its own transcript, whole, start to finish — and both succeed."""
    row_a = _row(title="A", transcript=[_seg("Alpha text.", "Drew", "00:01")])
    row_b = _row(title="B", transcript=[_seg("Bravo text.", "Tony", "00:02")])

    results, seen = _concurrent_run(promote_meeting_transcript, [row_a, row_b])

    assert results["A"] == {"ok": True, "asset_id": "asset-A"}
    assert results["B"] == {"ok": True, "asset_id": "asset-B"}
    assert seen["A"][1:] == ("[00:01] Drew: Alpha text.",) * 2
    assert seen["B"][1:] == ("[00:02] Tony: Bravo text.",) * 2
    # Still one stable path per recording (it is rag_assets' dedup key), and
    # it is cleaned up afterwards.
    assert seen["A"][0] == seen["B"][0]
    assert not Path(seen["A"][0]).exists()


def test_concurrent_summary_embeds_of_one_recording_do_not_overwrite():
    """Same race in `embed_meeting_summary`, which shares the pattern."""
    from cp_engine.meetings import embed_meeting_summary

    def embed(client, row, project_id, _company_id, **kw):
        return embed_meeting_summary(client, row, project_id, **kw)

    row_a = _row(title="A", summary="Alpha summary.")
    row_b = _row(title="B", summary="Bravo summary.")

    results, seen = _concurrent_run(embed, [row_a, row_b])

    assert results["A"]["ok"] is True and results["B"]["ok"] is True
    assert seen["A"][1:] == ("Alpha summary.",) * 2
    assert seen["B"][1:] == ("Bravo summary.",) * 2
