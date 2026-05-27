"""Sprint-file lifecycle: parsing, scaffold rendering, derived blocks.

Sprint files live at `sprints/<YYYY-W##>/<project-code>.md` in the tenant
working tree. Engine-managed regions use the existing
`<!-- cp-engine:start <name> -->` / `<!-- cp-engine:end <name> -->` markers
and re-render every sync. Hand-written regions are preserved verbatim.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import render as _render
from .render import MarkerMissing, splice_managed_region
from .state import (
    CarryForward,
    ClientAsk,
    CompanyKind,
    DecisionEntry,
    Deliverable,
    EntrySource,
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
    Stakeholder,
    Theme,
    WhereItStands,
    account_scope_for,
    dir_slug,
    scope_for,
)
from .status import is_active_status
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
        stakeholders=_parse_stakeholders(body),
        decisions=_parse_decisions(body),
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


# v0.8.5: tolerate optional backtick wrapping around the bracket. Many of the
# W19 hand-deepenings used `` `[open · ...]` `` (backtick-wrapped). The parser
# was silently dropping those — exactly the format-fragility risk the retro
# flagged. Lenient regex closes that gap. The closing backtick after the
# bracket is also optional (some authors only opened with a backtick).
_BRACKET_RE = re.compile(r"^\s*-\s*`?\[(?P<meta>[^\]]+)\]`?\s*(?P<text>.*)$")


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


def _parse_stakeholders(body: str) -> tuple[Stakeholder, ...]:
    """Parse the `### Stakeholders` subsection inside `## Client communication`.

    Bracket convention: `[name · role · context]`. Lenient — emits warnings
    via the markdown content but never fails (per v0.8.5 design: aggregators
    log on bad format, don't crash).
    """
    section = _section_body(body, "Client communication")
    out: list[Stakeholder] = []
    for first, _cont in _bullets(_subsection(section, "Stakeholders")):
        parsed = _parse_bracketed_bullet(first)
        if not parsed:
            continue
        parts, _text = parsed
        if not parts or not parts[0].strip():
            continue
        name = parts[0].strip()
        role = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        context = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        out.append(Stakeholder(name=name, role=role, context=context))
    return tuple(out)


def _parse_decisions(body: str) -> tuple[DecisionEntry, ...]:
    """Parse bracket-formatted decisions from `### Decisions` (v0.8.5 format).

    Coexists with `MeetingNotes.decisions` (which captures the legacy
    numbered-list freeform decisions). This parser handles the structured
    bullet format produced by `cp add-decision`:

        - [decision · YYYY-MM-DD][cross-cutting] text

    The `[cross-cutting]` flag is optional; absence implies cross_cutting=False.
    """
    section = _section_body(body, "Meeting notes & decisions")
    sub = _subsection(section, "Decisions")
    out: list[DecisionEntry] = []
    for first, _cont in _bullets(sub):
        # Only consider bullets matching the bracketed convention. Lines
        # without a leading `[decision` bracket are legacy freeform entries;
        # those are still captured by `_parse_meeting_notes` via the
        # numbered-list shape.
        if not first.lstrip("- ").startswith("[decision"):
            continue
        # Strip the leading `[decision · YYYY-MM-DD]` and optional
        # `[cross-cutting]` markers, then the remainder is the text.
        stripped = first.lstrip("- ").strip()
        cross = False
        if "[cross-cutting]" in stripped:
            cross = True
            stripped = stripped.replace("[cross-cutting]", "", 1).strip()
        m = re.match(r"\[decision\s*·\s*([^\]]+)\]\s*(.*)$", stripped)
        if not m:
            continue
        date_s = m.group(1).strip()
        text = m.group(2).strip()
        if not text:
            continue
        out.append(DecisionEntry(text=text, date=date_s, cross_cutting=cross))
    return tuple(out)


def parse_themes_from_week_file(week_md_path: Path) -> tuple[Theme, ...]:
    """Parse the `## Themes` section from `sprints/<W##>/_week.md`.

    Bracket convention: `[theme · YYYY-MM-DD] text`. Public helper (not
    underscore-prefixed) because aggregators in `sync.py` need to call it
    against a path that may or may not exist (returns empty tuple if not).
    """
    if not week_md_path.is_file():
        return ()
    body = week_md_path.read_text(encoding="utf-8")
    section = _section_body(body, "Themes")
    out: list[Theme] = []
    for first, _cont in _bullets(section):
        stripped = first.lstrip("- ").strip()
        if not stripped.startswith("[theme"):
            continue
        m = re.match(r"\[theme\s*·\s*([^\]]+)\]\s*(.*)$", stripped)
        if not m:
            continue
        date_s = m.group(1).strip()
        text = m.group(2).strip()
        if not text:
            continue
        out.append(Theme(text=text, date=date_s))
    return tuple(out)


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
    meetings_this_sprint: int = 0,
) -> str:
    """Render a sprint file scaffold from the Jinja template.

    The output round-trips through `parse_sprint_file` — the H1 date format
    ("Mon D – Mon D, YYYY") and the carry-forward bracket prefixes are
    contract surfaces between this renderer and the parser. Tests in
    `test_sprints.py` assert that contract end-to-end.
    """
    env = _render._env()
    # Initiative-source projects use a slimmer sprint scaffold without
    # the "Client communication" section (no client side; the
    # "Team communication" block keeps Open asks + Slack digest).
    template_name = (
        "initiative-sprint.md.j2"
        if project.source == "initiative"
        else "sprint-cp.md.j2"
    )
    template = env.get_template(template_name)
    week_dates = f"{_short_md_date(week_start)} – {_long_md_date(week_end)}"
    # account_scope_for returns "1p/<company>" for clients (the nested
    # layout), and the bare scope for FPSF / Canonic. The template uses
    # this for `← Project CP` / `← Initiative CP` navigation links, so
    # they must match the on-disk path.
    project_scope = account_scope_for(project)
    project_dir_slug = dir_slug(project.code, project.name)
    # Relative path from the sprint file (sprints/<week>/<code>.md) to the
    # project's meetings/ dir. Used only when meetings_this_sprint > 0.
    meetings_link = f"../../{project_scope}/{project_dir_slug}/meetings/"
    return template.render(
        project={
            "code": project.code,
            "name": project.name,
            "scope": project_scope,
            "dir_slug": project_dir_slug,
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
        meetings_this_sprint=meetings_this_sprint,
        meetings_link=meetings_link,
        last_session={
            "date": last_session_date,
            "who": last_session_who,
            "summary": last_session_summary,
        },
        recent_commits=recent_commits,
        open_issues=open_issues,
        carry_forward=carry_forward,
    )


def _short_md_date_no_year(iso: str) -> str:
    """Short month-day with no year ('May 11'). Empty input → empty string.

    Distinct from `_short_md_date` only in intent — both produce the same
    "Mon D" format. Aliased here so the current-sprint block renderer's
    Mon-D – Mon-D range reads obviously without commenting at every call
    site that the year is intentionally elided.
    """
    return _short_md_date(iso)


def render_current_sprint_block(sf: SprintFile, link_path: str) -> str:
    """Render the dashboard's "Current sprint" block for one project.

    Top-3 truncation is intentional: this block is meant to be a glanceable
    pointer back into the full sprint file, not a re-statement of it. Open
    asks fall back to carry-forward asks when there are fewer than 3 active
    asks this week. Risks are filtered to escalated + watching (dependency
    risks are excluded — they're informational, not dashboard-worthy).

    `link_path` is passed in by the caller (rather than derived from
    `sf.project_code` + `sf.week_iso`) so the dashboard can compute it
    relative to its own location once and pass it through here.
    """
    asks = list(sf.client_open_asks[:3])
    if len(asks) < 3:
        asks.extend(sf.carry_forward.asks[: 3 - len(asks)])
    asks = asks[:3]
    active = [r for r in sf.risks if r.severity in ("escalated", "watching")]
    risks = active[:3]

    def _hours(h: float) -> str:
        return f"{int(h)}" if h.is_integer() else f"{h}"

    alloc = " · ".join(f"{p.person_name} {_hours(p.hours)}h" for p in sf.allocation)
    week_label = sf.week_iso.split("-W")[-1]
    dates = (
        f"{_short_md_date_no_year(sf.week_start)} – "
        f"{_short_md_date_no_year(sf.week_end)}"
    )
    lines = [
        f"## Current sprint — [W{week_label} ({dates})]({link_path})",
        "",
        f"**Allocation:** {alloc or '—'}",
        f"**Open client asks** ({len(sf.client_open_asks)}):",
    ]
    for a in asks:
        lines.append(f"- {a.text} (asked {a.asked_date})")
    lines.append("")
    lines.append(f"**Active risks** ({len(active)}):")
    for r in risks:
        lines.append(f"- {r.text}")
    lines.append("")
    lines.append(
        f"_See [sprint file]({link_path}) for full plan, horizon, and meeting notes._"
    )
    return "\n".join(lines)


def render_sprint_index(
    *,
    week_iso: str,
    week_dates: str,
    sprint_files: list[SprintFile],
) -> str:
    """Render the per-week sprint-index README listing every active project.

    The Risks column counts active risks (severity in ``escalated`` or
    ``watching``) — same definition used by the master-CP agenda's risk
    rollup, so the two surfaces stay consistent. Decisions due counts
    horizon items with bucket ``decision``.
    """
    week_label = week_iso.split("-")[1] if "-" in week_iso else week_iso
    lines = [
        f"# Sprint {week_label} ({week_dates})",
        "",
        "| Project | Allocation | Asks | Risks | Decisions due | Sprint file |",
        "|---|---|---|---|---|---|",
    ]
    for sf in sprint_files:
        alloc = (
            " · ".join(f"{p.person_name} {int(p.hours)}h" for p in sf.allocation)
            or "—"
        )
        decisions = sum(1 for h in sf.horizon if h.bucket == "decision")
        active_risks = sum(
            1 for r in sf.risks if r.severity in ("escalated", "watching")
        )
        lines.append(
            f"| `{sf.project_code}` | {alloc} | "
            f"{len(sf.client_open_asks)} | {active_risks} | "
            f"{decisions} | [→]({sf.project_code}.md) |"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
#  Idempotent sprint-file writer
# ──────────────────────────────────────────────────────────────────────


def ensure_sprint_file(
    *,
    project: ProjectState,
    sprint_root: Path,
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
    open_issues: tuple,
) -> Path:
    """Create the sprint file if missing, or refresh just its engine regions.

    First-write path: no file at `<sprint_root>/<week_iso>/<project.code>.md`
    yet, so we render the full scaffold and write it. This is the only point
    at which hand-written placeholder text (the empty bullet stubs) lands
    on disk.

    Refresh path: file exists. We re-render the scaffold to derive fresh
    engine-region bodies, then splice ONLY those regions (`sprint-facts`,
    `where-it-stands`, `carry-forward`) into the existing file. Everything
    else — including any hand-written outbound bullets, notes, or status
    flips — is preserved byte-for-byte.

    Carry-forward derivation pulls from the prior sprint when one is named.
    """
    week_dir = sprint_root / week_iso
    week_dir.mkdir(parents=True, exist_ok=True)
    out = week_dir / f"{project.code}.md"

    prior_path = (
        sprint_root / prior_sprint / f"{project.code}.md"
        if prior_sprint
        else None
    )
    cf = (
        compute_carry_forward(prior_path)
        if prior_path is not None
        else CarryForward(asks=(), risks=(), horizon=())
    )

    # Count per-meeting artifacts (deeper-transcripts pipeline) dated in
    # this sprint window. The project's meetings/ dir sits next to its
    # cp.md, under <tenant_root>/<account_scope>/<dir_slug>/. sprint_root
    # is <tenant_root>/sprints, so tenant_root is its parent.
    # account_scope_for handles the per-client nesting (1p/<company>/...).
    meetings_dir = (
        sprint_root.parent
        / account_scope_for(project)
        / dir_slug(project.code, project.name)
        / "meetings"
    )
    meetings_this_sprint = count_sprint_meetings(
        meetings_dir, week_start=week_start, week_end=week_end
    )

    new_body = render_sprint_scaffold(
        project=project,
        week_iso=week_iso,
        week_label=week_label,
        week_start=week_start,
        week_end=week_end,
        prior_sprint=prior_sprint,
        last_sprint_hours_line=last_sprint_hours_line,
        sessions_this_week=sessions_this_week,
        last_session_date=last_session_date,
        last_session_who=last_session_who,
        last_session_summary=last_session_summary,
        recent_commits=recent_commits,
        open_issues=open_issues,
        carry_forward=cf,
        meetings_this_sprint=meetings_this_sprint,
    )

    if not out.exists():
        out.write_text(new_body)
        return out

    # Preserve hand-written regions: re-splice engine regions only.
    # Existing files may pre-date a region (e.g. `where-it-stands` was added
    # later); skip those gracefully rather than mangle hand-written content.
    existing = out.read_text()
    spliced = existing
    for region in ("sprint-facts", "where-it-stands", "carry-forward"):
        try:
            new_inner = _extract_region(new_body, region).strip()
        except ValueError:
            continue
        try:
            spliced = splice_managed_region(spliced, region, new_inner)
        except MarkerMissing:
            continue
    # Only write when the spliced result differs from what's on disk —
    # keeps resync a true no-op (no mtime churn) for unchanged projects.
    if spliced != existing:
        out.write_text(spliced)
    return out


# ──────────────────────────────────────────────────────────────────────
#  scaffold_from_prior — used by auto-ingest when target week is missing
# ──────────────────────────────────────────────────────────────────────


_CP_LINK_RE = re.compile(
    r"^← \[(?P<text>Project CP|Initiative CP)\]\(\.\./\.\./(?P<scope_path>[^)]+?)/cp\.md\)",
    re.MULTILINE,
)


def scaffold_from_prior(
    *,
    tenant_root: Path,
    project_code: str,
    target_week_iso: str,
) -> Path | None:
    """Create a sprint file for `target_week_iso` based on the most recent
    prior sprint file for the same project.

    Used by auto-ingest's `execute_plan` when the target sprint file is
    missing — the most common cause being the late-in-week roll-forward in
    `_planning_monday` (a Wed-Sun meeting wants to land in next week's
    sprint dir, which sync hasn't created yet). Rather than dropping the
    plan, race ahead of sync and scaffold the file.

    Returns the created path. Returns ``None`` when no prior sprint file
    exists for the project (first-ever-ingest edge case) — the caller
    should fall back to logging the error.
    """
    sprints_root = tenant_root / "sprints"
    if not sprints_root.is_dir():
        return None

    prior_weeks = sorted(
        (p.parent.name for p in sprints_root.glob(f"*/{project_code}.md")),
        reverse=True,
    )
    prior_weeks = [w for w in prior_weeks if w < target_week_iso]
    if not prior_weeks:
        return None

    prior_week = prior_weeks[0]
    prior_path = sprints_root / prior_week / f"{project_code}.md"

    project = _project_state_from_sprint_file(prior_path, project_code)
    if project is None:
        return None

    week_start_date, week_end_date = _iso_week_dates(target_week_iso)
    week_start = week_start_date.isoformat()
    week_end = week_end_date.isoformat()
    week_label = f"W{target_week_iso.split('-W')[1]}"

    cf = compute_carry_forward(prior_path)

    new_body = render_sprint_scaffold(
        project=project,
        week_iso=target_week_iso,
        week_label=week_label,
        week_start=week_start,
        week_end=week_end,
        prior_sprint=prior_week,
        last_sprint_hours_line=None,
        sessions_this_week=0,
        last_session_date=None,
        last_session_who=None,
        last_session_summary=None,
        recent_commits=(),
        open_issues=(),
        carry_forward=cf,
        meetings_this_sprint=0,
    )

    target_path = sprints_root / target_week_iso / f"{project_code}.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(new_body)
    return target_path


def _iso_week_dates(week_iso: str) -> tuple[date, date]:
    """Convert ``"2026-W23"`` to (Monday, Sunday) dates via stdlib.

    `date.fromisocalendar(year, week, weekday)` uses ISO 8601 semantics
    (Mon=1 … Sun=7), matching the rest of the engine since v0.10.0.
    """
    year_str, week_str = week_iso.split("-W")
    monday = date.fromisocalendar(int(year_str), int(week_str), 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _project_state_from_sprint_file(
    path: Path, code: str
) -> ProjectState | None:
    """Reconstruct a minimal ProjectState from an existing sprint file.

    We don't have MC-2 here, so we read the frontmatter (for `name`) and
    the `← [Project CP](../../<scope>/<dir>/cp.md)` navigation link (for
    `source` and `company_kind`/`company_name`). Fields we can't recover
    — deal_stage, budget, owner, last_touched — get None / defaults.
    These show up as "—" in the rendered sprint-facts table; the auto-
    ingest path doesn't need them to be accurate.

    Returns None when the file can't be parsed (caller falls back to
    logging the error and dropping the plan).
    """
    try:
        body = path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(body)
    except (OSError, ValueError):
        return None

    project_field = fm.get("Project", "")
    name = (
        project_field.split(" — ", 1)[1].strip()
        if " — " in project_field
        else code
    )

    link_match = _CP_LINK_RE.search(body)
    if not link_match:
        return None

    link_text = link_match.group("text")
    scope_path = link_match.group("scope_path")
    source: EntrySource = "initiative" if link_text == "Initiative CP" else "engagement"

    scope_segment = scope_path.split("/", 1)[0]
    if scope_segment == "1p":
        company_kind: CompanyKind = "client"
        parts = scope_path.split("/")
        company_slug_raw = parts[1] if len(parts) >= 2 else ""
        company_name = company_slug_raw.replace("-", " ").title() or None
    elif scope_segment == "firstpersonsf":
        company_kind = "self-fpsf"
        company_name = "First Person"
    elif scope_segment == "canonic":
        company_kind = "self-canonic"
        company_name = "Canonic"
    else:
        return None

    return ProjectState(
        code=code,
        name=name,
        source=source,
        company_kind=company_kind,
        company_code=None,
        company_name=company_name,
        status="Open" if source != "initiative" else "Active",
        is_internal=False,
        owner=None,
        last_touched=None,
        deadline=None,
    )


# Sprint-window helpers
#
# Week numbering uses ISO 8601 (v0.10.0+). Tue May 26 2026 is W22, matching
# the calendar / Slack / Google Calendar / human convention. Pre-v0.10.0
# the engine used Python's `%W` which produced ISO_week - 1 for all of 2026,
# leading to a one-week mismatch with every other tool. See the iso-week-
# cutover design doc in cp/docs/plans/2026-05-26-* for the rationale.
#
# **Planning-week anchor (v0.8.7.3):** the "current sprint" is whichever week
# is being *planned right now*, which depends on the day of week:
#
#   Mon (0) + Tue (1) → THIS calendar week's Monday  (e.g. Tue May 12 → W20)
#   Wed (2) – Sun (6) → NEXT calendar week's Monday  (e.g. Wed May 13 → W21)
#
# Matches MC-2's `planningWeekMonday()` rule in
# `frontend/src/components/sprint/WeekPicker.tsx` (which also flips to ISO
# in the v0.10.0 cutover). The motivation per the 2026-05-11 retro: when
# the partners open the planner mid-week, they're planning the *upcoming*
# sprint, not reviewing the current one. The cp tenant labels should match
# that intent so /cp-ingest writes to the file that represents "what we're
# planning now."
def is_in_sprint_window(now: datetime) -> bool:
    # Phase 4 keeps every sync inside the window. Refine via existing v0.7.4
    # anchor logic in a later task if needed.
    return True


# Per-meeting artifact filename: `<YYYY-MM-DD>-<slug>.md`, written by the
# deeper-transcripts pipeline. The date prefix is the meeting date.
_MEETING_FILE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})-.+\.md$")


def count_sprint_meetings(
    meetings_dir: Path, *, week_start: str, week_end: str
) -> int:
    """Count per-meeting artifact `.md` files dated within a sprint window.

    `meetings_dir` is a project's `meetings/` directory. `week_start` and
    `week_end` are inclusive ISO dates (the sprint's Monday and Sunday).

    Counts only `<YYYY-MM-DD>-<slug>.md` files whose date prefix falls in
    `[week_start, week_end]`. The sibling `.txt` transcripts and any `.md`
    without a parseable date prefix (e.g. a stray `README.md`) are ignored.
    Returns 0 when the directory does not exist.
    """
    if not meetings_dir.is_dir():
        return 0
    count = 0
    for entry in meetings_dir.iterdir():
        m = _MEETING_FILE_DATE.match(entry.name)
        if m and week_start <= m.group(1) <= week_end:
            count += 1
    return count


def _monday_of(now: datetime) -> date:
    """The Monday of the calendar week containing ``now``. Pure utility —
    does NOT apply the planning-week roll. Use ``_planning_monday`` for
    the sprint-week anchor."""
    today = now.date() if isinstance(now, datetime) else now
    return today - timedelta(days=today.weekday())


def _planning_monday(now: datetime) -> date:
    """The Monday that anchors the *currently-planned* sprint.

    Mon/Tue → this week's Monday. Wed-Sun → next week's Monday. Mirrors
    MC-2's `planningWeekMonday()` so cp and MC-2 agree on which sprint
    label refers to the planning target.
    """
    today = now.date() if isinstance(now, datetime) else now
    weekday = today.weekday()  # 0=Mon, 1=Tue, ..., 6=Sun
    is_late_in_week = weekday >= 2  # Wed or later
    this_monday = today - timedelta(days=weekday)
    return this_monday + timedelta(days=7) if is_late_in_week else this_monday


def current_sprint_week_iso(now: datetime) -> str:
    """ISO 8601 week label for the currently-planned sprint.

    Uses ``isocalendar()`` so the year + week respect ISO-year boundaries
    (e.g. Jan 1 2027 is in ISO 2026-W53). Pre-v0.10.0 this used `%W`
    which silently disagreed with every other tool that uses ISO weeks.
    """
    monday = _planning_monday(now)
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def prior_sprint_week_iso(now: datetime) -> str:
    """ISO 8601 week label for the sprint before the currently-planned one."""
    monday = _planning_monday(now) - timedelta(days=7)
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def sprint_week_dates(now: datetime) -> tuple[str, str]:
    monday = _planning_monday(now)
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def sprint_file_to_dict(sf: SprintFile) -> dict:
    """Recursively serialize a SprintFile to a plain dict.

    `dataclasses.asdict` walks every nested dataclass (Risk, ClientAsk,
    SprintFacts, …) and turns tuples into lists, which is what we want
    for JSON output. Callers should pass the result through
    `json.dumps(..., default=str)` to coerce any stray date/datetime
    values into ISO strings.
    """
    from dataclasses import asdict

    return asdict(sf)


def _is_active_for_sprint(project) -> bool:
    """Mirror the master-CP `is_active` rule from render.py.

    Engagement projects (source="engagement") use the MC-2 status vocabulary
    (`is_active_status`: Deal or Open) AND must not be internal.

    Repo projects (source="repo") — FPSF internal tooling and Canonic
    repos — use the literal "Active" status from the repos table.

    Before this v0.8.1 fix the orchestrator only used `is_active_status`,
    which meant FPSF/Canonic projects (status="Active", not in
    MC_STATUSES) were silently filtered out and never got sprint files.
    """
    source = getattr(project, "source", None)
    if source == "engagement":
        return is_active_status(project.status) and not getattr(project, "is_internal", False)
    return project.status == "Active"


def ensure_sprint_files_for_active_projects(
    *,
    active_projects,
    sprint_root: Path,
    now: datetime,
    per_project_data: dict,
) -> list[Path]:
    """Write a sprint file for each active project in the iterable.

    `active_projects` is a candidate list — projects outside the canonical
    active subset (Deal/Open, per `is_active_status` in `status.py`) are
    filtered out here rather than upstream, so callers can pass any
    iterable of ProjectState. Returns the list of paths actually written
    on disk this call (skipping idempotent no-ops — see
    `ensure_sprint_file`).
    """
    week_iso = current_sprint_week_iso(now)
    prior = prior_sprint_week_iso(now)
    week_start, week_end = sprint_week_dates(now)
    week_label = f"W{int(week_iso.split('-W')[1])}"
    out: list[Path] = []
    for project in active_projects:
        if not _is_active_for_sprint(project):
            continue
        data = per_project_data.get(project.code, {})
        # Track the mtime before so we only report paths where the call
        # actually wrote (ensure_sprint_file is idempotent — refreshing an
        # unchanged file is a no-op on disk). Without this, every resync
        # would mark sprint files as "written" and break sync_tenant's
        # no-op detection.
        sprint_path = sprint_root / week_iso / f"{project.code}.md"
        before = sprint_path.stat().st_mtime_ns if sprint_path.exists() else None
        ensure_sprint_file(
            project=project,
            sprint_root=sprint_root,
            week_iso=week_iso,
            week_label=week_label,
            week_start=week_start,
            week_end=week_end,
            prior_sprint=prior,
            last_sprint_hours_line=data.get("last_sprint_hours_line"),
            sessions_this_week=data.get("sessions_this_week", 0),
            last_session_date=data.get("last_session_date"),
            last_session_who=data.get("last_session_who"),
            last_session_summary=data.get("last_session_summary"),
            recent_commits=data.get("recent_commits", ()),
            open_issues=data.get("open_issues", ()),
        )
        after = sprint_path.stat().st_mtime_ns if sprint_path.exists() else None
        if after is not None and after != before:
            out.append(sprint_path)
    return out
