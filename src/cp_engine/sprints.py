"""Sprint-file lifecycle: parsing, scaffold rendering, derived blocks.

Sprint files live at `sprints/<YYYY-W##>/<project-code>.md` in the tenant
working tree. Engine-managed regions use the existing
`<!-- cp-engine:start <name> -->` / `<!-- cp-engine:end <name> -->` markers
and re-render every sync. Hand-written regions are preserved verbatim.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .state import (
    CarryForward,
    SprintFacts,
    SprintFile,
    WhereItStands,
)
from .sync import _extract_region

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_HEADING_RE = re.compile(
    r"^# (?P<code>\S+) — (?P<name>.+?) · Sprint (?P<week>W\d+) "
    r"\((?P<start>[A-Za-z]+ \d+) – (?P<end>[A-Za-z]+ \d+), (?P<year>\d{4})\)",
    re.MULTILINE,
)


def parse_sprint_file(path: Path) -> SprintFile:
    body = path.read_text(encoding="utf-8")
    fm = _parse_frontmatter(body)
    project_code = fm["Project"].split(" — ", 1)[0].strip()
    week_iso = fm["Sprint"]
    prior = fm.get("PriorSprint")
    start, end = _parse_heading_dates(body)
    return SprintFile(
        project_code=project_code,
        week_iso=week_iso,
        week_start=start,
        week_end=end,
        prior_sprint=prior,
        facts=_parse_facts_region(body),
        where_it_stands=_empty_where(),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        client_outbound=(),
        client_open_asks=(),
        client_inbound=(),
        risks=(),
        allocation=(),
        deliverables=(),
        definition_of_done="",
        horizon=(),
        meeting_notes=None,
    )


def _parse_frontmatter(body: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(body)
    if not m:
        raise ValueError("sprint file missing frontmatter block")
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _parse_heading_dates(body: str) -> tuple[str, str]:
    m = _HEADING_RE.search(body)
    if not m:
        raise ValueError("sprint file missing H1 with week date range")
    year = m.group("year")
    start = datetime.strptime(f"{m.group('start')} {year}", "%b %d %Y").date()
    end = datetime.strptime(f"{m.group('end')} {year}", "%b %d %Y").date()
    return start.isoformat(), end.isoformat()


def _empty_facts() -> SprintFacts:
    return SprintFacts(None, None, None, None, None, 0, 0)


def _parse_facts_region(body: str) -> SprintFacts:
    try:
        region = _extract_region(body, "sprint-facts")
    except ValueError:
        return _empty_facts()
    rows: dict[str, str] = {}
    for line in region.splitlines():
        if line.startswith("|") and "|" in line[1:] and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 2 and cells[0] and cells[1]:
                rows[cells[0]] = cells[1]

    def _int(key: str) -> int:
        try:
            return int(rows.get(key, "0"))
        except ValueError:
            return 0

    return SprintFacts(
        stage=rows.get("Stage") or None,
        owner=rows.get("Owner") or None,
        budget_short=rows.get("Budget") or None,
        last_touched_short=rows.get("Last touched") or None,
        last_sprint_hours_line=rows.get("Last sprint hours") or None,
        sessions_this_week=_int("Sessions this week"),
        open_issues=_int("Open issues"),
    )


def _empty_where() -> WhereItStands:
    return WhereItStands(None, None, None, (), ())
