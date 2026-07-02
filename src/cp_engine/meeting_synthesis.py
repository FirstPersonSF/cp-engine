"""Deep multimodal meeting synthesis — the ``meeting_synthesis`` RAG fidelity.

The third fidelity above ``meeting_summary`` (cheap, always) and
``meeting_transcript`` (★ on-demand): a deep synthesis produced by the
meeting-synthesizer service (``meeting-synth.1p.is``), which reads the meeting's
VIDEO (slides/whiteboards off the frames) — and, when supplied, the DOCUMENTS
presented in the meeting — a transcript-only synthesis fundamentally cannot.

Design: ``meeting-synthesizer/docs/pipeline-integration-design.md`` §2. This
module is the cp-engine half: it (a) auto-discovers the recording in the
project's mc-2 ingest folder, (b) calls the synth service (X-API-Key), (c) writes
the synthesis into rag_assets as ``meta.kind = meeting_synthesis`` and stamps
``fathom_meetings.synthesis_generated_at``.

Mirrors ``meetings.promote_meeting_transcript`` (engagement gate → guard → produce
text → ingest → stamp → verify-one → write-back → never-raises).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import httpx

from cp_engine.asset_ingest import _utc_now_iso
from cp_engine.meetings import _safe, stamp_meeting_asset
from cp_engine.spine_promote import ingest_single_file

# The deployed synthesizer service (Cloudflare Worker → Railway). Overridable for
# tests / alternate deploys.
DEFAULT_SYNTH_BASE_URL = "https://meeting-synth.1p.is"

# Video-ish extensions we'd hand the synthesizer as the meeting recording.
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")


# --------------------------------------------------------------------------- #
# Media auto-discovery (§4b Q1: find the recording in the project ingest folder)
# --------------------------------------------------------------------------- #


def discover_recording(files, meeting_row) -> dict:
    """Pick the best candidate media file for a meeting from a project's folder.

    Returns ``{"best": FileRef|None, "candidates": [FileRef, ...]}``. Video files
    only; ranked by closeness of the file's ``modified`` date to the meeting date,
    then by title token overlap. The caller confirms/overrides — we never silently
    auto-pick as if certain (a Fathom recording in Dropbox isn't named after the
    meeting).
    """
    candidates = [
        f for f in files
        if (f.name or "").lower().endswith(_VIDEO_EXTS)
    ]
    if not candidates:
        return {"best": None, "candidates": []}

    meeting_date = (meeting_row.get("meeting_date") or "")[:10]
    title_tokens = {t for t in _safe(meeting_row.get("title") or "").split("-") if len(t) > 3}

    def score(f) -> tuple:
        # lower is better: (date distance proxy, -title overlap)
        fdate = (f.modified or "")[:10]
        date_match = 0 if (meeting_date and fdate == meeting_date) else 1
        fname_tokens = {t for t in _safe(f.name or "").split("-") if len(t) > 3}
        overlap = len(title_tokens & fname_tokens)
        return (date_match, -overlap)

    ranked = sorted(candidates, key=score)
    return {"best": ranked[0], "candidates": ranked}


# --------------------------------------------------------------------------- #
# Synthesizer-service client
# --------------------------------------------------------------------------- #


def call_synth_service(
    *,
    media_url: str | None = None,
    supplied_transcript=None,
    documents=None,
    title: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    poll_interval: float = 5.0,
    timeout_seconds: float = 1800.0,
    http=httpx,
) -> dict:
    """Call meeting-synth ``/api/analyze`` and poll to completion.

    Returns the ``MeetingSynthesis`` dict. Raises on transport/timeout/failure —
    the caller (``synthesize_meeting``) wraps into an ok/reason result.

    ``http`` is injectable (httpx by default) so tests never hit the network.
    """
    base_url = (base_url or os.environ.get("SYNTH_SERVICE_URL") or DEFAULT_SYNTH_BASE_URL).rstrip("/")
    api_key = api_key or os.environ.get("SYNTH_SERVICE_API_KEY")
    if not api_key:
        raise RuntimeError("SYNTH_SERVICE_API_KEY not set (service X-API-Key bypass)")
    if not media_url and not supplied_transcript:
        raise RuntimeError("call_synth_service needs media_url or supplied_transcript")

    headers = {"x-api-key": api_key}
    form: dict = {}
    if media_url:
        form["media_url"] = media_url
    if title:
        form["title"] = title
    if supplied_transcript is not None:
        form["supplied_transcript"] = json.dumps(supplied_transcript)
    if documents:
        form["documents"] = json.dumps(documents)

    start = http.post(f"{base_url}/api/analyze", data=form, headers=headers, timeout=60.0)
    start.raise_for_status()
    job_id = start.json()["job_id"]

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        r = http.get(f"{base_url}/api/analyze/{job_id}/result", headers=headers, timeout=60.0)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 400:
            # not ready yet (endpoint returns 400 until completed)
            time.sleep(poll_interval)
            continue
        r.raise_for_status()
        time.sleep(poll_interval)
    raise TimeoutError(f"synth job {job_id} did not complete within {timeout_seconds}s")


# --------------------------------------------------------------------------- #
# Synthesis → markdown (what lands in rag_assets + the meetings/ artifact)
# --------------------------------------------------------------------------- #


def synthesis_to_markdown(synth: dict) -> str:
    """Render a MeetingSynthesis dict to the durable markdown source text."""
    s = synth.get("synthesis") or {}
    lines: list[str] = []
    src = synth.get("source") or {}
    lines.append(f"# {src.get('title') or 'Meeting synthesis'}")
    lines.append("")
    if s.get("tldr"):
        lines += ["## TL;DR", s["tldr"], ""]
    if s.get("narrative"):
        lines += ["## Narrative", s["narrative"], ""]
    for key, header in (
        ("tensions", "Tensions"),
        ("unresolved", "Unresolved"),
        ("follow_ups", "Follow-ups"),
    ):
        items = s.get(key) or []
        if items:
            lines.append(f"## {header}")
            lines += [f"- {i}" for i in items]
            lines.append("")
    docs = synth.get("documents")
    if docs:
        lines.append("## Documents presented — reconciled against the room")
        for key, header in (
            ("alignments", "Affirmed by the room"),
            ("contradictions", "Contradicted by the discussion"),
            ("deck_only", "Presented but not discussed"),
            ("room_only", "Discussed but on no document"),
        ):
            items = docs.get(key) or []
            if items:
                lines.append(f"### {header}")
                lines += [f"- {i}" for i in items]
                lines.append("")
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------- #
# Orchestration — mirrors promote_meeting_transcript
# --------------------------------------------------------------------------- #


def synthesize_meeting(
    client,
    meeting_row: dict,
    project_id: str,
    company_id: str | None,
    *,
    media_url: str | None = None,
    documents=None,
    supplied_transcript=None,
    ingest=ingest_single_file,
    stamp=stamp_meeting_asset,
    synth_call=call_synth_service,
    supabase_url: str,
    supabase_key: str,
    force: bool = False,
) -> dict:
    """Produce + store a deep meeting synthesis for ``project_id``.

    Returns ``{"ok": True, "asset_id": ...}`` or ``{"ok": False, "reason": ...}``.
    NEVER raises.

    CONTRACT A — engagement gate first: initiatives (``company_id is None``) are
    deferred in v1 (same as promote), before any service call.

    Skip guards: missing recording_id; already-synthesized (unless ``force``);
    neither media_url nor supplied_transcript to work from.
    """
    try:
        if company_id is None:
            return {"ok": False, "reason": "initiative synthesis not yet supported"}

        recording_id = meeting_row.get("recording_id")
        if not recording_id:
            return {"ok": False, "reason": "meeting has no recording_id"}

        if meeting_row.get("synthesis_generated_at") and not force:
            return {"ok": False, "reason": "synthesis already generated"}

        if not media_url and not supplied_transcript:
            return {"ok": False, "reason": "no media_url or transcript to synthesize"}

        title = meeting_row.get("title") or "Meeting synthesis"

        synth = synth_call(
            media_url=media_url,
            supplied_transcript=supplied_transcript,
            documents=documents,
            title=title,
        )

        text = synthesis_to_markdown(synth)
        if not text.strip():
            return {"ok": False, "reason": "synthesis produced no text"}

        # STABLE dest path keyed on recording_id → re-synthesis lands on the same
        # rag_assets row (idempotent), mirroring promote_meeting_transcript.
        dest_dir = Path(tempfile.gettempdir()) / "meeting-synthesis" / _safe(recording_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "synthesis.md"
        dest.write_text(text, encoding="utf-8")
        stable_path = str(dest)

        ingest(stable_path, project_id, title,
               supabase_url=supabase_url, supabase_key=supabase_key)

        st = stamp(client, project_id=project_id, source_file_id=str(recording_id),
                   title=title, file_path=stable_path,
                   meta={"kind": "meeting_synthesis"})
        ids = st.get("ids") or []
        if not st.get("stamped") or len(ids) != 1:
            reason = ("stamp matched no row" if not st.get("stamped")
                      else f"stamp matched {len(ids)} rows")
            return {"ok": False, "reason": reason}

        (
            client.table("fathom_meetings")
            .update({"synthesis_generated_at": _utc_now_iso()})
            .eq("recording_id", recording_id)
            .execute()
        )

        return {"ok": True, "asset_id": ids[0]}
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}
