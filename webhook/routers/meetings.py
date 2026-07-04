"""Meeting routes: promote a meeting transcript to RAG; deep synthesis.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import observability
import pipeline
import signatures
from fastapi import APIRouter, HTTPException, Request, Response

from cp_engine.mc2_db import Tables

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


def _fetch_meeting_by_recording_id(client, recording_id: int) -> dict | None:
    """Pull the single fathom_meetings row for `recording_id`, INCLUDING the
    `transcript` jsonb column that promote_meeting_transcript flattens.

    Distinct from `_fetch_meeting` (which keys on the uuid `id` and omits
    `transcript`). Selects everything promote_meeting_transcript reads
    (recording_id, transcript, transcript_promoted_at, title) plus the
    project link/tags used to resolve the project here, and the fields the
    deep-synthesis path reads (`synthesis_generated_at` skip guard, `meeting_date`
    for recording auto-discovery). Returns the row or None (no match, or any
    error — best-effort).
    """
    try:
        resp = (
            client.table(Tables.FATHOM_MEETINGS)
            .select(
                "recording_id, title, transcript, transcript_promoted_at, "
                "synthesis_generated_at, meeting_date, project_tags, project_id"
            )
            .eq("recording_id", recording_id)
            .single()
            .execute()
        )
        return resp.data or None
    except Exception as exc:  # noqa: BLE001 — best-effort; treat as not found
        log.warning(
            "meeting-promote: fetch failed for recording_id=%s: %s",
            recording_id, exc,
        )
        return None


async def _run_meeting_promote(
    client, meeting_row: dict, project_id: str, company_id: str | None,
) -> None:
    """Background: run the (sync, slow) meeting-transcript promotion off the event
    loop. Never raises — this is the fire-and-forget tail of
    /api/meetings/promote-transcript, which has already returned 202.

    There is NO runs table: promote_meeting_transcript sets
    `fathom_meetings.transcript_promoted_at` on success (the status signal the
    mc-2 meetings-list `transcript_promoted` flag reflects), so the outcome is
    only logged here. Unlike _run_promote there is NO tenant clone: the
    transcript comes off `meeting_row["transcript"]`, not a committed file.

    Engagement-only carries through: an initiative has company_id=None and
    promote_meeting_transcript's CONTRACT A returns {ok:False, reason:"initiative…"}
    without work — logged as a warning, not a crash."""
    from cp_engine.meetings import promote_meeting_transcript

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    recording_id = meeting_row.get("recording_id")
    try:
        result = await asyncio.to_thread(
            promote_meeting_transcript,
            client,
            meeting_row,
            project_id,
            company_id,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )
        if result.get("ok"):
            log.info(
                "meeting-promote: recording_id=%s promoted (asset_id=%s)",
                recording_id, result.get("asset_id"),
            )
        else:
            log.warning(
                "meeting-promote: recording_id=%s not promoted: %s",
                recording_id, result.get("reason"),
            )
    except Exception as exc:  # noqa: BLE001 — never crash the background task
        log.warning(
            "meeting-promote: recording_id=%s run failed: %s",
            recording_id, exc, exc_info=True,
        )
        observability.capture(exc, area="meeting_promote_run")


@router.post("/api/meetings/promote-transcript")
async def meeting_promote_transcript(request: Request) -> Response:
    """Fire-and-forget promotion of a meeting's FULL transcript into the RAG store.

    The mc-2 meetings-list "Promote transcript" click is proxied here (signed) as
    ``{"recording_id": <int>}``. Promotion flattens the meeting's
    `fathom_meetings.transcript` jsonb → text and embeds it into rag_assets so
    it's retrievable via pull_project_source. The embed takes seconds-to-minutes
    (Voyage + Supabase), so this endpoint does NOT block on it: it verifies the
    HMAC, fetches the meeting row (with transcript), resolves the project +
    company, returns 202 immediately, and runs the promotion in a background task.

    Simpler than /api/spine/promote-transcript: keyed on recording_id (not
    code+key); NO tenant clone (transcript is on the DB row); NO runs table
    (`fathom_meetings.transcript_promoted_at`, set by the engine fn on success, is
    the status signal — the mc-2 meetings list reflects it). Engagement-only
    carries through (initiative → company_id None → the engine fn defers).

    Request body (JSON):
        {"recording_id": <int>}  # required

    Response (202):
        {"recording_id": <int>, "status": "running"}
    """
    from cp_engine.asset_ingest import resolve_project_folders_by_id
    from cp_engine.meetings import resolve_meeting_project

    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )
    payload = json.loads(raw_body)
    recording_id = payload.get("recording_id")
    if recording_id is None:
        raise HTTPException(status_code=400, detail="recording_id is required")
    # Coerce to int (the column is bigint). A stringly/float id would otherwise
    # flow into `.eq("recording_id", ...)`, miss, and surface as a misleading
    # "no meeting" 404 instead of an honest 400 for a malformed request.
    try:
        recording_id = int(recording_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="recording_id must be an int")

    client = pipeline._create_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for meeting promote"
        )

    meeting_row = _fetch_meeting_by_recording_id(client, recording_id)
    if meeting_row is None:
        raise HTTPException(
            status_code=404, detail=f"no meeting with recording_id {recording_id}"
        )

    # Prefer the link-flow project_id on the row; fall back to resolving the
    # meeting's project_tags. company_id (None for initiatives → engine gate
    # defers) comes from the resolved project's folders.
    project_id = meeting_row.get("project_id")
    if not project_id:
        project_id, _matched = resolve_meeting_project(
            client, meeting_row.get("project_tags")
        )
    if not project_id:
        raise HTTPException(
            status_code=404,
            detail=f"meeting {recording_id} not linked to a project",
        )
    folders = resolve_project_folders_by_id(client, project_id)
    company_id = folders.company_id if folders else None

    pipeline._spawn_background(
        _run_meeting_promote(client, meeting_row, project_id, company_id)
    )
    return Response(
        content=json.dumps({"recording_id": recording_id, "status": "running"}),
        status_code=202,
        media_type="application/json",
    )


async def _run_meeting_synthesize(
    client, meeting_row: dict, project_id: str, company_id: str | None,
    *, media_url: str | None, documents,
) -> None:
    """Background: run the (slow) deep synthesis off the event loop. Never raises.

    Mirrors _run_meeting_promote but calls the synthesizer service (Gemini video +
    the §4b document channel) and stamps `fathom_meetings.synthesis_generated_at`
    on success. If no media_url was supplied/confirmed, falls back to a supplied
    transcript so the service still produces a (text-only) synthesis."""
    from cp_engine.meeting_synthesis import synthesize_meeting

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    recording_id = meeting_row.get("recording_id")

    # No video → hand the service the transcript we already hold, so "Deep
    # synthesis" degrades to a text synthesis rather than failing (design §2).
    supplied_transcript = None
    if not media_url:
        supplied_transcript = _transcript_segments_for_service(meeting_row)

    try:
        result = await asyncio.to_thread(
            synthesize_meeting,
            client,
            meeting_row,
            project_id,
            company_id,
            media_url=media_url,
            documents=documents,
            supplied_transcript=supplied_transcript,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )
        if result.get("ok"):
            log.info("meeting-synthesize: recording_id=%s done (asset_id=%s)",
                     recording_id, result.get("asset_id"))
        else:
            log.warning("meeting-synthesize: recording_id=%s not done: %s",
                        recording_id, result.get("reason"))
    except Exception as exc:  # noqa: BLE001 — never crash the background task
        log.warning("meeting-synthesize: recording_id=%s run failed: %s",
                    recording_id, exc, exc_info=True)
        observability.capture(exc, area="meeting_synthesize_run")


def _transcript_segments_for_service(meeting_row: dict) -> list | None:
    """Shape `fathom_meetings.transcript` jsonb into the synth service's
    supplied_transcript form ([{timestamp, speaker, text}, ...]), or None."""
    segs = meeting_row.get("transcript")
    if not segs:
        return None
    out = []
    for s in segs:
        out.append({
            "timestamp": s.get("timestamp") or "",
            "speaker": (s.get("speaker") or {}).get("display_name") or "Speaker",
            "text": s.get("text") or "",
        })
    return out or None


@router.post("/api/meetings/synthesize")
async def meeting_synthesize(request: Request) -> Response:
    """Fire-and-forget DEEP synthesis of a meeting (design §2, the third fidelity).

    Proxied (signed) from mc-2's "Deep synthesis" action as
    ``{"recording_id": <int>, "media_url"?: <str>, "documents"?: [...]}``.
    ``media_url`` is the dashboard's confirmed/overridden recording (auto-
    discovered from the project's ingest folder, human-confirmed); omit it to fall
    back to a transcript-only synthesis. ``documents`` is the optional §4b channel
    (each ``{title, pdf_b64}``).

    Like promote-transcript: verify HMAC, fetch the meeting, resolve project +
    company, return 202, run in the background. Status signal is
    ``fathom_meetings.synthesis_generated_at`` (no runs table).
    """
    from cp_engine.asset_ingest import resolve_project_folders_by_id
    from cp_engine.meetings import resolve_meeting_project

    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )
    payload = json.loads(raw_body)
    recording_id = payload.get("recording_id")
    if recording_id is None:
        raise HTTPException(status_code=400, detail="recording_id is required")
    try:
        recording_id = int(recording_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="recording_id must be an int")

    media_url = payload.get("media_url")
    documents = payload.get("documents")

    client = pipeline._create_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for meeting synthesize"
        )

    meeting_row = _fetch_meeting_by_recording_id(client, recording_id)
    if meeting_row is None:
        raise HTTPException(
            status_code=404, detail=f"no meeting with recording_id {recording_id}"
        )

    project_id = meeting_row.get("project_id")
    if not project_id:
        project_id, _matched = resolve_meeting_project(
            client, meeting_row.get("project_tags")
        )
    if not project_id:
        raise HTTPException(
            status_code=404,
            detail=f"meeting {recording_id} not linked to a project",
        )
    folders = resolve_project_folders_by_id(client, project_id)
    company_id = folders.company_id if folders else None

    pipeline._spawn_background(
        _run_meeting_synthesize(
            client, meeting_row, project_id, company_id,
            media_url=media_url, documents=documents,
        )
    )
    return Response(
        content=json.dumps({"recording_id": recording_id, "status": "running"}),
        status_code=202,
        media_type="application/json",
    )
