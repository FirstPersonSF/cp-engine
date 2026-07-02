"""Tests for meeting_synthesis (the deep `meeting_synthesis` fidelity).

Covers the four new units, all with injected seams so no network / Voyage /
Supabase is touched:
  - discover_recording  — best-candidate media matching in a project folder
  - call_synth_service  — /api/analyze POST + poll, via an injected http client
  - synthesis_to_markdown — render a MeetingSynthesis dict to source text
  - synthesize_meeting  — orchestration mirroring promote_meeting_transcript
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from cp_engine.meeting_synthesis import (
    call_synth_service,
    discover_recording,
    synthesis_to_markdown,
    synthesize_meeting,
)


# --------------------------------------------------------------------------- #
# fakes (mirror test_meetings_promote_transcript)
# --------------------------------------------------------------------------- #


class _Recorder:
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
        self._recorder.updates.append({"payload": self._payload, "filters": self._filters})
        return SimpleNamespace(data=[{"id": "ignored"}])


class _FakeClient:
    def __init__(self):
        self.updates = []

    def table(self, name):
        assert name == "fathom_meetings"
        return _FakeUpdateChain(self)


@dataclass
class _File:
    name: str
    modified: str | None = None
    source: str = "dropbox"
    id: str = "x"
    path: str | None = None


# --------------------------------------------------------------------------- #
# discover_recording
# --------------------------------------------------------------------------- #


def test_discover_prefers_date_then_title():
    files = [
        _File(name="random.mp4", modified="2026-05-01T00:00:00Z"),
        _File(name="srs-platform-pitch.mp4", modified="2026-06-24T10:00:00Z"),
        _File(name="notes.pdf", modified="2026-06-24T10:00:00Z"),  # not video
    ]
    meeting = {"meeting_date": "2026-06-24T09:35:00+00:00", "title": "SRS Platform Pitch"}
    out = discover_recording(files, meeting)
    assert out["best"].name == "srs-platform-pitch.mp4"
    # the pdf is excluded (not video); both mp4s are candidates
    assert len(out["candidates"]) == 2


def test_discover_no_video_returns_none():
    files = [_File(name="deck.pdf"), _File(name="board.png")]
    out = discover_recording(files, {"title": "x"})
    assert out["best"] is None and out["candidates"] == []


# --------------------------------------------------------------------------- #
# call_synth_service (injected http)
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttp:
    def __init__(self, result_sequence):
        self.posts = []
        self.gets = []
        self._results = list(result_sequence)

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        return _FakeResp(200, {"job_id": "job123"})

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return self._results.pop(0)


def test_call_synth_service_posts_and_polls(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    http = _FakeHttp([
        _FakeResp(409, {"detail": "Job status: running"}),      # not ready
        _FakeResp(200, {"synthesis": {"tldr": "done"}}),        # ready
    ])
    out = call_synth_service(
        media_url="https://x/v.mp4",
        title="T",
        documents=[{"title": "Deck", "pdf_b64": "AAA"}],
        api_key="k",
        http=http,
        poll_interval=0,
    )
    assert out["synthesis"]["tldr"] == "done"
    # posted with x-api-key + media_url + json documents
    p = http.posts[0]
    assert p["headers"]["x-api-key"] == "k"
    assert p["data"]["media_url"] == "https://x/v.mp4"
    assert "Deck" in p["data"]["documents"]
    assert len(http.gets) == 2  # polled twice


def test_call_synth_service_requires_api_key(monkeypatch):
    monkeypatch.delenv("SYNTH_SERVICE_API_KEY", raising=False)
    try:
        call_synth_service(media_url="x", http=_FakeHttp([]))
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "API_KEY" in str(e)


def test_call_synth_service_raises_on_failed_job(monkeypatch):
    # 409 with a "failed" status must stop polling and surface the failure,
    # not spin until timeout.
    monkeypatch.setattr("time.sleep", lambda *_: None)
    http = _FakeHttp([
        _FakeResp(409, {"detail": "Job status: running"}),   # still going
        _FakeResp(409, {"detail": "Job status: failed"}),    # terminal failure
    ])
    try:
        call_synth_service(media_url="x", api_key="k", http=http, poll_interval=0)
        assert False, "expected RuntimeError on failed job"
    except RuntimeError as e:
        assert "failed" in str(e).lower()


# --------------------------------------------------------------------------- #
# synthesis_to_markdown
# --------------------------------------------------------------------------- #


def test_markdown_includes_synthesis_and_documents():
    synth = {
        "source": {"title": "SRS Pitch"},
        "synthesis": {"tldr": "We aligned.", "narrative": "It moved.", "tensions": ["A vs B"]},
        "documents": {"alignments": ["deck matched"], "deck_only": ["slide 4"]},
    }
    md = synthesis_to_markdown(synth)
    assert "# SRS Pitch" in md
    assert "## TL;DR" in md and "We aligned." in md
    assert "A vs B" in md
    assert "Documents presented" in md
    assert "deck matched" in md and "slide 4" in md


# --------------------------------------------------------------------------- #
# synthesize_meeting orchestration
# --------------------------------------------------------------------------- #


def _meeting(**over):
    row = {"recording_id": 999, "title": "SRS", "transcript": [{"text": "hi"}]}
    row.update(over)
    return row


def test_synthesize_initiative_gated_first():
    # company_id None → deferred, no synth call at all
    synth = _Recorder()
    out = synthesize_meeting(
        _FakeClient(), _meeting(), "proj", None,
        media_url="u", synth_call=synth, ingest=_Recorder(), stamp=_Recorder(),
        supabase_url="", supabase_key="",
    )
    assert out == {"ok": False, "reason": "initiative synthesis not yet supported"}
    assert synth.calls == []


def test_synthesize_already_generated_skips():
    synth = _Recorder()
    out = synthesize_meeting(
        _FakeClient(), _meeting(synthesis_generated_at="2026-07-01T00:00:00Z"), "proj", "co",
        media_url="u", synth_call=synth, ingest=_Recorder(), stamp=_Recorder(),
        supabase_url="", supabase_key="",
    )
    assert out["ok"] is False and "already" in out["reason"]
    assert synth.calls == []


def test_synthesize_happy_path_stamps_and_writes_back():
    client = _FakeClient()
    synth = _Recorder(result={"source": {"title": "SRS"}, "synthesis": {"tldr": "ok"}})
    ingest = _Recorder()
    stamp = _Recorder(result={"stamped": True, "ids": ["asset-1"]})

    out = synthesize_meeting(
        client, _meeting(), "proj", "co",
        media_url="https://x/v.mp4",
        synth_call=synth, ingest=ingest, stamp=stamp,
        supabase_url="U", supabase_key="K",
    )
    assert out == {"ok": True, "asset_id": "asset-1"}
    # ingest got the stable synthesis path; stamp got meeting_synthesis kind
    assert stamp.calls[0]["kwargs"]["meta"] == {"kind": "meeting_synthesis"}
    # synthesis_generated_at written back, located by recording_id
    assert client.updates[0]["payload"].keys() == {"synthesis_generated_at"}
    assert ("recording_id", 999) in client.updates[0]["filters"]


def test_synthesize_stamp_zero_rows_is_reason_not_crash():
    synth = _Recorder(result={"synthesis": {"tldr": "ok"}, "source": {"title": "t"}})
    out = synthesize_meeting(
        _FakeClient(), _meeting(), "proj", "co",
        media_url="u", synth_call=synth, ingest=_Recorder(),
        stamp=_Recorder(result={"stamped": False, "ids": []}),
        supabase_url="U", supabase_key="K",
    )
    assert out["ok"] is False and out["reason"] == "stamp matched no row"


def test_synthesize_service_error_wrapped_not_raised():
    def boom(**kw):
        raise RuntimeError("service 500")

    out = synthesize_meeting(
        _FakeClient(), _meeting(), "proj", "co",
        media_url="u", synth_call=boom, ingest=_Recorder(), stamp=_Recorder(),
        supabase_url="U", supabase_key="K",
    )
    assert out["ok"] is False and "service 500" in out["reason"]
