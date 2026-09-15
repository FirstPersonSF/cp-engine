"""One-line summary derivation for the master-CP.

Strategy: the master-CP one-liner is derived from the project CP's
model-authored ``exec-summary`` region during sync. Specifically:

1. Look at the exec-summary region's `**Status:**` field (the one-phrase
   field, ideal for a one-line summary). If authored, use it.
2. Otherwise, the first real `Where it stands` bullet in that region.
3. Otherwise, the `## Current Work` section's first non-placeholder
   paragraph (legit fallback while the exec-summary is unauthored).
4. Otherwise, return None — the master-CP shows an empty cell. The column
   activates the moment content exists; until then, it's inert.

Hard cap: ≤120 characters, single sentence, no markdown. Truncates with `…`.

Pure-Python heuristic; no LLM call. Deterministic, free, fast. If the
heuristic proves too crude, swap in an LLM call later without changing
the public function shape.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START

logger = logging.getLogger(__name__)

MAX_SUMMARY_LEN = 120

# Sentinel that marks a CP section as untouched (matches the template's
# placeholder lines). Anything containing this is ignored.
_PLACEHOLDER_PATTERN = re.compile(r"_<[^>]+>_")


def enforce_summary_cap(summary: str) -> str:
    """Trim `summary` to ≤120 chars, single line, no markdown.

    Strips newlines and common markdown markers. Truncates with `…` if
    over-length, logged at DEBUG.

    Deliberately NOT a warning (#197): the cap is a designed constraint on a
    master-CP cell, and most project summaries are longer than 120 chars — a
    typical tenant-wide sync trips it ~23 times. Logging routine, expected
    behavior at WARNING drowns the warnings that mean something (one stranded
    element hid among 23 of these) and teaches the reader to ignore the count.
    """
    cleaned = " ".join(summary.split())
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    if len(cleaned) > MAX_SUMMARY_LEN:
        logger.debug("Summary truncated: %d chars → %d", len(cleaned), MAX_SUMMARY_LEN)
        cleaned = cleaned[: MAX_SUMMARY_LEN - 1].rstrip() + "…"
    return cleaned


def derive_from_project_cp(file_path: Path) -> str | None:
    """Read a project CP and derive its one-line summary.

    Tries (in order):
    1. The exec-summary region's `**Status:**` field value
    2. The first real `Where it stands` bullet in the exec-summary region
    3. First non-placeholder paragraph in `## Current Work`

    Returns None if none yields content. The master CP renders an
    empty cell when None.
    """
    if not file_path.exists():
        return None

    try:
        contents = file_path.read_text()
    except OSError:
        logger.exception("Could not read %s", file_path)
        return None

    summary = (
        _extract_exec_summary_status_or_where(contents)
        or _extract_current_work_first_paragraph(contents)
    )
    if summary is None:
        return None
    return enforce_summary_cap(summary)


# The `## Exec Summary  ·  updated <date>` heading stamp. Mirrors
# prep_planning._EXEC_SUMMARY_STAMP_RE — kept as its own copy because this
# module deliberately imports nothing from prep_planning (a sync-path module
# must not pull in the planning stack).
_EXEC_SUMMARY_STAMP_RE = re.compile(
    r"^##\s+Exec Summary\s*·\s*updated\s+(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)

# How far the summary may fall behind real activity before it is called out.
# 30 days is a sprint-and-a-half: long enough that a quiet engagement does not
# trip it, short enough that "this is current" stops being a safe assumption.
STALE_AFTER_DAYS = 30


def exec_summary_updated_on(file_path: Path) -> date | None:
    """The date this project CP's Exec Summary was last hand-written.

    Reads the `## Exec Summary · updated <date>` heading stamp. Returns None
    when the CP is missing, unreadable, or predates the stamp — an unstamped
    summary is NOT treated as stale, because we cannot tell the difference
    between old and merely unstamped, and guessing would flag the whole tenant.
    """
    if not file_path.exists():
        return None
    try:
        contents = file_path.read_text()
    except OSError:
        logger.exception("Could not read %s", file_path)
        return None

    m = _EXEC_SUMMARY_STAMP_RE.search(contents)
    if m is None:
        return None
    try:
        return date.fromisoformat(m.group("date"))
    except ValueError:
        logger.warning("Unparseable Exec Summary stamp in %s", file_path)
        return None


def summary_stale_days(
    updated_on: date | None,
    last_activity: date | None,
) -> int | None:
    """Days the hand-written summary trails real project activity, or None.

    WHY THIS EXISTS. The master-CP one-liner is derived from the Exec Summary,
    which is hand-written prose — no sync, spine write or ingest ever refreshes
    it. So a project can be worked on daily while its summary sits untouched
    for months, and the index renders that old prose as though it were current
    fact. Measured 2026-09-14: seven Google engagements shared an identical
    `updated 2026-07-14` stamp while their spines had been written that same
    morning — a 62-day gap, invisible in the table.

    A stale summary is worse than an empty one: empty reads as "nobody has
    said", stale reads as "this is how it is". Same reason a figure that
    cannot be computed prints an em dash instead of a confident `$0`.

    Returns None when either date is unknown (nothing to compare) or when the
    gap is within STALE_AFTER_DAYS. Never returns a negative: a summary written
    AFTER the last spine write is current, not stale by -7 days.
    """
    if updated_on is None or last_activity is None:
        return None
    gap = (last_activity - updated_on).days
    return gap if gap >= STALE_AFTER_DAYS else None


def latest_recorded_signal(
    tenant_root: Path,
    project_code: str,
    *,
    newer_than: date | None = None,
) -> tuple[str, date] | None:
    """The most recent dated decision recorded for this project, if any.

    WHY THIS EXISTS. `summary_stale_days` says a summary has stopped tracking
    the work; it cannot say what the work is now. That answer already exists —
    auto-ingest writes dated `[decision · YYYY-MM-DD]` bullets into the sprint
    file on every meeting — but no renderer reads it, so the index shows
    months-old prose while a current statement sits one directory away.
    Measured 2026-09-14: all 13 stale engagements had a decision newer than
    their summary; ggl-5136's summary said July while its sprint file carried
    a 09-01 decision.

    This READS ONLY. The engine owns scaffold/read/render and the model owns
    all prose (`docs/plans/2026-06-30-exec-summary.md`) — so this surfaces a
    sentence a person already wrote, beside the summary, and authors nothing.
    The Exec Summary remains the only thing claiming to BE the summary.

    `newer_than` filters to decisions after that date (pass the summary's own
    stamp) so a current summary is never second-guessed by an older bullet.
    Returns (text, date) for the newest match, or None.
    """
    from cp_engine.sprints import parse_sprint_file

    sprints = tenant_root / "sprints"
    if not sprints.is_dir():
        return None

    best: tuple[str, date] | None = None
    # Newest week first: the answer is nearly always in the last week or two,
    # and an older week can only lose the max() comparison below.
    for week_dir in sorted(sprints.iterdir(), reverse=True):
        path = week_dir / f"{project_code}.md"
        if not path.is_file():
            continue
        try:
            parsed = parse_sprint_file(path)
        except Exception:  # noqa: BLE001 — one malformed file must not
            # break a tenant-wide sync; the surface is advisory.
            logger.warning("Could not parse sprint file %s", path, exc_info=True)
            continue
        for entry in getattr(parsed, "decisions", ()) or ():
            when = _parse_iso_date(getattr(entry, "date", None))
            text = (getattr(entry, "text", "") or "").strip()
            if when is None or not text:
                continue
            if newer_than is not None and when <= newer_than:
                continue
            if best is None or when > best[1]:
                best = (enforce_summary_cap(text), when)
    return best


def _parse_iso_date(value: str | None) -> date | None:
    """An ISO date from a bullet's `[decision · <date>]` marker, or None.

    Dates in sprint bullets are model-written, so a malformed one is a normal
    input rather than an error — it is skipped, never raised.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


def _extract_exec_summary_status_or_where(contents: str) -> str | None:
    """Derive a one-liner from the model-authored ``exec-summary`` region.

    The region (between EXEC_SUMMARY_START/END markers) is structured as:
        **Last session:** <date>
        **Objective:** <one line>
        **Status:** <current state in a phrase>
        **Where it stands:**
        - <bullets of current reality>
        ...

    Prefer the `**Status:**` field value (a designed one-phrase field).
    If Status is absent or a `_<...>_` placeholder, fall back to the first
    real (non-placeholder) `Where it stands` bullet. Returns None when the
    region is absent or both candidates are placeholders.

    Note: this extracts a SPECIFIC field for the master-cp one-liner, so it
    uses per-candidate `_PLACEHOLDER_PATTERN` checks rather than the shared
    ``render.exec_summary_is_authored`` boolean (which the region-slicing
    readers in ``prep_planning`` / ``agenda`` use). The two agree on what
    counts as real content — a value that trips one trips the other.
    """
    start = contents.find(EXEC_SUMMARY_START)
    if start == -1:
        return None
    end = contents.find(EXEC_SUMMARY_END, start)
    if end == -1:
        return None
    region = contents[start + len(EXEC_SUMMARY_START) : end]

    # 1. Status field — the designed one-phrase summary.
    status = re.search(r"^\*\*Status:\*\*[ \t]*(.+?)[ \t]*$", region, re.MULTILINE)
    if status:
        value = status.group(1).strip()
        if value and not _PLACEHOLDER_PATTERN.search(value):
            return value

    # 2. First real bullet under `**Where it stands:**`.
    where = re.search(
        r"^\*\*Where it stands:\*\*\s*\n(.+?)(?=^\*\*|\Z)",
        region,
        re.DOTALL | re.MULTILINE,
    )
    if where:
        for line in where.group(1).splitlines():
            bullet = line.strip()
            if not bullet.startswith("- "):
                continue
            value = bullet[2:].strip()
            if value and not _PLACEHOLDER_PATTERN.search(value):
                return value
    return None


def _extract_current_work_first_paragraph(contents: str) -> str | None:
    """Pull the first non-placeholder paragraph from the `## Current Work`
    section. Used as fallback when Quick Resume isn't filled in but Current
    Work has body content."""
    section = _section_body(contents, "Current Work")
    if section is None:
        return None

    # Split on blank lines into paragraphs; return first that's not a placeholder.
    for paragraph in re.split(r"\n\s*\n", section):
        cleaned = paragraph.strip()
        if not cleaned:
            continue
        if _PLACEHOLDER_PATTERN.search(cleaned):
            continue
        return cleaned
    return None


def _section_body(contents: str, heading: str) -> str | None:
    """Return the body of `## <heading>` from a markdown file, or None.

    The body is everything between this heading and the next `## ` heading
    (or end of file). Heading match is exact; case-sensitive.
    """
    pattern = rf"^##\s+{re.escape(heading)}\s*\n(.+?)(?=^##\s+|\Z)"
    match = re.search(pattern, contents, re.DOTALL | re.MULTILINE)
    return match.group(1) if match else None
