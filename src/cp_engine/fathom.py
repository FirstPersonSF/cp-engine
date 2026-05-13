"""Fathom bridge — Tier 1 Phase 1.3 / v0.8.7.

Reads from `fathom_meetings` (the same Supabase project MC-2 uses; populated
by the fathom-meeting-sync webhook live at /Users/drewf/Documents/Python/
fathom-meeting-sync). Doesn't reimplement Fathom ingest — bridges the
already-ingested data into the cp tenant.

Three CLI surfaces (wired in cli.py):
- `cp fathom-list --since <ts>` → JSON list of meetings newer than <ts>
- `cp fathom-fetch <meeting-id>` → stages transcript to
  `transcripts/incoming/<meeting-id>.txt` for later /cp-ingest
- `cp ingest --from-fathom <meeting-id>` → fetches + delegates to existing
  /cp-ingest plugin orchestration

Plus an auto-poll mode (CLI: `cp fathom-auto-poll`) that's wired to a
GitHub Actions cron (workflows/fathom-auto-poll.yml). Uses a confidence
gate: meetings with non-empty + non-`untagged` `project_tags` proceed
through the full /cp-ingest plan flow; everything else lands in
`transcripts/needs-review/<id>.txt` for manual handling.

Idempotency: per-tenant state file at `.cp-engine/state.json` (gitignored)
tracks `last_polled_at` + `processed_ids`. Fast lookup; survives by being
in the cp tenant root.

Auth reuses `_load_supabase_creds` from sync_mc2 — same project, same keys.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from supabase import Client, create_client

from cp_engine.config import TenantConfig
from cp_engine.sync_mc2 import _load_supabase_creds

# Where the bridge stages transcripts before/after ingest.
INCOMING_DIR = "transcripts/incoming"
NEEDS_REVIEW_DIR = "transcripts/needs-review"

# Per-tenant state file. Gitignored. Holds last_polled_at + processed_ids.
STATE_FILE = ".cp-engine/state.json"

# Tags that indicate the auto-tagger ran but produced no real classification.
# These trip the confidence gate → meeting goes to needs-review.
_NO_REAL_TAG = ("untagged", "")


@dataclass
class FathomMeetingSummary:
    """Lightweight row for `cp fathom-list` output."""

    id: str
    title: str
    meeting_date: str  # ISO timestamp
    project_tags: list[str] = field(default_factory=list)
    duration_minutes: int | None = None
    # Phase A from cp/docs/plans/2026-05-12-meeting-type-cascade.md.
    # One of: project-status, account-status, sprint-planning,
    # work-session, 1-1, untagged. Drives auto-poll routing in Phase C
    # and the recurring-meeting cadence tracker in Phase D.
    meeting_type: str = "untagged"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "meeting_date": self.meeting_date,
            "project_tags": self.project_tags,
            "duration_minutes": self.duration_minutes,
            "meeting_type": self.meeting_type,
        }


@dataclass
class FathomMeetingFull:
    """Full meeting payload for `cp fathom-fetch`. Includes transcript."""

    id: str
    title: str
    meeting_date: str
    project_tags: list[str]
    transcript: str
    summary: str | None = None
    duration_minutes: int | None = None
    # See FathomMeetingSummary.meeting_type docstring (Phase A).
    meeting_type: str = "untagged"


@dataclass
class FathomStateFile:
    """The persisted state at `.cp-engine/state.json` (`fathom` key)."""

    last_polled_at: str | None = None
    processed_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "last_polled_at": self.last_polled_at,
            "processed_ids": self.processed_ids,
        }


# ──────────────────────────────────────────────────────────────────────
#  Supabase client (reuses MC-2 creds)
# ──────────────────────────────────────────────────────────────────────


_client_cache: Client | None = None


def get_client(config: TenantConfig) -> Client:
    """Construct (and cache) a Supabase client for the fathom_meetings table.

    Reuses the MC-2 credential resolution path: env vars first, then
    `<mc-2 clone>/backend/.env`. Same Supabase project as MC-2 (verified
    in the W19 retro), so no separate auth surface.
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    url, key = _load_supabase_creds(config)
    _client_cache = create_client(url, key)
    return _client_cache


# ──────────────────────────────────────────────────────────────────────
#  fathom-list
# ──────────────────────────────────────────────────────────────────────


