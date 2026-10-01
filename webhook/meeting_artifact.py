"""Per-meeting project artifacts — Thread 1 of the 2026-05-21 brainstorm.

Every tagged meeting produces, in each affected project's working dir, a
`meetings/` subdir holding two files:

    <project-dir>/meetings/<YYYY-MM-DD>-<slug>.md     summary + action items
    <project-dir>/meetings/<YYYY-MM-DD>-<slug>.txt    raw transcript

The `.md` carries Fathom's verbatim summary, a plain reference list of the
meeting's action items (no checkboxes — commitments own task state), then a
link to the sibling `.txt`. No model call: the Claude "Deeper notes"
synthesis that used to head the file was retired in architecture step 5a —
nothing read it but a person opening the file, and it was a second Anthropic
call on every ingest. Files written before then keep their section.

Runs inline in the auto-ingest webhook after the sprint-file plan.
Best-effort: failures are logged and swallowed — a missing artifact must not
break auto-ingest.

Design: cp/docs/plans/2026-05-21-deeper-transcripts-design.md
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from cp_engine.plan_from_transcript import _find_project_dir

log = logging.getLogger("cp-engine-webhook")


def _slugify(title: str) -> str:
    """Kebab-case a meeting title for use in a filename, truncated."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "meeting").lower()).strip("-")
    return (slug or "meeting")[:60]


_MEETING_ID_LINE = re.compile(r"^Meeting-ID: *(.*?) *$", re.MULTILINE)


def _resolve_base(meetings_dir: Path, base: str, meeting_id: str) -> str:
    """The filename stem for this meeting's pair in `meetings_dir` (#307).

    `<date>-<slug>` is shared by every same-day meeting with the same title
    (Zoom's default "Impromptu Zoom Meeting"), but a meeting's identity is
    its id. The plain stem is kept when it is free or already holds this
    meeting (a re-tag overwrites in place); otherwise the stem takes an
    id suffix, which stays stable across re-runs without a directory scan.
    """
    existing = meetings_dir / f"{base}.md"
    if not existing.exists():
        return base
    m = _MEETING_ID_LINE.search(existing.read_text(encoding="utf-8"))
    if m and m.group(1) == meeting_id:
        return base
    return f"{base}-{meeting_id[:8]}"


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
    errors: list[str] | None = None,
) -> list[Path]:
    """Generate + write the per-meeting artifact pair for each project.

    `meeting` is a dict with keys: id, title, meeting_date, summary,
    action_items, participants, duration_minutes (as available from
    fathom_meetings).

    The same .md + .txt pair is written into every resolvable project's
    `meetings/` dir (account / sprint-planning meetings touch many).
    Returns the list of files written, for the caller to commit.

    Never raises — best-effort; a failure logs and returns what it has.
    `errors` (step 3): when given, a failure is ALSO appended there so the
    caller's run record carries it instead of only a log line.
    """
    written: list[Path] = []
    try:
        meeting_id = meeting.get("id") or "unknown"
        title = meeting.get("title") or "Meeting"
        # meeting_date may be a full timestamp; keep only the date.
        raw_date = str(meeting.get("meeting_date") or "")
        meeting_date = raw_date[:10] if raw_date else "unknown-date"

        base = f"{meeting_date}-{_slugify(title)}"

        for code in project_codes:
            project_dir = _find_project_dir(tenant_root, code)
            if project_dir is None:
                log.info("meeting-artifact: no project dir for code=%s", code)
                continue
            meetings_dir = project_dir / "meetings"
            meetings_dir.mkdir(parents=True, exist_ok=True)
            # Resolved per project: each dir holds its own set of meetings.
            stem = _resolve_base(meetings_dir, base, meeting_id)
            md_filename = f"{stem}.md"
            txt_filename = f"{stem}.txt"

            md_text = _build_markdown(
                project_label=code,
                meeting_title=title,
                meeting_date=meeting_date,
                recording_url=meeting.get("recording_url")
                or meeting.get("fathom_url"),
                participants=meeting.get("participants") or [],
                duration_minutes=meeting.get("duration_minutes"),
                fathom_summary=meeting.get("summary"),
                action_items=meeting.get("action_items") or [],
                txt_filename=txt_filename,
                md_filename=md_filename,
                meeting_id=meeting_id,
            )

            md_path = meetings_dir / md_filename
            txt_path = meetings_dir / txt_filename
            # Deterministic per meeting id — a re-tag overwrites in place
            # rather than duplicating; a same-titled meeting gets its own pair.
            md_path.write_text(md_text, encoding="utf-8")
            txt_path.write_text(transcript_text, encoding="utf-8")
            written.extend([md_path, txt_path])
            log.info(
                "meeting-artifact: wrote %s + .txt for project=%s",
                md_filename, code,
            )
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("meeting-artifact: generation failed: %s", exc)
        if errors is not None:
            errors.append(f"artifact generation failed after {len(written)} file(s): "
                          f"{type(exc).__name__}: {exc}")

    return written
