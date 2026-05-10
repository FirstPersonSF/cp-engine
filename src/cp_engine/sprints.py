"""Sprint-file lifecycle: parsing, scaffold rendering, derived blocks.

Sprint files live at `sprints/<YYYY-W##>/<project-code>.md` in the tenant
working tree. Engine-managed regions use the existing
`<!-- cp-engine:start <name> -->` / `<!-- cp-engine:end <name> -->` markers
and re-render every sync. Hand-written regions are preserved verbatim.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from . import render as _render
from .state import (
    CarryForward,
    ClientAsk,
    Deliverable,
    HorizonItem,
    InboundUpdate,
    Issue,
    MeetingNotes,
    Outbound,
    PersonHours,
    ProjectState,
    Risk,
    SprintCommit,
    SprintFacts,
    SprintFile,
    WhereItStands,
    dir_slug,
    scope_for,
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
    client_outbound, client_open_asks, client_inbound = _parse_client_section(body)
    allocation, deliverables, definition_of_done = _parse_this_sprint(body)
    return SprintFile(
        project_code=project_code,
        week_iso=week_iso,
        week_start=start,
        week_end=end,
        prior_sprint=prior,
        facts=_parse_facts_region(body),
        where_it_stands=_empty_where(),
        carry_forward=_parse_carry_forward(body),
        client_outbound=client_outbound,
        client_open_asks=client_open_asks,
        client_inbound=client_inbound,
        risks=_parse_risks(body),
        allocation=allocation,
        deliverables=deliverables,
        definition_of_done=definition_of_done,
        horizon=_parse_horizon(body),
        meeting_notes=_parse_meeting_notes(body),
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


_BRACKET_RE = re.compile(r"^\s*-\s*\[(?P<meta>[^\]]+)\]\s*(?P<text>.*)$")


def _parse_bracketed_bullet(line: str) -> tuple[list[str], str] | None:
    m = _BRACKET_RE.match(line)
    if not m:
        return None
    parts = [p.strip() for p in m.group("meta").split("·")]
    return parts, m.group("text").strip()


def _section_body(body: str, heading: str) -> str:
    """Slice out a `## heading` section up to next `## ` or EOF."""
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body)
    return m.group(1) if m else ""


def _subsection(section_body: str, sub: str) -> str:
    pattern = re.compile(
        rf"^### {re.escape(sub)}\s*$(.*?)(?=^### |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(section_body)
    return m.group(1) if m else ""


def _bullets(section_body: str) -> list[tuple[str, str]]:
    """Return [(first_line, indented_continuation)] pairs."""
    out: list[tuple[str, str]] = []
    cur_first: str | None = None
    cur_cont: list[str] = []
    for line in section_body.splitlines():
        if line.startswith("- ") or line.startswith("-\t"):
            if cur_first is not None:
                out.append((cur_first, "\n".join(cur_cont).strip()))
            cur_first = line
            cur_cont = []
        elif line.startswith("  ") and cur_first is not None:
            cur_cont.append(line.strip())
    if cur_first is not None:
        out.append((cur_first, "\n".join(cur_cont).strip()))
    return out


def _parse_client_section(
    body: str,
) -> tuple[tuple[Outbound, ...], tuple[ClientAsk, ...], tuple[InboundUpdate, ...]]:
    section = _section_body(body, "Client communication")
    out: list[Outbound] = []
    for first, cont in _bullets(_subsection(section, "Outbound")):
        parsed = _parse_bracketed_bullet(first)
        if parsed:
            parts, text = parsed
            status = parts[0] if parts else "draft"
            date_s = parts[1] if len(parts) > 1 else ""
            out.append(
                Outbound(text=text, status=status, date=date_s, note=cont or None)
            )
        else:
            text = first.lstrip("- ").strip()
            out.append(
                Outbound(text=text, status="draft", date="", note=cont or None)
            )
    asks: list[ClientAsk] = []
    for first, _cont in _bullets(_subsection(section, "Open asks")):
        parsed = _parse_bracketed_bullet(first)
        if parsed:
            parts, text = parsed
            status = parts[0] if parts else "open"
            asked_date = parts[1] if len(parts) > 1 else ""
            who = parts[2] if len(parts) > 2 else None
            asks.append(
                ClientAsk(text=text, asked_date=asked_date, status=status, who=who)
            )
    inbound: list[InboundUpdate] = []
    for first, _ in _bullets(_subsection(section, "Inbound")):
        parsed = _parse_bracketed_bullet(first)
        if parsed:
            parts, text = parsed
            inbound.append(
                InboundUpdate(
                    date=parts[0] if parts else "",
                    who=parts[1] if len(parts) > 1 else "",
                    text=text,
                )
            )
    return tuple(out), tuple(asks), tuple(inbound)


def _parse_risks(body: str) -> tuple[Risk, ...]:
    section = _section_body(body, "Dependencies & risks")
    out: list[Risk] = []
    for first, cont in _bullets(section):
        parsed = _parse_bracketed_bullet(first)
        if not parsed:
            continue
        parts, text = parsed
        severity = parts[0] if parts else "watching"
        category = parts[1] if len(parts) > 1 else ""
        raised = parts[2] if len(parts) > 2 else ""
        why = None
        if cont.lower().startswith("why it matters:"):
            why = cont.split(":", 1)[1].strip()
        out.append(
            Risk(
                text=text,
                severity=severity,
                category=category,
                raised_date=raised,
                why_it_matters=why,
            )
        )
    return tuple(out)


_ALLOCATION_RE = re.compile(r"\*\*Allocation:\*\*\s*(.+)")
_PERSON_HOURS_RE = re.compile(r"(?P<name>[A-Za-z][\w\s]*?)\s*·\s*(?P<hours>[\d.]+)h")


def _parse_this_sprint(
    body: str,
) -> tuple[tuple[PersonHours, ...], tuple[Deliverable, ...], str]:
    section = _section_body(body, "This sprint")
    alloc: list[PersonHours] = []
    m = _ALLOCATION_RE.search(section)
    if m:
        for pm in _PERSON_HOURS_RE.finditer(m.group(1)):
            alloc.append(
                PersonHours(
                    person_name=pm.group("name").strip(),
                    hours=float(pm.group("hours")),
                )
            )
    deliverables: list[Deliverable] = []
    deliv_section = _subsection(section, "Deliverables")
    for i, line in enumerate(
        [ln for ln in deliv_section.splitlines() if re.match(r"^\d+\.\s+", ln)],
        start=1,
    ):
        text = re.sub(r"^\d+\.\s+", "", line).strip()
        deliverables.append(Deliverable(text=text, position=i))
    dod = _subsection(section, "Definition of done").strip()
    return tuple(alloc), tuple(deliverables), dod


def compute_carry_forward(prior_path: Path) -> CarryForward:
    """Derive the carry-forward block for a new sprint from the prior file.

    Asks roll forward only when still `open`; risks only when `escalated` or
    `watching`; all horizon items roll forward (they remain unresolved by
    nature). Missing prior file → empty carry-forward.
    """
    if not prior_path.exists():
        return CarryForward(asks=(), risks=(), horizon=())
    prior = parse_sprint_file(prior_path)
    asks = tuple(a for a in prior.client_open_asks if a.status == "open")
    risks = tuple(r for r in prior.risks if r.severity in ("escalated", "watching"))
    horizon = tuple(prior.horizon)
    return CarryForward(asks=asks, risks=risks, horizon=horizon)


def _parse_carry_forward(body: str) -> CarryForward:
    try:
        region = _extract_region(body, "carry-forward")
    except ValueError:
        return CarryForward(asks=(), risks=(), horizon=())
    asks: list[ClientAsk] = []
    risks: list[Risk] = []
    horizon: list[HorizonItem] = []
    for first, _ in _bullets(region):
        parsed = _parse_bracketed_bullet(first)
        if not parsed:
            continue
        parts, text = parsed
        kind = parts[0] if parts else ""
        if kind == "ask":
            asks.append(
                ClientAsk(
                    text=text,
                    asked_date=parts[1] if len(parts) > 1 else "",
                    status="open",
                    who=parts[2] if len(parts) > 2 else None,
                )
            )
        elif kind == "risk":
            risks.append(
                Risk(
                    text=text,
                    severity=parts[1] if len(parts) > 1 else "watching",
                    category=parts[2] if len(parts) > 2 else "",
                    raised_date=parts[3] if len(parts) > 3 else "",
                )
            )
        elif kind in ("milestone", "decision", "opportunity"):
            horizon.append(
                HorizonItem(
                    text=text,
                    bucket=kind,
                    target_date=parts[1] if len(parts) > 1 else None,
                )
            )
    return CarryForward(asks=tuple(asks), risks=tuple(risks), horizon=tuple(horizon))


# Meeting-meta line is `_From <source> · <attendees> · <duration>_` where
# <source> may itself contain `·` separators (e.g. "sprint planning · May 11").
# Anchor attendees/duration to the LAST two `·`-delimited chunks by making the
# source group non-greedy; this absorbs any extra middle chunks (like a date)
# into source rather than mis-binding them to attendees.
_MEETING_META_RE = re.compile(
    r"_(?:From\s+)?(?P<source>.+?)\s*·\s*(?P<attendees>[^·]+?)\s*·\s*(?P<duration>[^·_]+?)_"
)


def _parse_meeting_notes(body: str) -> MeetingNotes | None:
    section = _section_body(body, "Meeting notes & decisions")
    if not section.strip():
        return None
    src = att = dur = None
    m = _MEETING_META_RE.search(section)
    if m:
        src = m.group("source").strip()
        att = m.group("attendees").strip()
        dur = m.group("duration").strip()
    decisions = tuple(
        re.sub(r"^\d+\.\s+", "", ln).strip()
        for ln in _subsection(section, "Decisions").splitlines()
        if re.match(r"^\d+\.\s+", ln)
    )
    discussion = _subsection(section, "Discussion notes").strip()
    return MeetingNotes(
        source=src,
        attendees=att,
        duration=dur,
        decisions=decisions,
        discussion_prose=discussion,
    )


def _parse_horizon(body: str) -> tuple[HorizonItem, ...]:
    section = _section_body(body, "Horizon")
    out: list[HorizonItem] = []
    for bucket, sub in (
        ("milestone", "Milestones"),
        ("decision", "Decisions due"),
        ("opportunity", "Opportunities"),
    ):
        for first, cont in _bullets(_subsection(section, sub)):
            parsed = _parse_bracketed_bullet(first)
            if parsed:
                parts, text = parsed
                target = parts[0] if parts else None
                out.append(
                    HorizonItem(
                        text=text,
                        bucket=bucket,
                        target_date=target,
                        note=cont or None,
                    )
                )
            else:
                text = first.lstrip("- ").strip()
                out.append(
                    HorizonItem(
                        text=text,
                        bucket=bucket,
                        target_date=None,
                        note=cont or None,
                    )
                )
    return tuple(out)


# ──────────────────────────────────────────────────────────────────────
#  Scaffold renderer
# ──────────────────────────────────────────────────────────────────────


def _short_md_date(iso: str) -> str:
    """Short month-day with no year ('May 11'). Empty input → empty string."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d") if iso else ""


