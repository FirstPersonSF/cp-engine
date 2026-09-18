r"""Append one dated entry to the tenant's `improvements.md` (#282, #293).

WHY A MODULE AND NOT A ONE-LINER. The file's own header states the format —
`- <date> · \`area\` — <observation>` — and the protocol that gives it value:
**log at the moment of friction**, never delete, and let `sweep improvements`
cluster entries into issues later. A caller that guesses the shape produces a
line the sweep cannot parse, and the sweep is the whole point of writing it
down.

WHY IT EXISTS AT ALL. `improvements.md` is a tenant FILE, so a session working
only through `cp-hosted` could read it and had no way to add to it. That is the
worst arrangement for this particular file: the sessions most likely to hit
friction with the hosted surface were the ones that could not record it, so the
log under-reports exactly where it should report most.

THE FILE IS SECTIONED (#293). The tenant log has a `## Open` section and a
`## Resolved` section, and `sweep improvements` reads section membership. An
append at EOF lands under **Resolved** — the first webhook commit did exactly
that — so a new entry goes at the END of `## Open`, immediately before the
next heading. A file with no `## Open` heading falls back to EOF, and the
result says which happened.

APPEND-ONLY, ALWAYS. Entries are never rewritten or removed here — the harvest
marks them in place (`[→ cp-engine #N]`, `[fixed: <date>]`, `[dropped: …]`)
and the marker IS the archive. This module INSERTS one line; it never edits or
removes an existing one, including blank ones. A verb that could edit an
existing entry would be a verb that could quietly rewrite the record of a
decision.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

# `- 2026-09-17 · `area` — observation`. The separators are the file's, not
# ours: `·` between date and area, `—` before the prose. Used to PARSE the
# area/date of a well-formed entry; dedupe deliberately does not depend on it
# (see `_entry_content`), because a multi-backtick area such as
# `` `mcp`/`engine` `` is a real entry the harvest reads and must still dedupe.
_ENTRY_RE = re.compile(r"^-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+·\s+`(?P<area>[^`]+)`\s+—")

# The dated prefix alone — what is stripped before comparing content, so that
# a retry across midnight and an entry with an odd area both compare on what
# was observed rather than when or how it was tagged.
_DATE_PREFIX_RE = re.compile(r"^-\s+\d{4}-\d{2}-\d{2}\s+·\s+")

_OPEN_HEADING_RE = re.compile(r"^##\s+Open\b")
_HEADING_RE = re.compile(r"^##\s")

Placement = Literal["open", "eof"]


class ImprovementsError(ValueError):
    """A caller error worth surfacing rather than absorbing."""


class AppendResult(tuple):
    """`(text, changed)` — unpacks as the pair every caller expects — plus
    `.placement`: `"open"` when the entry went under `## Open`, `"eof"` when
    the file had no such heading and the entry fell back to the end.

    A plain 2-tuple could not say which, and a 3-tuple would break every
    `updated, changed = append_entry(...)` already written.
    """

    placement: Placement

    def __new__(cls, text: str, changed: bool, placement: Placement) -> "AppendResult":
        self = super().__new__(cls, (text, changed))
        self.placement = placement
        return self

    @property
    def text(self) -> str:
        return self[0]

    @property
    def changed(self) -> bool:
        return self[1]


def _normalise(s: str) -> str:
    """Collapse internal whitespace and strip — applied to BOTH sides of a
    dedupe comparison, so an existing entry that was wrapped by hand or
    re-flowed by an editor still matches the same observation."""
    return " ".join((s or "").split())


def format_entry(area: str, observation: str, *, today: date) -> str:
    """One entry in the file's documented shape.

    `area` is a short tag naming the surface that fought you (`spine_lint`,
    `carry-forward`, `hosted-mcp`) — it is what `sweep improvements` clusters
    on, so a vague one costs the harvest rather than this call.
    """
    area = (area or "").strip().strip("`")
    observation = _normalise(observation)
    if not area:
        raise ImprovementsError("area is required — the harvest clusters on it")
    if "`" in area:
        raise ImprovementsError("area must not contain a backtick")
    if len(observation) < 20:
        raise ImprovementsError(
            "observation must be real prose — a one-word entry is noise the "
            "harvest cannot act on"
        )
    return f"- {today.isoformat()} · `{area}` — {observation}"


def _entry_blocks(lines: list[str]) -> list[str]:
    """Every entry in the file as ONE string each, continuation lines joined.

    Tenant entries are frequently multi-line: the bullet line, then indented
    paragraphs (blank lines between them). Comparing only the bullet line would
    let a long observation be re-logged whenever its first line differed from
    the single-line form a caller submits. An entry runs from its `- ` line
    until the next `- ` line, heading, rule, or non-indented prose.
    """
    blocks: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("- "):
            if current is not None:
                blocks.append(" ".join(current))
            current = [line]
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped or line[:1].isspace():
            current.append(line)
            continue
        # Non-indented, non-blank, not a bullet: the entry ended.
        blocks.append(" ".join(current))
        current = None
    if current is not None:
        blocks.append(" ".join(current))
    return blocks


_TAG_SPAN_RE = re.compile(r"`([^`]+)`")


def _entry_content(block: str) -> tuple[frozenset[str], str]:
    """The comparable parts of an entry: `(areas, body)`, whitespace
    normalised, date prefix off.

    `areas` is every backtick span in the tag position — one for a normal
    entry, several for a hand-written `` `mcp`/`engine` `` — so a
    multi-span tag is compared rather than skipped. A tag with no span at all
    (an entry not in the documented shape) is still compared, as its bare
    text, rather than silently ignored.
    """
    without_date = _DATE_PREFIX_RE.sub("", block, count=1)
    if without_date == block:
        without_date = block[2:] if block.startswith("- ") else block
    tag, sep, body = without_date.partition(" — ")
    if not sep:
        return frozenset(), _normalise(without_date)
    spans = _TAG_SPAN_RE.findall(tag)
    areas = frozenset(_normalise(a) for a in spans) or frozenset({_normalise(tag)})
    return areas, _normalise(body)


def _is_duplicate(existing: tuple[frozenset[str], str], new: tuple[frozenset[str], str]) -> bool:
    """Same observation under the same tag — OR the same observation with a
    harvest marker appended, OR under a multi-span tag that includes ours.

    The harvest marks an entry in place (`[→ cp-engine #N]`, `[fixed: …]`,
    `[dropped: …]`). That marker does not make the observation a different
    observation — re-logging it after harvest is the duplicate case the
    dedupe exists for.
    """
    existing_areas, existing_body = existing
    new_areas, new_body = new
    if not new_areas & existing_areas:
        return False
    return existing_body == new_body or existing_body.startswith(f"{new_body} [")


def append_entry(text: str, area: str, observation: str, *, today: date) -> AppendResult:
    """Insert one entry at the END of `## Open`. Returns (new text, changed).

    NEWEST LAST within the section, unlike the Exec Summary's `Updates`. This
    file is read as a chronological record and harvested in clusters, and
    every existing entry is in that order — inserting at the top would split
    the section into two orderings, which is worse than either.

    PLACEMENT (#293). The entry lands immediately after the last non-blank
    line of the `## Open` section, before the next `## ` heading, with one
    blank line kept before that heading. No `## Open` heading → EOF, and the
    result's `.placement` is `"eof"` so a caller can tell.

    A duplicate (same area, same observation, any date, either side's
    whitespace, harvest-marked or not) is a NO-OP, so a retry after a timeout
    cannot double-log. Matched on CONTENT rather than the whole line: a retry
    that crosses midnight is the same observation, not a new one.
    """
    entry = format_entry(area, observation, today=today)
    new_content = _entry_content(entry)

    lines = text.split("\n")
    for block in _entry_blocks(lines):
        if _is_duplicate(_entry_content(block), new_content):
            return AppendResult(text, False, _placement_of(lines))

    open_idx = _find_open_heading(lines)
    if open_idx is None:
        trimmed = text.rstrip("\n")
        return AppendResult(f"{trimmed}\n{entry}\n", True, "eof")

    next_idx = next(
        (i for i in range(open_idx + 1, len(lines)) if _HEADING_RE.match(lines[i])),
        None,
    )
    region_end = next_idx if next_idx is not None else len(lines)

    # Insert after the last non-blank line of the section. Blank lines already
    # there are kept — nothing is removed, only added.
    last_content = next(
        (i for i in range(region_end - 1, open_idx, -1) if lines[i].strip()),
        None,
    )
    if last_content is None:
        # Empty section: heading, blank line, entry.
        insert_at = open_idx + 1
        new_lines = ["", entry]
    else:
        insert_at = last_content + 1
        new_lines = [entry]

    # One blank line must separate the entry from the following heading. If
    # the section had none (entry directly against the heading), add one.
    if next_idx is not None and insert_at == next_idx:
        new_lines.append("")

    out = lines[:insert_at] + new_lines + lines[insert_at:]
    result = "\n".join(out)
    if not result.endswith("\n"):
        result += "\n"
    return AppendResult(result, True, "open")


def _find_open_heading(lines: list[str]) -> int | None:
    return next((i for i, l in enumerate(lines) if _OPEN_HEADING_RE.match(l)), None)


def _placement_of(lines: list[str]) -> Placement:
    return "open" if _find_open_heading(lines) is not None else "eof"
