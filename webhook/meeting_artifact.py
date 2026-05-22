"""Per-meeting project artifacts — Thread 1 of the 2026-05-21 brainstorm.

Every tagged meeting produces, in each affected project's working dir, a
`meetings/` subdir holding two files:

    <project-dir>/meetings/<YYYY-MM-DD>-<slug>.md     synthesis + summary
    <project-dir>/meetings/<YYYY-MM-DD>-<slug>.txt    raw transcript

The `.md` is layered: a Claude "Deeper notes" synthesis first (the
read-this layer), then Fathom's verbatim summary as a baseline, then a
plain reference list of the meeting's action items (no checkboxes —
ClickUp owns task state), then a link to the sibling `.txt`.

Runs inline in the auto-ingest webhook after the sprint-file plan. The
synthesis is a SEPARATE Claude call from plan generation so a synthesis
failure can't break the plan and vice versa. Best-effort: failures are
logged and swallowed — a missing artifact must not break auto-ingest.

Design: cp/docs/plans/2026-05-21-deeper-transcripts-design.md
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from cp_engine.plan_from_transcript import _find_project_dir

log = logging.getLogger("cp-engine-webhook")

_SYNTHESIS_MODEL = "claude-opus-4-7"
_SYNTHESIS_MAX_TOKENS = 8192

_SYNTHESIS_PROMPT = """\
You are writing the "Deeper notes" section of a meeting record for a \
creative agency's project-tracking system. You are given the full raw \
transcript of a client or team meeting.

Fathom (an automated note-taker) already produced a generic summary of \
this meeting, which is stored separately. Your job is NOT to restate that \
summary — it is to go deeper. Surface what a generic summarizer misses.

Write concise markdown. Do NOT use a level-1 (`#`) or level-2 (`##`) \
heading anywhere — those are reserved for the surrounding document. Use \
level-3 (`###`) headings for your subsections, so they nest correctly \
under the "Deeper notes" section that wraps your output.

Cover, only where the transcript actually supports it, using these \
`###` subsections:

- `### Decisions` — what was decided, and critically the *rationale*: \
  why this choice over the alternatives.
- `### Open questions` — things raised but left unresolved; what still \
  needs an answer and from whom.
- `### Risk / friction signals` — hesitation, scope creep, blockers, \
  timeline pressure, client dissatisfaction, or anything that could \
  become a problem. Be specific about the signal.
- `### Notable positions` — on any contested point, who held which \
  view. Only where it genuinely matters who said what.

Be substantive but tight. Omit a subsection entirely if the transcript \
gives you nothing real for it — do not pad. If the meeting was thin, a \
short note is correct.

--- TRANSCRIPT ---
{transcript}
--- END TRANSCRIPT ---
"""


def _slugify(title: str) -> str:
    """Kebab-case a meeting title for use in a filename, truncated."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "meeting").lower()).strip("-")
    return (slug or "meeting")[:60]