def list_meetings(
    config: TenantConfig,
    *,
    since_iso: str | None = None,
    limit: int = 50,
    meeting_type: str | None = None,
) -> list[FathomMeetingSummary]:
    """List meetings from `fathom_meetings`, newest first.

    Args:
        since_iso: only return meetings with `meeting_date > since_iso`.
            None → return the most recent `limit` meetings.
        limit: max rows to return (default 50).
        meeting_type: filter by meeting_type (Phase B). One of the
            6 taxonomy values (project-status, account-status,
            sprint-planning, work-session, 1-1, untagged) or None for
            no filter. Used by /cp-ingest --account to pull just
            account-status meetings.

    Output rows have lightweight fields only — full transcript is
    fetched separately via `fetch_meeting`.
    """
    client = get_client(config)
    query = (
        client.table("fathom_meetings")
        .select(
            "id, title, meeting_date, project_tags, duration_minutes, meeting_type"
        )
        .order("meeting_date", desc=True)
        .limit(limit)
    )
    if since_iso:
        query = query.gt("meeting_date", since_iso)
    if meeting_type:
        query = query.eq("meeting_type", meeting_type)
    rows = query.execute().data or []
    return [
        FathomMeetingSummary(
            id=r["id"],
            title=r.get("title") or "",
            meeting_date=r.get("meeting_date") or "",
            project_tags=list(r.get("project_tags") or []),
            duration_minutes=r.get("duration_minutes"),
            meeting_type=r.get("meeting_type") or "untagged",
        )
        for r in rows
    ]


# ──────────────────────────────────────────────────────────────────────
#  fathom-fetch
# ──────────────────────────────────────────────────────────────────────


