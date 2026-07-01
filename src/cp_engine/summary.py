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
    over-length and logs at WARNING.
    """
    cleaned = " ".join(summary.split())
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    if len(cleaned) > MAX_SUMMARY_LEN:
        logger.warning("Summary truncated: %d chars → %d", len(cleaned), MAX_SUMMARY_LEN)
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