def _call_claude_synthesis(transcript: str) -> str | None:
    """Run the Deeper-notes synthesis call. Returns markdown, or None on failure."""
    try:
        from anthropic import Anthropic
    except ImportError:
        log.warning("meeting-artifact: anthropic package not installed")
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.warning("meeting-artifact: ANTHROPIC_API_KEY not set")
        return None

    prompt = _SYNTHESIS_PROMPT.format(transcript=transcript)
    try:
        client = Anthropic(api_key=key)
        response = client.messages.create(
            model=_SYNTHESIS_MODEL,
            max_tokens=_SYNTHESIS_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — synthesis is best-effort
        log.warning("meeting-artifact: synthesis Claude call failed: %s", exc)
        return None

    if not response.content:
        return None
    text = getattr(response.content[0], "text", None)
    return text.strip() if text else None


def _render_action_items(action_items: list) -> str:
    """A plain reference list — no checkboxes. ClickUp owns task state."""
    lines: list[str] = []
    for item in action_items or []:
        if not isinstance(item, dict):
            continue
        desc = (item.get("description") or "").strip()
        if not desc:
            continue
        assignee = item.get("assignee") or {}
        who = assignee.get("name") or assignee.get("email") or "unassigned"
        url = item.get("recording_playback_url")
        line = f"- {desc} — {who}"
        if url:
            line += f" · [recording]({url})"
        lines.append(line)
    return "\n".join(lines) if lines else "_None recorded._"


def _build_markdown(
    *,
    project_label: str,
    meeting_title: str,
    meeting_date: str,
    recording_url: str | None,
    participants: list,
    duration_minutes: int | None,
    synthesis: str | None,
    fathom_summary: str | None,
    action_items: list,
    txt_filename: str,
    md_filename: str,
    meeting_id: str,
) -> str:
    """Assemble the layered per-meeting .md file."""
    names = []
    for p in participants or []:
        if isinstance(p, dict) and p.get("name"):
            names.append(p["name"])
        elif isinstance(p, str):
            names.append(p)
    participants_str = ", ".join(names) if names else "—"
    duration_str = f"{duration_minutes} min" if duration_minutes else "—"

    synthesis_body = synthesis or "_Synthesis unavailable for this meeting._"
    summary_body = (fathom_summary or "").strip() or "_No Fathom summary available._"

    # meeting_id in frontmatter is the stable key — a re-tag can
    # find-and-replace this file even if the title (and slug) changed.
    return (
        f"---\n"
        f"Project: {project_label}\n"
        f"Provenance: Version 01 | {meeting_date}\n"
        f"Filename: {md_filename}\n"
        f"Author: cp-engine\n"
        f"Meeting-ID: {meeting_id}\n"
        f"---\n\n"
        f"# {meeting_title} — {meeting_date}\n\n"
        f"**Meeting:** {recording_url or '—'}\n"
        f"**Participants:** {participants_str}\n"
        f"**Duration:** {duration_str}\n\n"
        f"## Deeper notes\n\n"
        f"{synthesis_body}\n\n"
        f"## Fathom summary\n\n"
        f"{summary_body}\n\n"
        f"## Action items\n\n"
        f"{_render_action_items(action_items)}\n\n"
        f"## Transcript\n\n"
        f"Full transcript: [`{txt_filename}`]({txt_filename})\n"
    )


def write_meeting_artifacts(
    *,
    tenant_root: Path,
    meeting: dict,
    transcript_text: str,
    project_codes: list[str],
) -> list[Path]:
    """Generate + write the per-meeting artifact pair for each project.

    `meeting` is a dict with keys: id, title, meeting_date, summary,
    action_items, participants, duration_minutes (as available from
    fathom_meetings).

    The same .md + .txt pair is written into every resolvable project's
    `meetings/` dir (account / sprint-planning meetings touch many).
    Returns the list of files written, for the caller to commit.

    Never raises — best-effort; a failure logs and returns what it has.
    """
    written: list[Path] = []
    try:
        meeting_id = meeting.get("id") or "unknown"
        title = meeting.get("title") or "Meeting"
        # meeting_date may be a full timestamp; keep only the date.
        raw_date = str(meeting.get("meeting_date") or "")
        meeting_date = raw_date[:10] if raw_date else "unknown-date"

        slug = _slugify(title)
        base = f"{meeting_date}-{slug}"
        md_filename = f"{base}.md"
        txt_filename = f"{base}.txt"

        # One Claude synthesis call, shared across all project copies.
        synthesis = _call_claude_synthesis(transcript_text)

        for code in project_codes:
            project_dir = _find_project_dir(tenant_root, code)
            if project_dir is None:
                log.info("meeting-artifact: no project dir for code=%s", code)
                continue
            meetings_dir = project_dir / "meetings"
            meetings_dir.mkdir(parents=True, exist_ok=True)

            md_text = _build_markdown(
                project_label=code,
                meeting_title=title,
                meeting_date=meeting_date,
                recording_url=meeting.get("recording_url")
                or meeting.get("fathom_url"),
                participants=meeting.get("participants") or [],
                duration_minutes=meeting.get("duration_minutes"),
                synthesis=synthesis,
                fathom_summary=meeting.get("summary"),
                action_items=meeting.get("action_items") or [],
                txt_filename=txt_filename,
                md_filename=md_filename,
                meeting_id=meeting_id,
            )

            md_path = meetings_dir / md_filename
            txt_path = meetings_dir / txt_filename
            # Deterministic paths — a re-tag overwrites in place rather
            # than duplicating.
            md_path.write_text(md_text, encoding="utf-8")
            txt_path.write_text(transcript_text, encoding="utf-8")
            written.extend([md_path, txt_path])
            log.info(
                "meeting-artifact: wrote %s + .txt for project=%s",
                md_filename, code,
            )
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("meeting-artifact: generation failed: %s", exc)

    return written
