"""Warn-only Exec Summary per-field word-budget lint.

The Exec Summary is the durable project-state surface, and its fields have
designed densities: Status is "current state in a phrase", Where it stands is
a handful of current-reality bullets. In practice fields accrete (the classic
failure is a Status that swells into a multi-hundred-word session log), and
nothing scans for it — the word-count discipline checks only look at the whole
file. This lint budgets the fields individually:

    Status          ≤ 100 words
    Where it stands ≤ 5 bullets, ≤ 40 words per bullet
    Next up         ≤ 6 bullets
    Blockers        ≤ 5 bullets

Pure functions: cp.md text in, display-ready warning strings out. WARN ONLY —
callers echo the lines and always exit 0; the lint never blocks a wrap-up and
never edits. Surfaced standalone by `cp exec-lint`, folded into
`cp spine-lint`'s cp.md pass, and echoed after `cp render` (the word-count
discipline path). An absent or unauthored exec-summary region is silent —
scaffolds aren't over budget.
"""

from __future__ import annotations

import re

from cp_engine.render import (
    exec_summary_is_authored,
    slice_exec_summary_region,
)

# Field budgets. Word-budgeted fields carry a max word count; bullet-budgeted
# fields a max bullet count (and optionally a per-bullet word cap).
STATUS_MAX_WORDS = 100
WHERE_MAX_BULLETS = 5
WHERE_MAX_WORDS_PER_BULLET = 40
NEXT_UP_MAX_BULLETS = 6
BLOCKERS_MAX_BULLETS = 5

# Every field label the exec-summary region carries (see the scaffold in
# sync's `_build_exec_summary_region`); parsing needs the full set so a
# field's body ends at the NEXT label, whichever it is.
_FIELD_LABELS = (
    "Last session",
    "Objective",
    "Status",
    "Where it stands",
    "Next up",
    "Blockers",
    "Updates",
)

_FIELD_RE = re.compile(
    r"^\*\*(?P<label>" + "|".join(re.escape(l) for l in _FIELD_LABELS)
    + r"):\*\*[ \t]*(?P<inline>.*)$",
    re.MULTILINE,
)

# Scaffold placeholder value (`_<...>_`) — an unauthored field, not a finding.
_PLACEHOLDER_RE = re.compile(r"_<[^>]+>_")


def _split_fields(region: str) -> dict[str, str]:
    """Map field label → body text (the inline remainder plus every line up
    to the next field label). Later duplicates win (shouldn't happen)."""
    fields: dict[str, str] = {}
    matches = list(_FIELD_RE.finditer(region))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(region)
        body = (m.group("inline") + "\n" + region[m.end():end]).strip()
        fields[m.group("label")] = body
    return fields


def _word_count(text: str) -> int:
    """Words in `text`, markdown emphasis/backticks stripped so `**bold**`
    styling doesn't change the count."""
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    return len(cleaned.split())


def _bullets(body: str) -> list[str]:
    """Top-level `- ` bullets in a field body, placeholder seeds excluded."""
    out = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.startswith("- "):
            continue  # top-level only — indented sub-bullets ride along
        value = line[2:].strip()
        if value and not _PLACEHOLDER_RE.search(value):
            out.append(value)
    return out


def lint_exec_summary(cp_md_text: str) -> list[str]:
    """Per-field word-budget check over a project cp.md's exec-summary region.

    Returns one display-ready warning per over-budget finding; [] when the
    region is absent, unauthored, or compliant.
    """
    region = slice_exec_summary_region(cp_md_text or "")
    if region is None or not exec_summary_is_authored(region):
        return []
    fields = _split_fields(region)
    out: list[str] = []

    # Status — the designed one-phrase field.
    status = fields.get("Status", "")
    if status and not _PLACEHOLDER_RE.search(status):
        n = _word_count(status)
        if n > STATUS_MAX_WORDS:
            out.append(
                f"⚠ exec-summary Status: {n} words (budget "
                f"{STATUS_MAX_WORDS}) — distill to a phrase; the session "
                "detail belongs in Updates and the sprint file")

    # Where it stands — bullet count + per-bullet density.
    where = _bullets(fields.get("Where it stands", ""))
    if len(where) > WHERE_MAX_BULLETS:
        out.append(
            f"⚠ exec-summary Where it stands: {len(where)} bullets (budget "
            f"{WHERE_MAX_BULLETS}) — merge or roll resolved threads off")
    over = [b for b in where if _word_count(b) > WHERE_MAX_WORDS_PER_BULLET]
    if over:
        worst = max(_word_count(b) for b in over)
        out.append(
            f"⚠ exec-summary Where it stands: {len(over)} bullet(s) over "
            f"{WHERE_MAX_WORDS_PER_BULLET} words (worst {worst}) — one "
            "current-reality line per bullet, not a paragraph")

    # Next up / Blockers — bullet counts.
    for label, budget, nudge in (
        ("Next up", NEXT_UP_MAX_BULLETS,
         "keep the actionable few; the rest is sprint-file material"),
        ("Blockers", BLOCKERS_MAX_BULLETS,
         "a blocker list this long hides the real gate"),
    ):
        bullets = _bullets(fields.get(label, ""))
        if len(bullets) > budget:
            out.append(
                f"⚠ exec-summary {label}: {len(bullets)} bullets (budget "
                f"{budget}) — {nudge}")

    return out
