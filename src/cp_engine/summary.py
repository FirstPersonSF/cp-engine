"""One-line summary derivation for the master-CP.

Strategy (v0.2.3 onwards): the master-CP one-liner is derived from the
project CP's hand-written content during sync. Specifically:

1. Look at the `## Quick Resume` section's "Current work:" line. If a
   human has written something there, use it.
2. Otherwise, look at the `## Current Work` section's first non-placeholder
   paragraph.
3. Otherwise, return None — the master-CP shows an empty cell. The column
   activates the moment a human writes content; until then, it's inert.

Hard cap: ≤120 characters, single sentence, no markdown. Truncates with `…`.

Pure-Python heuristic; no LLM call. Deterministic, free, fast. If the
heuristic proves too crude, swap in an LLM call in v0.3 without changing
the public function shape.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

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
    1. `## Quick Resume`'s "Current work:" line content
    2. First non-placeholder paragraph in `## Current Work`

    Returns None if neither yields content. The master CP renders an
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
        _extract_quick_resume_current_work(contents)
        or _extract_current_work_first_paragraph(contents)
    )
    if summary is None:
        return None
    return enforce_summary_cap(summary)


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


def _extract_quick_resume_current_work(contents: str) -> str | None:
    """Pull the "Current work:" line out of the `## Quick Resume` section.

    Quick Resume's body is structured as:
        **Last session:** <date>
        **Current work:** <one-line>
        **Next up:** <list>
        **Blockers:** <or "None">

    We want just the current-work value. Returns None if section absent,
    line absent, or value is the template placeholder.
    """
    section = _section_body(contents, "Quick Resume")
    if section is None:
        return None

    match = re.search(
        r"\*\*Current work:\*\*\s*(.+?)(?:\n\*\*|\n\n|\Z)",
        section,
        re.DOTALL,
    )
    if not match:
        return None

    value = match.group(1).strip()
    if not value or _PLACEHOLDER_PATTERN.search(value):
        return None
    return value


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