def _long_md_date(iso: str) -> str:
    """Month-day-year ('May 17, 2026'). Empty input → empty string."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y") if iso else ""


def render_sprint_scaffold(
    *,
    project: ProjectState,
    week_iso: str,
    week_label: str,
    week_start: str,
    week_end: str,
    prior_sprint: str | None,
    last_sprint_hours_line: str | None,
    sessions_this_week: int,
    last_session_date: str | None,
    last_session_who: str | None,
    last_session_summary: str | None,
    recent_commits: tuple[SprintCommit, ...],
    open_issues: tuple[Issue, ...],
    carry_forward: CarryForward,
) -> str:
    """Render a sprint file scaffold from the Jinja template.

    The output round-trips through `parse_sprint_file` — the H1 date format
    ("Mon D – Mon D, YYYY") and the carry-forward bracket prefixes are
    contract surfaces between this renderer and the parser. Tests in
    `test_sprints.py` assert that contract end-to-end.
    """
    env = _render._env()
    template = env.get_template("sprint-cp.md.j2")
    week_dates = f"{_short_md_date(week_start)} – {_long_md_date(week_end)}"
    return template.render(
        project={
            "code": project.code,
            "name": project.name,
            "scope": scope_for(project.company_kind),
            "dir_slug": dir_slug(project.code, project.name),
            "deal_stage": project.deal_stage,
            "owner": project.owner,
            "budget_short": _render._format_budget(project.budget),
            "last_touched_short": _render._short(project.last_touched),
            "contacts": getattr(project, "contacts", ()) or (),
        },
        engine_version=_render.ENGINE_VERSION,
        today=date.today().isoformat(),
        week_iso=week_iso,
        week_label=week_label,
        week_dates=week_dates,
        prior_sprint=prior_sprint,
        last_sprint_hours_line=last_sprint_hours_line,
        sessions_this_week=sessions_this_week,
        open_issues_count=len(open_issues),
        last_session={
            "date": last_session_date,
            "who": last_session_who,
            "summary": last_session_summary,
        },
        recent_commits=recent_commits,
        open_issues=open_issues,
        carry_forward=carry_forward,
    )