def fetch_meeting(config: TenantConfig, meeting_id: str) -> FathomMeetingFull:
    """Pull a single meeting row by id, including the full transcript."""
    client = get_client(config)
    rows = (
        client.table("fathom_meetings")
        .select(
            "id, title, meeting_date, project_tags, transcript, summary, "
            "duration_minutes, meeting_type"
        )
        .eq("id", meeting_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise ValueError(f"no fathom_meetings row found for id {meeting_id}")
    r = rows[0]
    return FathomMeetingFull(
        id=r["id"],
        title=r.get("title") or "",
        meeting_date=r.get("meeting_date") or "",
        project_tags=list(r.get("project_tags") or []),
        transcript=r.get("transcript") or "",
        summary=r.get("summary"),
        duration_minutes=r.get("duration_minutes"),
        meeting_type=r.get("meeting_type") or "untagged",
    )


def stage_transcript(
    meeting: FathomMeetingFull,
    *,
    tenant_root: Path,
    needs_review: bool = False,
) -> Path:
    """Write the transcript to a staged path under the tenant root.

    Filename is ``<YYYY-MM-DD>-<slugified-title>.txt`` with ``-2``, ``-3``…
    suffixes appended on collision (common with repeated titles like
    "Impromptu Zoom Meeting"). The meeting id is preserved inside the
    file's metadata header so idempotency in the auto-poll state file
    (which keys by id) still works.

    Args:
        needs_review: if True, write to `transcripts/needs-review/` instead
            of `transcripts/incoming/`. Used when the confidence gate
            (auto-tagger produced empty/`untagged` tags) trips.

    Returns: the absolute path of the staged file.
    """
    subdir = NEEDS_REVIEW_DIR if needs_review else INCOMING_DIR
    target_dir = tenant_root / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    # Build a readable filename: <date>-<slug>.txt with collision suffix.
    target_path = _resolve_target_path(target_dir, meeting)
    header = (
        f"# Fathom meeting: {meeting.title}\n"
        f"# id: {meeting.id}\n"
        f"# meeting_date: {meeting.meeting_date}\n"
        f"# meeting_type: {meeting.meeting_type}\n"
        f"# project_tags: {', '.join(meeting.project_tags) or '(none)'}\n"
        f"# duration_minutes: {meeting.duration_minutes or 'unknown'}\n"
        f"# (staged by cp fathom-fetch — feed to /cp-ingest <path>)\n"
        "\n---\n\n"
    )
    body = _render_transcript_body(meeting.transcript)
    target_path.write_text(header + body)
    return target_path


def _render_transcript_body(raw: object) -> str:
    """Render a Supabase-shape transcript into the format cp parse-transcript expects.

    Supabase stores transcripts as a JSONB array of utterance objects:
        [{"text": "...", "speaker": {"display_name": "..."}, "timestamp": "HH:MM:SS"}, ...]

    cp parse-transcript expects Fathom export format:
        MM:SS - Speaker Name (label)
          utterance text

    Handles three input shapes for resilience:
    - list[dict] (the Supabase JSONB shape) — render to Fathom format
    - str (some legacy rows or future schema changes) — pass through
    - None / empty — render the (no transcript) placeholder
    """
    if not raw:
        return "(no transcript)"
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return f"(unexpected transcript shape: {type(raw).__name__})"

    lines: list[str] = []
    for utt in raw:
        if not isinstance(utt, dict):
            continue
        ts_raw = (utt.get("timestamp") or "").strip()
        speaker_obj = utt.get("speaker") or {}
        if not isinstance(speaker_obj, dict):
            speaker_obj = {}
        speaker = (speaker_obj.get("display_name") or "Unknown").strip()
        text = (utt.get("text") or "").strip()

        # cp parse-transcript's _TIMESTAMP_LINE_RE matches MM:SS or HH:MM:SS.
        # Supabase stores HH:MM:SS; strip leading "00:" if present so the
        # output matches Fathom's typical export style for short meetings.
        ts = ts_raw
        if ts.startswith("00:") and ts.count(":") == 2:
            ts = ts[3:]  # "00:15:21" -> "15:21"

        lines.append(f"{ts} - {speaker}")
        if text:
            lines.append(f"  {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────────────
#  Filename slugging (for staged transcripts)
# ──────────────────────────────────────────────────────────────────────


def _slugify_title(title: str) -> str:
    """Lowercase, replace non-alphanumeric runs with hyphens, trim.

    Caps at 60 chars to keep filenames manageable on macOS/Linux/Windows.
    Empty input → 'untitled'.
    """
    if not title:
        return "untitled"
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    if not slug:
        return "untitled"
    return slug[:60].rstrip("-")


def _date_prefix(meeting_date_iso: str) -> str:
    """Extract YYYY-MM-DD prefix from a Supabase ISO timestamp.

    Returns 'undated' if parsing fails.
    """
    if not meeting_date_iso:
        return "undated"
    return meeting_date_iso[:10] if len(meeting_date_iso) >= 10 else "undated"


def _resolve_target_path(target_dir: Path, meeting: FathomMeetingFull) -> Path:
    """Pick a non-colliding filename `<date>-<slug>.txt` in target_dir.

    On collision (same date + slug — common for repeated titles like
    "Impromptu Zoom Meeting"), append `-2`, `-3`, etc. until free.

    Idempotency note: the meeting id is preserved inside the file's
    metadata header, so the auto-poll state file (which keys by id)
    correctly skips already-processed meetings even when filenames vary.
    """
    base = f"{_date_prefix(meeting.meeting_date)}-{_slugify_title(meeting.title)}"
    candidate = target_dir / f"{base}.txt"
    if not candidate.exists():
        return candidate
    # Collision — append -2, -3, etc.
    n = 2
    while True:
        candidate = target_dir / f"{base}-{n}.txt"
        if not candidate.exists():
            return candidate
        n += 1


# ──────────────────────────────────────────────────────────────────────
#  Confidence gate (for auto-poll)
# ──────────────────────────────────────────────────────────────────────


def has_good_tags(tags: list[str]) -> bool:
    """True if the auto-tagger produced at least one real project tag.

    Empty list → False. List containing only 'untagged' (or empty strings)
    → False. List with at least one non-trivial tag → True.

    Used by auto-poll to decide between proceeding to /cp-ingest vs.
    staging to needs-review for manual handling.
    """
    if not tags:
        return False
    return any(t and t.lower() not in _NO_REAL_TAG for t in tags)


# ──────────────────────────────────────────────────────────────────────
#  State file (idempotency)
# ──────────────────────────────────────────────────────────────────────


def load_state(tenant_root: Path) -> FathomStateFile:
    """Load the per-tenant state file. Returns empty state if missing."""
    state_path = tenant_root / STATE_FILE
    if not state_path.is_file():
        return FathomStateFile()
    try:
        data = json.loads(state_path.read_text()).get("fathom") or {}
    except (json.JSONDecodeError, OSError):
        return FathomStateFile()
    return FathomStateFile(
        last_polled_at=data.get("last_polled_at"),
        processed_ids=list(data.get("processed_ids") or []),
    )


def save_state(state: FathomStateFile, tenant_root: Path) -> None:
    """Persist the state file. Creates parent dir if needed."""
    state_path = tenant_root / STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve any non-fathom keys in the state file.
    full: dict = {}
    if state_path.is_file():
        try:
            full = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            full = {}
    full["fathom"] = state.to_dict()
    state_path.write_text(json.dumps(full, indent=2))


def already_processed(state: FathomStateFile, meeting_id: str) -> bool:
    return meeting_id in state.processed_ids


def mark_processed(
    state: FathomStateFile, meeting_id: str, *, polled_at: str | None = None
) -> FathomStateFile:
    """Return a new state with this id added + last_polled_at updated."""
    new_ids = list(state.processed_ids)
    if meeting_id not in new_ids:
        new_ids.append(meeting_id)
    return FathomStateFile(
        last_polled_at=polled_at or _now_iso(),
        processed_ids=new_ids,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
