"""Shared auto-ingest pipeline: transcript fetch/persist, per-project ingest, run logging.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import git_ops
import observability
from commitments_propose import propose_commitments
from fastapi import HTTPException
from meeting_artifact import write_meeting_artifacts

from cp_engine import mc2_db
from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, execute_plan
from cp_engine.mc2_db import Tables
from cp_engine.plan_from_transcript import PlanGenerationError, generate_plan
from cp_engine.retrospective import append_entry, build_entry
from cp_engine.spine import SpineDirNotFound, find_spine_dir

log = logging.getLogger("cp-engine-webhook")

# The Anthropic model used for spine-inbox distillation. Derived from
# generate_plan's own default so this webhook never drifts from the engine's
# canonical model choice (rather than re-hardcoding the id).
DEFAULT_MODEL: str = (
    inspect.signature(generate_plan).parameters["model"].default
)

# Strong-reference set for background tasks spawned from Slack-action
# handlers. Python's asyncio keeps only WEAK references to tasks created
# by asyncio.create_task — without a strong reference, the GC can collect
# a still-running task mid-execution. The set + done-callback pattern
# (see _spawn_background) keeps a strong reference until the task finishes.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """asyncio.create_task with strong-reference retention.

    Adds the task to the module-level `_background_tasks` set and adds a
    done-callback that discards it once complete. Required for the Slack
    interactive flow where the request returns 200 before the work runs.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _create_supabase_client():
    """Build a Supabase client for execute_plan's ClickUp-proposal verbs.

    Returns None when env vars are missing OR the supabase package isn't
    importable. Callers MUST treat None as "ClickUp routing degraded" and
    keep going — the primary sprint-file ingest contract is that we never
    break it on best-effort Supabase work (see clickup_propose._supabase_client
    for the original of this pattern; this helper exists so every
    `execute_plan(...)` callsite can thread a real client through to the
    v0.15 ``set-milestone`` / ``set-client-ask-task`` verbs instead of
    falling into ingest.py's "no client → silent skip" branch, which is
    what made the v0.15.0 headline feature dead code in prod).
    """
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning(
            "execute_plan supabase client unavailable: "
            "SUPABASE_URL/SUPABASE_SERVICE_KEY env missing OR supabase pkg not installed"
        )
    return client


def _load_tenant_config(tenant_root: Path) -> TenantConfig:
    """Load .cp-engine.toml from the freshly-cloned tenant root.

    The webhook deployment doesn't get .cp-engine.local.toml — that's
    a per-machine file with absolute paths to project clones. cp_engine.config.load()
    already treats local as optional when the committed config declares
    no [[projects]] (the CI-runner case), which is true for the cp tenant
    (MC-2-backed, no committed [[projects]]). So plain load() just works.
    """
    from cp_engine import config as cfg_mod

    return cfg_mod.load(tenant_root=tenant_root)


def _speaker_names(participants) -> list[str]:
    """Extract participant display names defensively.

    Fathom's `participants` is a list whose items may be dicts (with a
    `name` field) or bare strings. Anything else is ignored. Mirrors the
    parsing already done in meeting_artifact._build_markdown.
    """
    names: list[str] = []
    for p in participants or []:
        if isinstance(p, dict) and p.get("name"):
            names.append(p["name"])
        elif isinstance(p, str) and p:
            names.append(p)
    return names


