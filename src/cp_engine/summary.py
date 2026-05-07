"""One-line summary regeneration for the master-CP.

Per spec v02 §7.3, summaries are regenerated only during the deepening
pass — accepting some staleness mid-week in exchange for not making
every `update <code>` session touch two files.

Hard cap (spec §3.2 / Q5): ≤120 characters, single sentence, no markdown.
The renderer enforces the cap; over-length truncates with `…` and logs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_SUMMARY_LEN = 120


def enforce_summary_cap(summary: str) -> str:
    """Trim `summary` to ≤120 chars, single line, no markdown.

    Strips newlines and common markdown markers. Truncates with `…` if
    over-length and logs at WARNING.
    """
    cleaned = " ".join(summary.split())  # collapse whitespace + newlines
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    if len(cleaned) > MAX_SUMMARY_LEN:
        logger.warning("Summary truncated: %d chars → %d", len(cleaned), MAX_SUMMARY_LEN)
        cleaned = cleaned[: MAX_SUMMARY_LEN - 1].rstrip() + "…"
    return cleaned


def regenerate_from_quick_resume(quick_resume: str) -> str:
    """Produce a one-line summary from a project CP's Quick Resume section.

    The pure-text helper — actually invoking the LLM happens in the
    deepening-pass session. This module owns the post-processing
    (cap enforcement, formatting normalization).

    For v0.1, accepts a Quick Resume text and currently just enforces
    the cap on its first sentence. v0.2+ may invoke an LLM call.
    """
    first_sentence = quick_resume.split(".")[0]
    return enforce_summary_cap(first_sentence)
