"""Retrospective layer — the living per-project meeting timeline.

Part B of the spine-inversion design: every Fathom meeting that touches a
project leaves a dated entry in that project's Retrospective element. The
Fathom `summary` is embedded WHOLE (anti-compression) — structured pointers
(decisions, action items, links) are *added*, never a replacement for the
narrative summary.

`build_entry` is a pure renderer for one entry. `append_entry` does the file
IO: it creates the per-project history file with parseable frontmatter on
first call, then appends idempotently keyed on the meeting id (re-ingesting the
same meeting is a no-op).

WHERE IT LIVES (architecture step 4c, 2026-10-01). The history is an ordinary
hand-owned tenant file at ``<workstream dir>/meeting-history.md``. It used to
sit at ``spine/Retrospective/meeting-history.md``, but ``spine/`` is now a
view generated from MC-2 on every sync, and the history has no MC-2 copy (its
table was dropped in mig 072): left there, the first render would quarantine
and delete ~390k words. ``history_path`` is the ONE resolver every reader and
writer uses. Until the one-time move (``scripts/move_meeting_history.py``) has
run for a workstream, it returns the legacy file when only that exists, so an
append never forks one history into two files.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Sequence

#: The history file's home, relative to the workstream dir.
HISTORY_FILENAME = "meeting-history.md"
#: Where it lived before step 4c (inside the generated ``spine/`` view).
LEGACY_RELPATH = Path("spine") / "Retrospective" / HISTORY_FILENAME


def history_path(project_dir: Path) -> Path:
    """The workstream's meeting-history file — the path to read AND append.

    ``<project_dir>/meeting-history.md``, except while a workstream still has
    only the legacy ``spine/Retrospective/meeting-history.md`` (the move script
    has not run): then the legacy file, so writes keep landing on the one
    history that exists rather than starting a second."""
    new = project_dir / HISTORY_FILENAME
    legacy = project_dir / LEGACY_RELPATH
    if not new.exists() and legacy.exists():
        return legacy
    return new


def read_history(project_dir: Path) -> str | None:
    """The history file's text, or None when the workstream has none."""
    path = history_path(project_dir)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _meeting_marker(meeting_id: str) -> str:
    """Idempotency trailer, mirroring ingest.py's `<!-- cp:hash=... -->` style."""
    return f"<!-- cp:meeting={meeting_id} -->"


def build_entry(
    *,
    date: str,
    title: str,
    speakers: Sequence[str],
    summary: str,
    decisions: Sequence[str],
    action_items: Sequence[str],
    recording_url: str | None = None,
    transcript_link: str | None = None,
    meeting_id: str | None = None,
) -> str:
    """Render one dated Retrospective entry (a markdown fragment, not a file).

    The Fathom `summary` goes in WHOLE — structured pointers are ADDED, never
    replace it. See the format spec in Part B of the spine-inversion design.
    """
    speaker_list = [s for s in speakers if s]
    if speaker_list:
        header = f"### {date} · {title} ({', '.join(speaker_list)})"
    else:
        header = f"### {date} · {title}"

    parts: list[str] = [header, "", summary, ""]

    if decisions:
        parts.append(f"**Decisions:** {' · '.join(decisions)}")
    if action_items:
        parts.append(f"**Action items:** {' · '.join(action_items)}")

    links: list[str] = []
    if recording_url:
        links.append(f"[Fathom recording]({recording_url})")
    if transcript_link:
        links.append(f"[transcript]({transcript_link})")
    if links:
        parts.append(" · ".join(links))

    if meeting_id:
        parts.append(_meeting_marker(meeting_id))

    return "\n".join(parts).rstrip() + "\n"


def _frontmatter(*, code: str, project: str, today: date) -> str:
    """The Retrospective element's frontmatter — parseable by spine.parse_element."""
    return (
        "---\n"
        f"id: {code}/retrospective/meeting-history\n"
        f"project: {project}\n"
        "layer: Retrospective\n"
        "title: Meeting history\n"
        "type: retrospective\n"
        "status: active\n"
        f"last_touched: {today.isoformat()}\n"
        "---\n"
    )


def _bump_last_touched(frontmatter_block: str, today: date) -> str:
    """Replace the `last_touched:` line in an existing frontmatter block."""
    lines = frontmatter_block.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("last_touched:"):
            lines[i] = f"last_touched: {today.isoformat()}"
            break
    return "\n".join(lines) + "\n"


def append_entry(
    history_path: Path,
    meeting_id: str,
    entry_md: str,
    *,
    code: str,
    project: str,
    today: date,
) -> bool:
    """Append `entry_md` to the project's Retrospective element.

    Creates the file with frontmatter + a `# Meeting history` H1 on first call.
    Idempotent on `meeting_id`: if `<!-- cp:meeting=<meeting_id> -->` already
    appears in the file, do nothing and return False. Otherwise insert the
    entry at the TOP (newest first, just under the H1) so the latest meeting is
    most visible, bump `last_touched`, and return True.
    """
    history_path.parent.mkdir(parents=True, exist_ok=True)

    if not history_path.exists():
        content = (
            _frontmatter(code=code, project=project, today=today)
            + "\n# Meeting history\n\n"
            + entry_md.rstrip()
            + "\n"
        )
        history_path.write_text(content)
        return True

    existing = history_path.read_text()
    if _meeting_marker(meeting_id) in existing:
        return False

    # Split off the frontmatter block so we can (a) bump last_touched and
    # (b) insert the new entry just below the H1, keeping newest-first order.
    if existing.startswith("---\n"):
        end = existing.find("\n---\n", 4)
        fm_block = existing[: end + len("\n---\n")]
        rest = existing[end + len("\n---\n") :]
        fm_block = _bump_last_touched(fm_block, today)
    else:
        fm_block = ""
        rest = existing

    # Insert the new entry directly after the `# Meeting history` H1 if present,
    # otherwise at the very top of the body.
    h1 = "# Meeting history\n"
    idx = rest.find(h1)
    if idx != -1:
        after = idx + len(h1)
        body = (
            rest[:after]
            + "\n"
            + entry_md.rstrip()
            + "\n\n"
            + rest[after:].lstrip("\n")
        )
    else:
        body = entry_md.rstrip() + "\n\n" + rest.lstrip("\n")

    history_path.write_text(fm_block + body)
    return True