def _plan_decisions(plan: dict | None, code: str) -> list[str]:
    """Pull decision texts out of the generated plan's project block, if any.

    The plan's `record-decision` verb (when present) carries one dict per
    decision; we extract a text/description field defensively. Returns []
    if the plan has no decisions for this project.
    """
    if not plan:
        return []
    block = (plan.get("projects") or {}).get(code) or {}
    out: list[str] = []
    for item in block.get("record-decision") or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("decision") or item.get("description")
            if text:
                out.append(str(text).strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _action_item_texts(action_items: list[dict] | None) -> list[str]:
    """Extract a text/description field from each Fathom action item."""
    out: list[str] = []
    for item in action_items or []:
        if isinstance(item, dict):
            text = item.get("description") or item.get("text") or item.get("title")
            if text:
                out.append(str(text).strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _append_retrospective(
    *,
    config: TenantConfig,
    code: str,
    meeting: dict | None,
    action_items: list[dict] | None,
    plan: dict | None,
) -> str:
    """Append the meeting's WHOLE Fathom summary to the project's Retrospective.

    Best-effort, never raises. Returns one of:
      - "skipped"   — no meeting, or no summary to preserve
      - "appended"  — a new entry was written
      - "duplicate" — meeting already present (idempotency no-op)
      - "error"     — an exception was caught (logged); ingest continues

    The Fathom `summary` is embedded WHOLE (anti-compression); decisions /
    action items / links are ADDED as structured pointers, never a
    replacement. The history file is committed by the caller's normal
    `git add -A` commit.
    """
    if meeting is None:
        return "skipped"
    summary = (meeting.get("summary") or "").strip()
    if not summary:
        return "skipped"

    try:
        meeting_id = str(meeting.get("id") or "")
        entry_md = build_entry(
            date=str(meeting.get("meeting_date") or ""),
            title=str(meeting.get("title") or "Untitled meeting"),
            speakers=_speaker_names(meeting.get("participants")),
            summary=summary,
            decisions=_plan_decisions(plan, code),
            action_items=_action_item_texts(action_items),
            recording_url=meeting.get("fathom_url"),
            transcript_link=None,
            meeting_id=meeting_id or None,
        )
        history_path = (
            find_spine_dir(config.root, code)
            / "spine"
            / "Retrospective"
            / "meeting-history.md"
        )
        wrote = append_entry(
            history_path,
            meeting_id,
            entry_md,
            code=code,
            project=code,
            today=datetime.now().date(),
        )
        return "appended" if wrote else "duplicate"
    except SpineDirNotFound as exc:
        # The project's working dir hasn't been synced yet (e.g. its first
        # meeting arrives before sync ran). Distinct from a genuine error so
        # it's observable as a benign skip, not a failure.
        log.warning("retrospective: no spine dir yet for %s: %s", code, exc)
        return "no-spine-dir"
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("retrospective: append failed for %s: %s", code, exc)
        observability.capture(exc, area="retrospective_append")
        return "error"


def _append_inbox_card(
    *,
    code: str,
    transcript_path: Path,
    meeting_id: str | None,
) -> str:
    """Distill the meeting into a *proposed* ``spine_inbox`` card (Phase 3).

    The spine no longer gets un-framed substance stubs written straight from a
    transcript: instead a human frames+promotes a proposed card (`cp
    spine-frame`). This step writes that proposed card — a raw-faithful first-pass
    distillation + a best-guess estimate work item — for one project.

    Best-effort, never raises. Returns one of:
      - "skipped"   — no supabase client, no source_ref, or no MC-2 project
      - "proposed"  — a proposed card was upserted
      - "error"     — an exception was caught (logged); ingest continues

    Writes ONLY to ``spine_inbox`` (one ANTHROPIC call). Does NOT write spine
    substance — that is the human-directed frame→promote step.
    """
    # Env-gated kill-switch. Default ON (prod unchanged), but a single env var
    # can disable the live inbox write — and its ANTHROPIC call — without a
    # redeploy. Short-circuits BEFORE any supabase/distiller work.
    if os.environ.get("SPINE_INBOX_ENABLED", "1") not in ("1", "true", "True"):
        return "skipped"
    source_ref = str(meeting_id or "").strip()
    if not source_ref:
        return "skipped"
    client = _create_supabase_client()
    if client is None:
        return "skipped"
    try:
        from cp_engine import asset_ingest
        from cp_engine.estimate import fetch_estimate
        from cp_engine.plan_from_transcript import _call_claude, _read_transcript
        from cp_engine.spine_inbox import build_inbox_card_from_transcript

        folders = asset_ingest.resolve_project_folders(client, code)
        if folders is None:
            log.warning("inbox: no MC-2 project resolved for %s", code)
            return "skipped"
        project_id = folders.project_id

        # Estimate is best-effort scope: a missing/unreachable estimate just
        # means no item guess (guessed_type falls back to "source").
        try:
            estimate = fetch_estimate(client, project_id)
        except Exception as exc:  # noqa: BLE001 — estimator unreachable
            log.warning("inbox: estimate fetch failed for %s: %s", code, exc)
            estimate = None

        transcript = _read_transcript(transcript_path)
        build_inbox_card_from_transcript(
            client,
            project_id=project_id,
            project_code=code,
            source_ref=source_ref,
            transcript=transcript,
            estimate=estimate,
            distiller=_call_claude,
            model=DEFAULT_MODEL,
        )
        return "proposed"
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("inbox: proposed-card write failed for %s: %s", code, exc)
        observability.capture(exc, area="inbox_card_write")
        return "error"


def _ingest_one_project(
    *,
    config: TenantConfig,
    code: str,
    transcript_path: Path,
    action_items: list[dict] | None = None,
    meeting_id: str | None = None,
    meeting: dict | None = None,
    roster: list | None = None,
) -> dict:
    """Generate plan + execute for a single project. Returns a summary dict.

    `action_items` (the Fathom meeting's structured action_items JSONB)
    is threaded into `generate_plan` where each item becomes a
    deterministic `record-ask` appended to the plan. Default-None keeps
    the function callable from any other site that hasn't fetched the
    meeting row yet.

    `meeting_id` is threaded into ``execute_plan`` so the v0.15+
    ClickUp-proposal verbs (``set-milestone`` / ``set-client-ask-task``)
    can stamp the originating meeting onto each proposal row.
    """
    entry = {
        "code": code,
        "plan_summary": None,
        "files_written": [],
        "skipped_duplicate": 0,
        "errors": [],
        "cross_project": [],
    }
    try:
        gen = generate_plan(
            config=config,
            project_code=code,
            transcript_path=transcript_path,
            action_items=action_items,
            roster=roster,
        )
    except PlanGenerationError as exc:
        entry["errors"].append(f"plan generation failed: {exc}")
        log.warning("plan generation failed for %s: %s", code, exc)
        return entry

    # Summarize what's in the plan for logging + observability.
    projects = gen.plan.get("projects") or {}
    project_block = projects.get(code) or {}
    entry["plan_summary"] = {verb: len(items) for verb, items in project_block.items()}
    # Cross-project proposals (#88): validated annotations collected by
    # generate_plan. The caller writes them to MC-2 after this returns.
    entry["cross_project"] = list(gen.cross_project)

    try:
        exec_result = execute_plan(
            gen.plan,
            tenant_root=config.root,
            today=datetime.now().date(),
            supabase=_create_supabase_client(),
            meeting_id=meeting_id,
        )
    except IngestPlanError as exc:
        entry["errors"].append(f"plan execution failed: {exc}")
        log.warning("plan execution failed for %s: %s", code, exc)
        return entry

    entry["files_written"] = [str(p) for p in exec_result.files_written]
    entry["skipped_duplicate"] = exec_result.skipped_duplicate
    entry["errors"].extend(exec_result.errors)

    # Retrospective append (spine-inversion Part B). Best-effort: never
    # raises, never appends to entry["errors"], so a retrospective failure
    # can't flip the project's ingest status to "failed". The new file is
    # picked up by the caller's normal `git add -A` commit.
    entry["retrospective"] = _append_retrospective(
        config=config,
        code=code,
        meeting=meeting,
        action_items=action_items,
        plan=gen.plan,
    )

    # Spine ingestion inbox (Phase 3). Best-effort, never raises: write a
    # PROPOSED card (raw distillation + guessed estimate item) for a human to
    # frame+promote via `cp spine-frame`. Never writes spine substance.
    entry["inbox_card"] = _append_inbox_card(
        code=code,
        transcript_path=transcript_path,
        meeting_id=meeting_id,
    )
    return entry


def _normalize_transcript(transcript_field) -> str:
    """Normalize Fathom's transcript field into a single readable string.

    Four shapes show up in production:
      1. None / empty → ""
      2. plain string → returned as-is (older webhook payload format)
      3. JSONB object with "text"/"plain_text" key → that value
      4. JSONB list of segments `[{text, speaker:{display_name}, timestamp}, ...]`
         → rendered as `<ts> - <speaker>\\n  <text>` blocks matching the
         staged-transcript file format that plan_from_transcript was
         tuned against.

    Shape 4 is the standard across all current Fathom-delivered meetings;
    the earlier `str(...)` fallback turned a Python list into its repr
    (which silently passed validation but produced garbage prompts).
    """
    if transcript_field is None:
        return ""
    if isinstance(transcript_field, str):
        return transcript_field
    if isinstance(transcript_field, list):
        return _segments_to_text(transcript_field)
    if isinstance(transcript_field, dict):
        if transcript_field.get("text"):
            return transcript_field["text"]
        if transcript_field.get("plain_text"):
            return transcript_field["plain_text"]
        segments = transcript_field.get("segments")
        if isinstance(segments, list):
            return _segments_to_text(segments)
    return ""


def _segments_to_text(segments: list) -> str:
    """Render Fathom segments as `<ts> - <speaker>\\n  <text>` blocks."""
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker_obj = seg.get("speaker") or {}
        speaker = (
            (speaker_obj.get("display_name") if isinstance(speaker_obj, dict) else None)
            or seg.get("display_name")
            or seg.get("speaker_name")
            or "Unknown"
        )
        ts = seg.get("timestamp") or seg.get("ts") or ""
        header = f"{ts} - {speaker}" if ts else speaker
        lines.append(f"{header}\n  {text}")
    return "\n\n".join(lines)


def _fetch_transcript(meeting_id: str) -> str:
    """Pull a transcript from Supabase fathom_meetings table."""
    client = mc2_db.get_client(required=False)
    if client is None:
        raise HTTPException(
            status_code=500,
            detail="Supabase unavailable (env not set or package not installed)",
        )
    resp = (
        client.table(Tables.FATHOM_MEETINGS)
        .select(mc2_db.FATHOM_TRANSCRIPT_COLUMNS)
        .eq("id", meeting_id)
        .single()
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")

    text = _normalize_transcript(resp.data.get("transcript"))
    if not text:
        raise HTTPException(
            status_code=422, detail=f"meeting {meeting_id} has no transcript text"
        )

    header = (
        f"# {resp.data.get('title', 'Meeting')}\n"
        f"# Date: {resp.data.get('meeting_date', 'unknown')}\n"
        f"# Meeting ID: {meeting_id}\n\n"
    )
    return header + text


def _fetch_meeting(meeting_id: str) -> dict | None:
    """Pull the full fathom_meetings row needed for per-meeting artifacts.

    Includes `summary` and `action_items` — fields the plan-generation
    path drops but the meeting-artifact path needs. Best-effort: returns
    None on any failure so artifact generation degrades gracefully
    without breaking auto-ingest.
    """
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning("meeting-artifact: Supabase env not set; skipping fetch")
        return None
    try:
        resp = (
            client.table(Tables.FATHOM_MEETINGS)
            # Link/embed path reads recording_id/project_tags/project_id/
            # summary_embedded_at off the SAME row: resolve_meeting_project →
            # link_meeting → embed_meeting_summary. Omitting them made every
            # webhook meeting resolve to no project and no-op.
            .select(mc2_db.FATHOM_ARTIFACT_COLUMNS)
            .eq("id", meeting_id)
            .single()
            .execute()
        )
        return resp.data or None
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("meeting-artifact: meeting fetch failed for %s: %s", meeting_id, exc)
        return None


def _stage_transcript(tenant_root: Path, meeting_id: str, text: str) -> Path:
    """Write transcript to tenant's transcripts/incoming/ for the prompt + audit."""
    incoming = tenant_root / "transcripts" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    filename = f"auto-ingest-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{meeting_id[:8]}.txt"
    path = incoming / filename
    path.write_text(text, encoding="utf-8")
    return path


def _sanitize_transcript_title(title: str) -> str:
    """Make a meeting title filesystem-safe but still human-readable.

    Matches the existing hand-added transcript style (e.g.
    "Sync on AI workshop Agenda and Plan") — readable words, NOT a
    hyphen-slug. Path separators and control chars become spaces;
    repeated whitespace collapses; leading/trailing whitespace strips.
    """
    title = title or ""
    # Replace path separators and any control chars (incl. newlines/tabs)
    # with a space; these would either break the path or render unreadably.
    title = re.sub(r"[\x00-\x1f/\\]", " ", title)
    # Collapse runs of whitespace into a single space.
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _persist_transcript(
    project_dir: Path, meeting_date: str, title: str, text: str
) -> Path:
    """Persist the FULL verbatim transcript into a project's tree.

    Writes `<project_dir>/meeting-transcripts/<date> <safe-title>.txt`,
    matching the readable filename style of the existing hand-added
    transcripts. `meeting_date` may be a full ISO timestamp (the
    `fathom_meetings.meeting_date` column is); only the `YYYY-MM-DD`
    prefix is used for the stem — matching the sibling `meetings/`
    artifact writer (`write_meeting_artifacts`, which slices `[:10]`),
    and keeping the colon-laden time out of the filename.

    Re-ingesting the SAME meeting overwrites (idempotent backfill).
    A DIFFERENT meeting that would sanitize to the same stem (two
    same-day meetings with the same title — this happens; cf. the
    `2026-05-12 impromptu zoom meeting -2/-3/...` files in the tenant)
    is disambiguated with a ` (2)`, ` (3)`, … suffix rather than
    silently clobbering the earlier meeting's transcript. Same-stem +
    same-body is treated as the idempotent case and overwritten in
    place.

    The file is committed by the caller's normal `git add -A` commit.

    Returns the written Path.
    """
    safe_title = _sanitize_transcript_title(title)
    # `meeting_date` is an ISO timestamp; keep only the date, matching
    # the sibling meetings/ artifact convention. Guards a bare date too.
    date_part = (meeting_date or "").strip()[:10]
    if date_part and safe_title:
        stem = f"{date_part} {safe_title}"
    elif date_part:
        stem = date_part
    elif safe_title:
        stem = safe_title
    else:
        stem = "Untitled meeting"

    out_dir = project_dir / "meeting-transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)

    path = out_dir / f"{stem}.txt"
    # Collision guard: never silently overwrite a *different* meeting's
    # transcript. If the stem is taken by a file with different content,
    # walk to the next free ` (N)` suffix. Identical content = the same
    # meeting re-ingested → overwrite in place (idempotent).
    counter = 2
    while path.exists() and path.read_text(encoding="utf-8") != text:
        path = out_dir / f"{stem} ({counter}).txt"
        counter += 1

    path.write_text(text, encoding="utf-8")
    return path


def _link_meeting_safe(client, meeting, supabase_url, supabase_key):
    """Best-effort: link the meeting to its project + embed its summary into RAG.
    NEVER raises — a failure here must not abort the primary sprint-file ingest.

    Resolves company_id from the MEETING'S OWN resolved project (the first
    resolvable tag in `meeting["project_tags"]`), NOT from any caller-supplied
    `code`. This is load-bearing for cross-company fan-outs (sprint-planning /
    account meetings span projects under DIFFERENT companies): the rescope
    cascade must write the company of the project the meeting is actually linked
    to, never the company of whichever project happened to be iterating. The
    meeting links to exactly ONE project, so this runs exactly once per meeting.
    """
    try:
        from cp_engine.asset_ingest import resolve_project_folders_by_id

        # NOTE: no `assigner=` is threaded — link_meeting's default is a no-op,
        # so meetings land unassigned ("needs assignment") until the LLM
        # work-item classifier (a later task) wires a real assigner here.
        from cp_engine.meetings import link_meeting, rescope_meeting, resolve_meeting_project

        # Resolve the meeting's OWN project once. Untagged / unresolvable → no
        # work (link_meeting would no-op too, but resolving here lets us also
        # skip the company lookup). This is the SAME project link_meeting will
        # resolve internally, so company_id below matches the linked project.
        project_id, _matched = resolve_meeting_project(
            client, meeting.get("project_tags"))
        if project_id is None:
            log.info("meeting-link: meeting=%s no resolvable project tag — skip",
                     meeting.get("recording_id"))
            return {"ok": True, "linked": False, "reason": "no resolvable project tag"}

        folders = resolve_project_folders_by_id(client, project_id)
        company_id = folders.company_id if folders else None

        # v1 ENGAGEMENTS ONLY: `company_id is None` is the engagement-vs-initiative
        # signal (initiatives have no company/folders; promote's CONTRACT A keys
        # on it too). fathom_meetings.project_id is FK'd to projects(id) (migration
        # 084), so an initiative id would violate the FK on write; we thread
        # is_engagement so link_meeting DEFERS initiative meetings cleanly rather
        # than half-trying and silently crashing inside the best-effort wrapper.
        is_engagement = company_id is not None

        # Wire the real rescope, threading the meeting's-project company_id so a
        # retag preserves the correct account scope.
        def _rescope(c, row, new_pid):
            return rescope_meeting(c, row, new_pid, new_company_id=company_id)

        result = link_meeting(client, meeting,
                              rescope=_rescope,
                              is_engagement=is_engagement,
                              supabase_url=supabase_url, supabase_key=supabase_key)
        log.info("meeting-link: meeting=%s result=%s",
                 meeting.get("recording_id"), result)
        return result
    except Exception as exc:  # noqa: BLE001 — never block primary ingest
        log.warning("meeting-link failed (non-fatal): meeting=%s err=%s",
                    meeting.get("recording_id"), exc, exc_info=True)
        observability.capture(exc, area="meeting_link")
        return None


def _propose_cross_project(
    *,
    meeting_id: str | None,
    source_code: str,
    proposals: list[dict],
) -> dict:
    """Write cross-project proposals (#88) to MC-2's review gate.

    Returns a summary {inserted, duplicate, unresolvable} for the run log.
    Raises only on client-creation problems the caller already guards.
    """
    from cp_engine.cross_project import write_proposal

    client = _create_supabase_client()
    if client is None:
        log.info("cross-project: no Supabase client; skipping %d proposal(s)",
                 len(proposals))
        return {}

    summary = {"inserted": 0, "duplicate": 0, "unresolvable": 0}
    for p in proposals:
        outcome = write_proposal(
            client,
            meeting_id=meeting_id,
            source_code=source_code,
            target_code=p["target_code"],
            verb=p["verb"],
            text=p["text"],
            confidence=p.get("confidence"),
            item=p.get("item"),
        )
        summary[outcome] = summary.get(outcome, 0) + 1
    log.info(
        "cross-project: %s → proposals %s (meeting %s)",
        source_code, summary, meeting_id,
    )
    return summary


def _perform_auto_ingest(
    *,
    meeting_id: str,
    project_codes: list[str],
    transcript_text: str | None = None,
) -> dict:
    """Body of /api/auto-ingest, factored out so the rerun endpoint can
    call it directly without re-marshalling through a fastapi Request.

    Does the transcript fetch (if absent), tenant clone, per-project
    ingest+commit loop, ClickUp proposals, meeting artifacts, and
    auto_ingest_runs row insert. Same return shape as /api/auto-ingest.
    """
    if transcript_text is None:
        transcript_text = _fetch_transcript(meeting_id)

    log.info(
        "auto-ingest start: meeting=%s projects=%s", meeting_id, project_codes
    )

    # Best-effort Supabase coords for the ADDITIVE meeting-link side-step below
    # (resolved once — loop-invariant). A None client (env missing / pkg absent)
    # or missing creds just skips linking — the primary sprint-file ingest never
    # depends on it. Cred split mirrors _run_promote: client via the factory,
    # url/key from env.
    link_client = _create_supabase_client()
    link_url = os.environ.get("SUPABASE_URL")
    link_key = os.environ.get("SUPABASE_SERVICE_KEY")

    # Hoisted out of the `with` below so the failure handler at the bottom
    # of this function can report PARTIAL state. Commits are pushed inside
    # the per-project loop, so a mid-run crash (classically: a push that
    # loses a concurrent-webhook race) leaves earlier projects already
    # published and later ones not. The failure row has to say which.
    ingested: list[dict] = []
    commits: list[str] = []

    try:
        with git_ops._cloned_tenant() as tenant_root:
            config = _load_tenant_config(tenant_root)
            transcript_path = _stage_transcript(tenant_root, meeting_id, transcript_text)

            # Fetch the meeting row once per request. We need `action_items`
            # for the plan merge (each item becomes a deterministic
            # record-ask) AND for the per-meeting artifact write later in
            # this same function. Pre-Lever-1 the artifact path did its own
            # fetch; we now share the result to avoid a redundant Supabase
            # round-trip per auto-ingest call. Best-effort: a None here
            # means we'll fall back to LLM-only ingest (no action_items
            # merged) and artifact generation will skip cleanly.
            meeting = _fetch_meeting(meeting_id)
            action_items = (meeting or {}).get("action_items") or []

            # Cross-project detection roster (#88): every active project across
            # the tenant, fetched once per run. Best-effort — an MC-2 hiccup
            # degrades to roster=None, which turns detection OFF for this run
            # (the prompt tells the model to emit no cross_project fields).
            try:
                from cp_engine.plan_from_account_meeting import list_active_all

                roster = list_active_all(config)
            except Exception:  # noqa: BLE001 — detection must never break ingest
                log.warning("cross-project roster fetch failed", exc_info=True)
                roster = None

            # Per-project ingest + per-project commit. A multi-project
            # auto-ingest call produces N commits (one per project that
            # actually wrote files), not one combined commit. This makes
            # the git log readable (each commit's diff scopes to one
            # project's sprint file) and lets revert target one project
            # without disturbing the others.
            ingested: list[dict] = []
            commits: list[str] = []
            for code in project_codes:
                entry = _ingest_one_project(
                    config=config,
                    code=code,
                    transcript_path=transcript_path,
                    action_items=action_items,
                    meeting_id=meeting_id,
                    meeting=meeting,
                    roster=roster,
                )
                ingested.append(entry)

                # Cross-project proposals (#88) → MC-2 review gate. Best-effort:
                # a store failure must never break the ingest or the commit.
                if entry.get("cross_project"):
                    try:
                        entry["cross_project_proposed"] = _propose_cross_project(
                            meeting_id=meeting_id,
                            source_code=code,
                            proposals=entry["cross_project"],
                        )
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "cross-project proposal write failed for %s (meeting %s)",
                            code, meeting_id, exc_info=True,
                        )

                # Persist the FULL verbatim transcript into this project's
                # meeting-transcripts/ dir. It's the input for the
                # workshop-synthesis pipeline and a durable project source.
                # Best-effort: a persist failure must NEVER abort the ingest
                # or break the commit. Only persist when we actually have a
                # transcript. The write lands BEFORE _commit_and_push so its
                # `git add -A` sweeps it into this project's commit.
                transcript_persisted = False
                if transcript_text:
                    try:
                        project_dir = find_spine_dir(config.root, code)
                        _persist_transcript(
                            project_dir,
                            str((meeting or {}).get("meeting_date") or ""),
                            str((meeting or {}).get("title") or ""),
                            transcript_text,
                        )
                        transcript_persisted = True
                    except Exception as exc:  # noqa: BLE001 — never break auto-ingest
                        log.warning(
                            "transcript-persist failed for %s (meeting %s)",
                            code, meeting_id, exc_info=True,
                        )
                        observability.capture(exc, area="transcript_persist")

                # Record per-entry whether a transcript landed so the commit
                # message can attribute a transcript-only commit and a result
                # consumer can tell "transcript only" from "wrote bullets".
                entry["transcript_persisted"] = transcript_persisted

                if entry["files_written"] or transcript_persisted:
                    commit_sha = git_ops._commit_and_push(
                        tenant_root=tenant_root,
                        meeting_id=meeting_id,
                        ingested=[entry],
                    )
                    entry["commit_sha"] = commit_sha
                    commits.append(commit_sha)
                    log.info(
                        "auto-ingest commit: meeting=%s project=%s commit=%s",
                        meeting_id, code, commit_sha,
                    )

            # ADDITIVE, NON-FATAL: link the meeting to its project + embed its
            # summary into RAG. Runs ONCE per meeting, AFTER the per-project loop —
            # the meeting links to exactly one project (the first resolvable tag),
            # so company_id is derived from THAT project, never from a loop `code`
            # (which on a cross-company fan-out could be a different company and
            # corrupt account-scoped retrieval on a retag). Independent side-work:
            # it does not gate the commit/push above and a failure is swallowed by
            # _link_meeting_safe. Cred split mirrors _run_promote intentionally:
            # link_client via _create_supabase_client(), url/key from env — these
            # match in the Railway env.
            #
            # NOTE (retag-trigger gap): re-tagging a meeting to its CURRENT project
            # does NOT re-fire this webhook (a known fathom-meeting-sync trigger
            # gap; workaround is unassign-then-reassign). So the retag re-scope
            # cascade only fires when the webhook actually re-runs — a deploy-time
            # dependency on fathom-meeting-sync, not fixable here.
            if meeting and link_client is not None and link_url and link_key:
                _link_meeting_safe(link_client, meeting, link_url, link_key)

            # Stage A — propose commitments from the meeting's Fathom action
            # items (MC-2 public.commitments; successor to the ClickUp proposal
            # path — commitments consolidation, cp-engine #38). Independent of
            # whether the transcript produced cp bullets; best-effort, never
            # raises. The #88 roster (fetched once above) powers off-project
            # flagging (#114); roster=None just turns the screen off.
            commitments_summary = propose_commitments(
                meeting_id, project_codes, roster=roster
            )

            # Per-meeting artifacts — synthesis + transcript into each
            # project's meetings/ dir. Runs after the per-project bullet
            # commits so their `git add -A` doesn't sweep these in. Reuses
            # the meeting row already fetched above; passes it via
            # `meeting=` to skip the redundant Supabase round-trip inside
            # _generate_meeting_artifacts.
            artifact_summary = _generate_meeting_artifacts(
                tenant_root=tenant_root,
                meeting_id=meeting_id,
                transcript_text=transcript_text,
                project_codes=project_codes,
                meeting=meeting,
            )

            if not commits:
                log.info("auto-ingest no-op: no files changed for meeting=%s", meeting_id)
                response = {
                    "ingested": ingested,
                    "commit_sha": None,
                    "skipped_no_op": True,
                    "commitments": commitments_summary,
                    "meeting_artifacts": artifact_summary,
                }
                _log_run_to_supabase(
                    meeting_id=meeting_id,
                    project_codes=project_codes,
                    status=_status_from_ingested(ingested, anything_wrote=False),
                    ingested=ingested,
                    commit_sha=None,
                )
                return response

            # observability: log one row per multi-project auto-ingest run
            # against the last commit SHA. Per-project commit shas are
            # surfaced in the `ingested` payload field.
            last_commit = commits[-1]
            log.info(
                "auto-ingest done: meeting=%s commits=%d last=%s",
                meeting_id, len(commits), last_commit,
            )
            response = {
                "ingested": ingested,
                "commit_sha": last_commit,
                "commit_shas": commits,
                "skipped_no_op": False,
                "commitments": commitments_summary,
                "meeting_artifacts": artifact_summary,
            }
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=project_codes,
                status="success",
                ingested=ingested,
                commit_sha=last_commit,
            )
            return response

    except Exception as exc:  # noqa: BLE001 — re-raised below; see docstring
        # A failure AFTER the per-project loop starts had no observability
        # at all: every _log_run_to_supabase call sits on the success path,
        # so an exception escaping the `with` above wrote NO row — neither
        # success nor failed. auto_ingest_runs showed a clean record while
        # commits sat unpublished in a discarded clone (observed 2026-08-12
        # 11:21–11:31: an 11-webhook burst where losers of the push race
        # exhausted _push_with_retry and raised CalledProcessError 128).
        # Sentry was the only signal, which is why the runs table showed no
        # failure since June.
        #
        # `commits` is the PARTIAL published set: pushes happen inside the
        # per-project loop, so earlier projects are already on origin/main
        # and later ones never made it. Recording the last pushed sha (not
        # None) is what makes the row diagnosable — it's the boundary
        # between what landed and what didn't.
        #
        # Re-raises: the caller must still 500 so fathom-meeting-sync
        # retries the delivery. This adds a record; it does not swallow.
        log.warning(
            "auto-ingest failed: meeting=%s projects=%s commits_pushed=%d: %s",
            meeting_id, project_codes, len(commits), exc,
        )
        _log_run_to_supabase(
            meeting_id=meeting_id,
            project_codes=project_codes,
            status="failed",
            ingested=ingested,
            commit_sha=commits[-1] if commits else None,
            top_level_errors=[f"{type(exc).__name__}: {exc}"],
        )
        raise


def _generate_meeting_artifacts(
    *,
    tenant_root: Path,
    meeting_id: str,
    transcript_text: str,
    project_codes: list[str],
    meeting: dict | None = None,
) -> dict:
    """Fetch the meeting, write per-meeting artifacts, commit them.

    Shared by all auto-ingest endpoints. Best-effort — never raises.
    Returns a summary dict for the response payload.

    `meeting` lets a caller pass in an already-fetched fathom_meetings
    row to skip the redundant Supabase round-trip. The /api/auto-ingest
    endpoint fetches the meeting up front to extract `action_items` for
    plan generation; it passes the same row here. Other endpoints that
    don't pre-fetch leave `meeting=None` and we fetch ourselves.
    """
    summary = {"files_written": 0, "commit_sha": None}
    if not project_codes:
        return summary
    try:
        if meeting is None:
            meeting = _fetch_meeting(meeting_id)
        if meeting is None:
            return summary
        paths = write_meeting_artifacts(
            tenant_root=tenant_root,
            meeting=meeting,
            transcript_text=transcript_text,
            project_codes=project_codes,
        )
        summary["files_written"] = len(paths)
        if paths:
            sha = git_ops._commit_meeting_artifacts(
                tenant_root=tenant_root,
                meeting_id=meeting_id,
                artifact_paths=paths,
            )
            summary["commit_sha"] = sha
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("meeting-artifact: generation step failed: %s", exc)
        observability.capture(exc, area="meeting_artifact_generation")
    return summary


def _status_from_ingested(ingested: list[dict], *, anything_wrote: bool) -> str:
    """Derive the auto_ingest_runs.status enum value.

    - 'failed': at least one project errored out
    - 'skipped_no_op': nothing wrote AND nothing errored (clean no-op)
    - 'success': we wrote files (used in the commit-and-push branch)
    """
    if any(entry.get("errors") for entry in ingested):
        return "failed"
    if not anything_wrote:
        return "skipped_no_op"
    return "success"


def _find_successful_duplicate_run(
    meeting_id: str, project_codes: list[str]
) -> str | None:
    """Return the existing ``auto_ingest_runs.id`` if a successful run
    already exists for this (meeting_id, sorted project_codes) tuple.

    Used by /api/auto-ingest to short-circuit Fathom's retry-on-timeout
    behavior. A duplicate delivery is harmless thanks to the content-hash
    dedupe inside execute_plan, but the second pass still spends a Claude
    call + git push for zero net change and pollutes auto_ingest_runs
    with two rows. This check turns that into a 200 with
    ``status: duplicate_delivery_skipped`` and the original run_id.

    Best-effort: any Supabase failure logs and returns None, so a
    transient observability outage degrades to "process every request"
    rather than "fail every request".

    Project codes are sorted before comparison so the dedupe key is
    independent of the caller's ordering — Fathom may not preserve
    array order between retries.
    """
    client = mc2_db.get_client(required=False)
    if client is None:
        return None
    sorted_codes = sorted(project_codes)
    try:
        # Filter to status='success' — a previous failure should NOT
        # block a fresh attempt. Limit 20 lets us tolerate a handful
        # of past runs against the same meeting while still being a
        # single-page query.
        resp = (
            client.table(Tables.AUTO_INGEST_RUNS)
            .select("id, project_codes")
            .eq("meeting_id", meeting_id)
            .eq("status", "success")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        for row in resp.data or []:
            row_codes = row.get("project_codes") or []
            if sorted(row_codes) == sorted_codes:
                return row.get("id")
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "duplicate-delivery check failed for meeting=%s: %s",
            meeting_id, exc,
        )
    return None


def _log_run_to_supabase(
    *,
    meeting_id: str,
    project_codes: list[str],
    status: str,
    ingested: list[dict],
    commit_sha: str | None,
    top_level_errors: list[str] | None = None,
) -> None:
    """Insert one row into auto_ingest_runs. Never raises.

    Observability is best-effort: a failure to log must not break the
    primary auto-ingest contract with fathom-meeting-sync. We log + swallow.

    `top_level_errors` captures failures that happen BEFORE the per-project
    loop runs (plan generation crashed, transcript fetch failed, etc.) —
    cases where `ingested=[]` so the per-project error aggregation has
    nothing to surface. Without this, top-level failures landed in
    auto_ingest_runs as status=failed but errors=null, leaving the
    dashboard unable to show what actually went wrong.
    """
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning("auto_ingest_runs insert skipped: Supabase env not set")
        return

    plan_summary = {
        entry["code"]: entry.get("plan_summary") or {}
        for entry in ingested
    }
    errors_flat: list[str] = list(top_level_errors or [])
    for entry in ingested:
        for err in entry.get("errors") or []:
            errors_flat.append(f"{entry['code']}: {err}")

    row = {
        "meeting_id": meeting_id,
        "project_codes": project_codes,
        "status": status,
        "plan_summary": plan_summary,
        "commit_sha": commit_sha,
        "errors": errors_flat or None,
    }
    cid = observability.current_correlation_id()
    if cid:
        row["correlation_id"] = cid
    try:
        try:
            client.table(Tables.AUTO_INGEST_RUNS).insert(row).execute()
        except Exception as exc:  # noqa: BLE001 — column-tolerant retry below
            # Pre-migration tolerance: until the `correlation_id` column
            # lands in the shared DB (mc-2 ledger), a PostgREST unknown-
            # column error must not cost us the whole run row.
            if "correlation_id" in row and "correlation_id" in str(exc):
                row.pop("correlation_id")
                client.table(Tables.AUTO_INGEST_RUNS).insert(row).execute()
            else:
                raise
    except Exception as exc:  # noqa: BLE001 — observability must never throw
        log.warning("auto_ingest_runs insert failed for %s: %s", meeting_id, exc)
        observability.capture(exc, area="auto_ingest_runs_insert")
