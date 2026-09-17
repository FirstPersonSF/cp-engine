r"""Append one dated entry to the tenant's `improvements.md` (#282).

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

APPEND-ONLY, ALWAYS. Entries are never rewritten or removed here — the harvest
marks them in place (`[→ cp-engine #N]`, `[fixed: <date>]`, `[dropped: …]`) and
the marker IS the archive. A verb that could edit an existing entry would be a
verb that could quietly rewrite the record of a decision.
"""

from __future__ import annotations

import re
from datetime import date

# `- 2026-09-17 · `area` — observation`. The separators are the file's, not
# ours: `·` between date and area, `—` before the prose.
_ENTRY_RE = re.compile(r"^-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+·\s+`(?P<area>[^`]+)`\s+—")


class ImprovementsError(ValueError):
    """A caller error worth surfacing rather than absorbing."""


def format_entry(area: str, observation: str, *, today: date) -> str:
    """One entry in the file's documented shape.

    `area` is a short tag naming the surface that fought you (`spine_lint`,
    `carry-forward`, `hosted-mcp`) — it is what `sweep improvements` clusters
    on, so a vague one costs the harvest rather than this call.
    """
    area = (area or "").strip().strip("`")
    observation = " ".join((observation or "").split())
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


def append_entry(
    text: str, area: str, observation: str, *, today: date
) -> tuple[str, bool]:
    """Add one entry to the END of the log. Returns (new text, changed).

    NEWEST LAST, unlike the Exec Summary's `Updates`. This file is read as a
    chronological record and harvested in clusters, and every existing entry
    is in that order — appending at the top would split the file into two
    orderings, which is worse than either.

    A duplicate (same area, same observation, any date) is a NO-OP, so a retry
    after a timeout cannot double-log. Matched on CONTENT rather than the whole
    line: a retry that crosses midnight is the same observation, not a new one.
    """
    entry = format_entry(area, observation, today=today)
    body = entry.split(" — ", 1)[1]

    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        m = _ENTRY_RE.match(line)
        if m is None:
            continue
        existing_body = line.split(" — ", 1)[1] if " — " in line else ""
        if m.group("area") == area.strip().strip("`") and existing_body == body:
            return text, False

    trimmed = text.rstrip("\n")
    return f"{trimmed}\n{entry}\n", True
