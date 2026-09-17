"""Warn-only lint for sprint-file bullets the parser silently refused.

THE FAILURE THIS EXISTS TO CATCH (#272). `sprints._parse_decisions` required
the marker to be a bare `[decision`, so a bullet whose marker was wrapped in a
code span — `` - `[decision · 2026-09-15]` … `` — failed the check and was
skipped. Both forms are in the tree: `cp add-decision` writes the bare one,
while hand-written and model-written bullets routinely code-span it because
that is how the marker reads in the file.

**It was invisible from every angle a person looks from.** The bullet renders
correctly in Markdown, so a human reviewing the sprint file sees the decision
and reasonably assumes the system does too. Nothing downstream says otherwise:
`latest_recorded_signal`, the ⚠️ stale-summary "Since then" line and #260's
Last-activity column all read a truncated record with no indication that it is
truncated. 17 bullets across 4 files were lost this way, and `ibx-5153`'s W39
file parsed to ZERO decisions while visibly containing two. It surfaced only
because an unrelated feature happened to read the same parser.

**So the lint is not about backticks.** That specific bug is fixed. This
catches the NEXT variant — a new marker kind, a format drift, a parser change
— at the moment it appears rather than weeks later, by asserting a property
that should always hold: *a line that looks like a dated bullet should produce
a parsed entry.* When the corpus is clean this is silent and costs nothing.

DELIBERATELY CONSERVATIVE. Only lines that are unambiguously a bullet attempt
are reported: the marker must sit at the START of the line (after `- ` and at
most a couple of wrapper characters), so prose that merely mentions
`[decision · 2026-09-14]` mid-sentence is not flagged. A false positive here
would train the reader to ignore the warning, which is exactly how the
original bug survived.

PER FILE, NOT PER LINE. One line per file with a count and the first offender
quoted. A format change can break every bullet in the tenant at once, and 200
warning lines is a wall nobody reads; four lines with an example is a bug
report. The quoted line is what makes a false positive obviously dismissible
rather than mysterious.

WARN ONLY. Callers echo and always exit 0 — consistent with `word_count_lint`,
`spine_lint` and `exec_summary_lint`, none of which block.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# A line that is trying to be a dated bullet: `- `, then at most two wrapper
# characters (a backtick, a bold marker), then `[<kind> · <date>`.
#
# The wrapper allowance is what makes this a LINT rather than a duplicate of
# the parser: it matches the shapes a person plausibly writes, including ones
# no parser accepts yet, which is the point.
_LOOKS_LIKE_BULLET = re.compile(
    r"^\s*-\s*.{0,2}?\[(?P<kind>decision|open|owed|sent|risk|ask)\b[^\]]*·",
    re.IGNORECASE,
)

# Bullet kinds that carry a date and are parsed into typed entries. A line
# whose marker is none of these is somebody's freeform bullet, not a failure.
_PARSED_KINDS = frozenset({"decision", "open", "owed", "sent", "risk", "ask"})


def unparsed_bullet_warnings(root: Path) -> list[str]:
    """One warning per sprint file containing dated bullets nothing parsed.

    Walks `root/sprints/**/<project>.md`, counts the lines that LOOK like a
    dated bullet, counts what `parse_sprint_file` actually returned, and
    reports the shortfall.

    Files starting with `_` are skipped: `_planning.md` and `_ingest-log/`
    entries are not project sprint files and carry bullets in other shapes.
    """
    from cp_engine.sprints import parse_sprint_file

    sprints = root / "sprints"
    if not sprints.is_dir():
        return []

    out: list[str] = []
    for week_dir in sorted(sprints.iterdir()):
        if not week_dir.is_dir():
            continue
        for path in sorted(week_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue

            looks_like = [
                line
                for line in text.splitlines()
                if _LOOKS_LIKE_BULLET.match(line)
            ]
            if not looks_like:
                continue

            try:
                parsed = parse_sprint_file(path)
            except Exception:  # noqa: BLE001 — a malformed file is its own
                # problem and `sync` already reports it; do not double-warn.
                logger.debug("Skipping unparseable sprint file %s", path)
                continue

            found = _parsed_entry_count(parsed)
            missing = len(looks_like) - found
            if missing <= 0:
                continue

            rel = path.relative_to(root)
            example = looks_like[0].strip()
            if len(example) > 90:
                example = example[:89].rstrip() + "…"
            out.append(
                f"{rel}: ⚠ {missing} dated bullet(s) present in the text but "
                f"not parsed — every consumer of this file reads a truncated "
                f"record. First: {example}"
            )
    return out


def _parsed_entry_count(parsed: object) -> int:
    """How many dated entries the parser actually produced for one file.

    Counts the same sections `summary.latest_dated_activity` reads, and counts
    only entries carrying a date — an undated entry is not evidence that a
    dated line was understood.
    """
    from cp_engine.summary import _DATED_BULLET_FIELDS

    total = 0
    for field in _DATED_BULLET_FIELDS:
        total += _count_dated(getattr(parsed, field, None))

    # `carry-forward` is an engine-managed region with its own parser and its
    # own container (CarryForward, not a list), and its bullets use the
    # `[ask · …]` marker rather than `[open · …]`. Counting only the list
    # fields above reported every carried-over ask as unparsed — 21 in W39
    # alone, all of them correctly read by `_parse_carry_forward`.
    carry = getattr(parsed, "carry_forward", None)
    if carry is not None:
        for field in ("asks", "risks", "horizon"):
            total += _count_dated(getattr(carry, field, None))
    return total


# The entry types do not agree on what to call the date: DecisionEntry and
# InboundUpdate use `date`, ClientAsk uses `asked_date`, Risk uses
# `raised_date`. Chasing the names one at a time produced three rounds of
# false positives here, so this asks the shape of the VALUE instead — any
# field ending in `date` holding an ISO-looking string counts. A fourth entry
# type with a fourth spelling is then free.
_DATE_FIELD_SUFFIX = "date"
# An ISO day, OR a sprint-week reference. Horizon items are dated `by W39` by
# design — the sprint file's own vocabulary — and an ISO-only test reported
# every one of them as unparsed (#280). A date field is "filled" if it holds
# anything non-empty; the lint is asking whether the bullet PARSED, not whether
# someone used a format it likes.
_DATED = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|by\s+W\d{1,2}|W\d{1,2})$", re.IGNORECASE)


def _count_dated(value: object) -> int:
    """Dated entries in one list-shaped section, or 0 for anything else."""
    if not isinstance(value, (list, tuple)):
        return 0
    return sum(1 for entry in value if _has_date(entry))


def _has_date(entry: object) -> bool:
    """Did this entry parse into something with a recognisable date?

    Deliberately tolerant. An entry whose only date field is empty may still be
    a correctly parsed bullet — a horizon `opportunity` carries no date by
    design — so the fallback asks whether the entry has TEXT, which every
    parser fills when it understood the line. Counting it as unparsed reported
    healthy rows as data loss (#280).
    """
    has_date_field = False
    for name in dir(entry):
        if name.startswith("_") or not name.endswith(_DATE_FIELD_SUFFIX):
            continue
        has_date_field = True
        raw = getattr(entry, name, None)
        if isinstance(raw, str) and _DATED.match(raw.strip()):
            return True
    # A parsed entry with text is a parsed entry, dated or not.
    text = getattr(entry, "text", None)
    return bool(has_date_field and isinstance(text, str) and text.strip())
