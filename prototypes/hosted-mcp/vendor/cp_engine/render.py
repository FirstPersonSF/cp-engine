"""VENDORED from `cp_engine.render` — the exec-summary region helpers only.

`exec_summary_lint` imports these two at module level, and the real `render`
needs jinja2 plus the template tree. Both are pure functions on the cp.md body
string; the marker constants travel with them because a second spelling of
`<!-- cp-engine:start exec-summary -->` would silently slice nothing.
"""

from __future__ import annotations

import re


EXEC_SUMMARY_REGION = "exec-summary"
EXEC_SUMMARY_START = f"<!-- cp-engine:start {EXEC_SUMMARY_REGION} -->"
EXEC_SUMMARY_END = f"<!-- cp-engine:end {EXEC_SUMMARY_REGION} -->"

EXEC_SUMMARY_MIGRATION_SUFFIX = " — migrated from Quick Resume"

# The migration stamps `- <date> — migrated from Quick Resume` under
# Updates. Built from the suffix (via re.escape) so producer + readers
# can't drift.
EXEC_SUMMARY_MIGRATION_BULLET_RE = re.compile(
    r"^- \d{4}-\d{2}-\d{2}" + re.escape(EXEC_SUMMARY_MIGRATION_SUFFIX) + r"\s*$"
)


def slice_exec_summary_region(cp_md_body: str) -> str | None:
    """Return the inner text between the exec-summary markers (markers
    excluded, leading/trailing blank lines trimmed), or None if either
    marker is absent. Pure function on the body string."""
    start = cp_md_body.find(EXEC_SUMMARY_START)
    if start == -1:
        return None
    end = cp_md_body.find(EXEC_SUMMARY_END, start)
    if end == -1:
        return None
    return cp_md_body[start + len(EXEC_SUMMARY_START):end].strip("\n")


def exec_summary_is_authored(region: str) -> bool:
    """True if the exec-summary region carries real human content — i.e. any
    `**Label:**` field has a non-placeholder value OR any bullet is real.

    A `_<...>_` placeholder value or bullet does NOT count, and the
    auto-stamped migration bullet (`- <date> — migrated from Quick Resume`)
    does NOT count. So a fresh scaffold (all placeholders) and a freshly-
    migrated-but-unauthored region both read as unauthored.

    This is the single authoritative copy; agenda + prep_planning both call
    it so their authored-checks can't silently diverge.
    """
    for raw in region.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Field line: `**Label:** value` — real iff value is a non-placeholder.
        field = re.match(r"^\*\*[^*]+:\*\*\s*(?P<value>.*)$", line)
        if field is not None:
            value = field.group("value").strip()
            if value and "_<" not in value:
                return True
            continue
        # Bullet line — real unless it's a placeholder seed or the migration stamp.
        if line.startswith("- "):
            if "_<" in line:  # placeholder seed bullet — not authored
                continue
            if EXEC_SUMMARY_MIGRATION_BULLET_RE.match(line):
                continue
            return True
        # Anything else (headings, stray prose) is not authored on its own.
    return False
