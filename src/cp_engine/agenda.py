"""Sprint planning agenda renderer — Tier 2.4 / v0.8.8.

Closes the loop the other direction from /cp-ingest: instead of capturing
what happened in a meeting, prepares what should happen in one. Reads cp
tenant state and assembles a project-grouped markdown agenda that bridges
master-cp.md (too sparse) and the per-project sprint files (too heavy).

Per the W19 retro's Tier 2.4: would have prevented "ran out of time before
Tony's projects" by giving partners a single doc to read before the meeting.

Design (locked 2026-05-12 with Drew):
- Project-grouped, alphabetical (matches master-cp.md's display order).
- Per-project: Quick Resume excerpt + recent inbound + open asks aged + decisions due
  + cross-referenced weekly-cp decisions (parsed from `source: <code>` suffix).
- Tenant-wide header: themes/decisions/carry-forward strips from weekly-cp.md.
- Discussion prompt only when a real urgency signal exists (stale ask,
  decision due, escalated risk). Quiet otherwise.
- Two scopes: full sprint planning (all active) or scoped (named projects).

Single source of truth: parses existing artifacts (master-cp.md row data,
weekly-cp.md decisions, project cp.md handwritten Quick Resume, sprint-file
strip regions). No new state, no new files written by this module — the
caller (cp prep-agenda CLI) decides where to emit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from cp_engine.aggregators import (
    ProjectStrips,
    aggregate_project_strips,
    aggregate_tenant_strips,
)
from cp_engine.config import TenantConfig
from cp_engine.render import (
    EXEC_SUMMARY_MIGRATION_BULLET_RE,
    exec_summary_is_authored,
    slice_exec_summary_region,
)
from cp_engine.sprints import (
    current_sprint_week_iso,
    parse_sprint_file,
    parse_themes_from_week_file,
    sprint_week_dates,
)
from cp_engine.state import (
    DecisionEntry,
    InboundUpdate,
    ProjectState,
    Stakeholder,
    account_scope_for,
    dir_slug,
    scope_for,
)
from cp_engine.status import is_active_status

# How many cross-referenced weekly-cp decisions to surface per project.
# Caps to keep the agenda block readable; ordered newest-first.
_MAX_DECISIONS_PER_PROJECT = 6

# How many recent inbound entries to surface per project.
_MAX_INBOUND_PER_PROJECT = 3

# Stale-ask threshold matches aggregators._STALE_ASK_DAYS.
_STALE_ASK_DAYS = 7

# v0.8.8.1: strip the v0.8.6 idempotency markers from rendered text.
# /cp-ingest writes bullets with trailing `<!-- cp:hash=<sha8> -->` so re-runs
# can dedupe; that marker is meant to be invisible in the rendered surface.
# In the agenda we surface the same bullet text via aggregator output, so we
# need to strip the marker again here.
_HASH_MARKER_RE = re.compile(r"\s*<!--\s*cp:hash=[0-9a-f]+\s*-->\s*$")


def _strip_hash_marker(text: str) -> str:
    """Remove a trailing ``<!-- cp:hash=... -->`` marker if present."""
    return _HASH_MARKER_RE.sub("", text).rstrip()


# ──────────────────────────────────────────────────────────────────────
#  Data shapes
# ──────────────────────────────────────────────────────────────────────


@dataclass
class WeeklyDecision:
    """One decision parsed from weekly-cp.md's handwritten decisions list."""

    number: int  # the leading "19." numbering
    text: str  # the bold-marked title + body up to the trailing meta
    date: str  # ISO date from the (YYYY-MM-DD, source: ...) suffix
    sources: tuple[str, ...]  # parsed source codes/labels (could be empty)


@dataclass
class ProjectAgendaBlock:
    """Per-project agenda content, ready to render."""

    project: ProjectState
    quick_resume_excerpt: str | None  # first paragraph of cp.md Quick Resume
    last_touched: str | None
    last_sprint_hours: str | None  # one-line hours summary from master-cp
    recent_inbound: tuple[InboundUpdate, ...]
    open_asks_aged: tuple[dict, ...]  # only those aged > 7 days
    decisions_due: tuple[dict, ...]  # parsed from sprint file's Horizon
    relevant_weekly_decisions: tuple[WeeklyDecision, ...]
    stakeholders: tuple[Stakeholder, ...]
    discussion_prompt: str | None  # set only when a real signal exists

    @property
    def has_urgency(self) -> bool:
        return bool(
            self.open_asks_aged or self.decisions_due or self.discussion_prompt
        )


@dataclass
class TenantAgendaHeader:
    """Tenant-wide header content."""

    week_iso: str
    week_dates: str  # "May 11 – May 17, 2026"
    project_count: int
    estimated_minutes: int  # at ~3 min/project
    themes: tuple[str, ...]  # raw text bullets from themes-strip
    cross_cutting_decisions: tuple[dict, ...]
    carry_forward: dict  # {escalated_risks, stale_asks, decisions_due}


# ──────────────────────────────────────────────────────────────────────
#  Weekly-cp decision parser
# ──────────────────────────────────────────────────────────────────────

# Decisions look like:
#   19. **Marcello hours triage for W19** — drop ... (2026-05-11, source: sprint planning)
#   3. **Drew handles Firebase...** for Go Safety; ... (2026-05-08, source: ggl-5136)
#   8. **Infoblox AI-campaign workshop...** ... (2026-05-07, source: ibx-5167 / ibx-5153)
_DECISION_RE = re.compile(
    r"^\s*(?P<num>\d+)\.\s+(?P<body>.+?)\s*\((?P<date>\d{4}-\d{2}-\d{2}),\s*source:\s*(?P<source>[^)]+)\)\s*$",
    re.MULTILINE | re.DOTALL,
)

# Project-code shape: 2-4 alphanumeric chars + optional hyphen + 1-5 digits.
# Matches ggl-5136, ibx-5167, 1pi-9001, snt-5186 etc.
_CODE_RE = re.compile(r"\b([a-z0-9]{2,4})-(\d{1,5})\b", re.IGNORECASE)


def parse_weekly_decisions(weekly_cp_body: str) -> tuple[WeeklyDecision, ...]:
    """Extract all numbered decisions from weekly-cp.md.

    Stops at the first cp-engine marker (so it doesn't bleed into the
    auto-aggregated decisions-strip region — those are already surfaced
    via aggregators.aggregate_tenant_strips).
    """
    # Truncate to before the first engine marker so we only parse handwritten.
    marker = weekly_cp_body.find("<!-- cp-engine:start")
    body = weekly_cp_body if marker == -1 else weekly_cp_body[:marker]
    out: list[WeeklyDecision] = []
    for m in _DECISION_RE.finditer(body):
        sources = _parse_sources(m.group("source"))
        out.append(
            WeeklyDecision(
                number=int(m.group("num")),
                text=m.group("body").strip(),
                date=m.group("date"),
                sources=sources,
            )
        )
    return tuple(out)


def _parse_sources(raw: str) -> tuple[str, ...]:
    """Split the source field on `/` and `,`, normalize to lowercase tokens.

    A source token might be a project code (`ggl-5136`), a meeting type
    (`sprint planning`, `weekly account meeting`), or a free-form label.
    Codes get lowercased; labels stay verbatim (they aren't matched against
    project codes anyway).
    """
    tokens = re.split(r"[,/]", raw)
    return tuple(t.strip() for t in tokens if t.strip())


def decisions_for_project(
    project_code: str, all_decisions: tuple[WeeklyDecision, ...]
) -> tuple[WeeklyDecision, ...]:
    """Filter decisions whose `sources` field references this project's code."""
    code_lc = project_code.lower()
    return tuple(d for d in all_decisions if code_lc in (s.lower() for s in d.sources))


# ──────────────────────────────────────────────────────────────────────
#  Exec-summary parsing (formerly Quick Resume)
# ──────────────────────────────────────────────────────────────────────

def extract_quick_resume(cp_md_body: str) -> str | None:
    """Pull the cleaned content of the engine-managed `exec-summary` region.

    (Formerly read the `## Quick Resume` heading; that region was replaced by
    the model-authored exec-summary region. Name kept to minimize churn in
    this deprecated module — callers + the `quick_resume_excerpt` field and
    `quick_resume_coverage` metric still reference it.)

    Returns None when the region is missing OR contains only template
    placeholder lines (`_<...>_`) / the auto-stamped migration bullet. The
    placeholder detection is fuzzy on purpose — we don't want to surface
    'Last session: <date>' or 'migrated from Quick Resume' as real content.
    """
    region = slice_exec_summary_region(cp_md_body)
    if region is None or not exec_summary_is_authored(region):
        return None
    # Drop placeholder template lines (`_<...>_`) and the auto migration bullet.
    lines = [
        ln
        for ln in region.splitlines()
        if "_<" not in ln and not EXEC_SUMMARY_MIGRATION_BULLET_RE.match(ln.strip())
    ]
    cleaned = "\n".join(lines).strip()
    return cleaned or None


# ──────────────────────────────────────────────────────────────────────
#  Per-project agenda assembly
# ──────────────────────────────────────────────────────────────────────


def build_project_block(
    project: ProjectState,
    *,
    tenant_root: Path,
    sprint_files: tuple,  # tuple[SprintFile, ...] — avoid circular import
    weekly_decisions: tuple[WeeklyDecision, ...],
    today: date,
    last_sprint_hours: str | None = None,
) -> ProjectAgendaBlock:
    """Assemble all per-project agenda data."""
    # Project working dir → cp.md path. account_scope_for includes the
    # per-client nesting layer (1p/<company>/...) so the path is correct.
    scope = account_scope_for(project)
    slug = dir_slug(project.code, project.name)
    cp_md_path = tenant_root / scope / slug / "cp.md"
    quick_resume = (
        extract_quick_resume(cp_md_path.read_text(encoding="utf-8"))
        if cp_md_path.is_file()
        else None
    )

    # Per-project strip rollup (last 4 weeks of sprint content for this project).
    strips = aggregate_project_strips(project.code, sprint_files, today)
    # Cap recent inbound + filter to aged open asks.
    recent_inbound = strips.inbound[:_MAX_INBOUND_PER_PROJECT]
    aged_asks = tuple(
        a for a in strips.open_asks
        if a.get("aged_days") is not None and a["aged_days"] > _STALE_ASK_DAYS
    )

    # Decisions due from this week's sprint file Horizon section.
    decisions_due = _extract_decisions_due_for_project(project.code, sprint_files, today)

    # Cross-referenced weekly-cp decisions (newest first, capped).
    relevant = decisions_for_project(project.code, weekly_decisions)
    relevant_sorted = sorted(relevant, key=lambda d: d.date, reverse=True)
    relevant_capped = tuple(relevant_sorted[:_MAX_DECISIONS_PER_PROJECT])

    # Discussion prompt: only if there's a real urgency signal.
    prompt = _discussion_prompt(aged_asks, decisions_due, strips.recent_decisions)

    return ProjectAgendaBlock(
        project=project,
        quick_resume_excerpt=quick_resume,
        last_touched=short_iso_date(project.last_touched.isoformat()) if project.last_touched else None,
        last_sprint_hours=last_sprint_hours,
        recent_inbound=recent_inbound,
        open_asks_aged=aged_asks,
        decisions_due=decisions_due,
        relevant_weekly_decisions=relevant_capped,
        stakeholders=strips.stakeholders,
        discussion_prompt=prompt,
    )


def _extract_decisions_due_for_project(
    project_code: str, sprint_files: tuple, today: date
) -> tuple[dict, ...]:
    """Pull `## Horizon → ### Decisions due` items for this week's sprint file.

    Reuses the carry_forward_rollup logic via aggregate_tenant_strips would
    overshoot — that aggregates across all projects. Here we just want this
    one project's horizon-decisions for the current sprint.
    """
    relevant = [sf for sf in sprint_files if sf.project_code == project_code]
    if not relevant:
        return ()
    # Use the most recent sprint file (highest week_start).
    sf = max(relevant, key=lambda s: s.week_start)
    out: list[dict] = []
    for h in sf.horizon:
        if h.bucket == "decision":
            out.append({"text": h.text, "target_date": h.target_date or ""})
    return tuple(out)


def _discussion_prompt(
    aged_asks: tuple[dict, ...],
    decisions_due: tuple[dict, ...],
    recent_decisions: tuple[DecisionEntry, ...],
) -> str | None:
    """Generate a one-line discussion prompt when an urgency signal exists.

    Quiet (returns None) when nothing's burning. Per the design, partners
    should only be nudged when there's a real reason to talk.
    """
    parts: list[str] = []
    if aged_asks:
        max_aged = max(a.get("aged_days") or 0 for a in aged_asks)
        parts.append(f"{len(aged_asks)} open ask{'s' if len(aged_asks) != 1 else ''} aged > 7d (oldest: {max_aged}d)")
    if decisions_due:
        parts.append(f"{len(decisions_due)} decision{'s' if len(decisions_due) != 1 else ''} due this/next sprint")
    if not parts:
        return None
    return "Discuss: " + "; ".join(parts) + "."


# ──────────────────────────────────────────────────────────────────────
#  Tenant-wide header
# ──────────────────────────────────────────────────────────────────────


def build_tenant_header(
    config: TenantConfig,
    *,
    sprint_files: tuple,
    today: date,
    project_count: int,
) -> TenantAgendaHeader:
    """Assemble tenant-wide agenda header data."""
    week_iso = current_sprint_week_iso(to_datetime(today))
    week_start, week_end = sprint_week_dates(to_datetime(today))
    week_label = f"{short_iso_date(week_start)} – {short_iso_date(week_end)}"

    # Themes from current + prior week's _week.md (matches sync.py's tenant-strip flow).
    from cp_engine.sprints import prior_sprint_week_iso
    prior_iso = prior_sprint_week_iso(to_datetime(today))
    themes_list = []
    for w in (week_iso, prior_iso):
        wpath = config.root / "sprints" / w / "_week.md"
        themes_list.extend(parse_themes_from_week_file(wpath))

    tenant_strips = aggregate_tenant_strips(sprint_files, tuple(themes_list), today)

    return TenantAgendaHeader(
        week_iso=week_iso,
        week_dates=week_label,
        project_count=project_count,
        estimated_minutes=project_count * 3,
        themes=tuple(t.text for t in tenant_strips.themes),
        cross_cutting_decisions=tenant_strips.cross_cutting_decisions,
        carry_forward=tenant_strips.carry_forward,
    )


# ──────────────────────────────────────────────────────────────────────
#  Markdown renderer
# ──────────────────────────────────────────────────────────────────────


def render_agenda_markdown(
    header: TenantAgendaHeader,
    project_blocks: tuple[ProjectAgendaBlock, ...],
    *,
    generated_at: str,
) -> str:
    """Render the full agenda as markdown."""
    lines: list[str] = []

    # H1 + one-line meta.
    lines.append(
        f"# Sprint {header.week_iso} Planning Agenda — {header.week_dates}"
    )
    lines.append(
        f"_Generated {generated_at} by `cp prep-agenda` · "
        f"{header.project_count} active projects · "
        f"est. {header.estimated_minutes} min @ 3min/project_"
    )
    lines.append("")

    # Tenant-wide context.
    lines.append("## Tenant-wide context")
    lines.append("")
    if header.themes:
        lines.append("**This week's themes:**")
        for t in header.themes:
            lines.append(f"- {_strip_hash_marker(t)}")
        lines.append("")
    cf = header.carry_forward
    has_carry = bool(
        cf.get("escalated_risks") or cf.get("stale_asks") or cf.get("decisions_due")
    )
    if has_carry:
        lines.append("**Carry-forward across the tenant:**")
        if cf.get("escalated_risks"):
            lines.append(f"- Escalated risks ({len(cf['escalated_risks'])}):")
            for r in cf["escalated_risks"]:
                lines.append(f"  - `{r['project_code']}`: {r['text']}")
        if cf.get("stale_asks"):
            lines.append(f"- Open asks aged > 7d ({len(cf['stale_asks'])}):")
            for a in cf["stale_asks"]:
                lines.append(f"  - `{a['project_code']}` ({a['aged_days']}d): {a['text']}")
        if cf.get("decisions_due"):
            lines.append(f"- Decisions due (next +2 sprints):")
            for d in cf["decisions_due"]:
                target = f" ({d['target_date']})" if d.get("target_date") else ""
                lines.append(f"  - `{d['project_code']}`{target}: {d['text']}")
        lines.append("")
    if header.cross_cutting_decisions:
        lines.append("**Cross-cutting decisions, last 4 weeks:**")
        for d in header.cross_cutting_decisions:
            lines.append(f"- [{d['date']} · `{d['project_code']}`] {_strip_hash_marker(d['text'])}")
        lines.append("")
    if not (header.themes or has_carry or header.cross_cutting_decisions):
        lines.append("_No tenant-wide context surfaced this sprint._")
        lines.append("")

    # Per-project blocks.
    lines.append("## Per-project")
    lines.append("")
    for block in project_blocks:
        lines.extend(_render_project_block(block))

    return "\n".join(lines).rstrip() + "\n"


def _render_project_block(block: ProjectAgendaBlock) -> list[str]:
    """Render one project's section."""
    p = block.project
    out: list[str] = []
    # H3 with code, name, owner, urgency emoji if applicable.
    urgency_marker = " 🔴" if block.has_urgency else ""
    owner = p.owner or "—"
    out.append(f"### `{p.code}` {p.name} — {owner}{urgency_marker}")
    # One-line meta.
    meta_parts = []
    if block.last_touched:
        meta_parts.append(f"Last touched: {block.last_touched}")
    if block.last_sprint_hours:
        meta_parts.append(f"Last sprint: {block.last_sprint_hours}")
    if meta_parts:
        out.append(f"_{' · '.join(meta_parts)}_")
    out.append("")

    # Quick Resume excerpt (single paragraph, max ~6 lines).
    if block.quick_resume_excerpt:
        excerpt = block.quick_resume_excerpt
        # Clamp to keep agenda readable.
        if excerpt.count("\n") > 6:
            kept_lines = excerpt.splitlines()[:6]
            excerpt = "\n".join(kept_lines) + "\n…"
        out.append("**Quick Resume:**")
        out.append(excerpt)
        out.append("")

    # Recent inbound.
    if block.recent_inbound:
        out.append("**Recent inbound:**")
        for ib in block.recent_inbound:
            out.append(f"- [{ib.date} · {ib.who}] {_strip_hash_marker(ib.text)}")
        out.append("")

    # Open asks aged > 7d.
    if block.open_asks_aged:
        out.append(f"**Open asks aged > 7d ({len(block.open_asks_aged)}):**")
        for a in block.open_asks_aged:
            who = f" · {a['who']}" if a.get("who") else ""
            out.append(f"- [{a['asked_date']}{who} · {a['aged_days']}d stale] {_strip_hash_marker(a['text'])}")
        out.append("")

    # Decisions due.
    if block.decisions_due:
        out.append("**Decisions due this/next sprint:**")
        for d in block.decisions_due:
            target = f" ({d['target_date']})" if d.get("target_date") else ""
            out.append(f"- {d['text']}{target}")
        out.append("")

    # Cross-referenced weekly decisions.
    if block.relevant_weekly_decisions:
        out.append("**Relevant decisions (from `weekly-cp.md`):**")
        for wd in block.relevant_weekly_decisions:
            # Trim very long decision text to keep the agenda readable.
            text = wd.text if len(wd.text) <= 200 else wd.text[:200].rstrip() + "…"
            out.append(f"- #{wd.number} ({wd.date}) — {text}")
        out.append("")

    # Discussion prompt last.
    if block.discussion_prompt:
        out.append(f"> 💬 {block.discussion_prompt}")
        out.append("")

    return out


# ──────────────────────────────────────────────────────────────────────
#  Top-level entry point
# ──────────────────────────────────────────────────────────────────────


def build_agenda(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    *,
    today: date,
    project_filter: tuple[str, ...] | None = None,
    last_sprint_hours_by_project: dict[str, str] | None = None,
) -> str:
    """Top-level entry: assemble + render the full agenda as markdown.

    Args:
        config: tenant config (gives tenant root + scope rules).
        projects: full ProjectState list (e.g. from MC2Backend.read_projects).
            Filtered to active here based on each project's source.
        today: date to anchor "current sprint" against.
        project_filter: if provided, restricts the agenda to these codes.
            None → full sprint planning (all active projects).
        last_sprint_hours_by_project: optional map of code → "Tony 12h, Drew 4h"
            string from master-cp's last-week-workload data. The CLI populates
            this from the same MC-2 allocations source master-cp uses.
    """
    # Filter to active.
    active = tuple(filter_active(projects))
    if project_filter:
        wanted = {c.lower() for c in project_filter}
        active = tuple(p for p in active if p.code.lower() in wanted)

    # Sort alphabetical (matches master-cp's display order within scope).
    active_sorted = tuple(sorted(active, key=lambda p: (scope_for(p.company_kind), p.code)))

    # Load all sprint files for the current week (drives strips + aggregator).
    week_iso = current_sprint_week_iso(to_datetime(today))
    sprint_dir = config.root / "sprints" / week_iso
    sprint_files = _load_sprint_files(sprint_dir)

    # Parse weekly-cp.md decisions once.
    weekly_path = config.root / "weekly-cp.md"
    weekly_decisions: tuple[WeeklyDecision, ...] = ()
    if weekly_path.is_file():
        weekly_decisions = parse_weekly_decisions(weekly_path.read_text(encoding="utf-8"))

    # Build the header.
    header = build_tenant_header(
        config,
        sprint_files=sprint_files,
        today=today,
        project_count=len(active_sorted),
    )

    # Build per-project blocks.
    blocks = tuple(
        build_project_block(
            p,
            tenant_root=config.root,
            sprint_files=sprint_files,
            weekly_decisions=weekly_decisions,
            today=today,
            last_sprint_hours=(last_sprint_hours_by_project or {}).get(p.code),
        )
        for p in active_sorted
    )

    from datetime import datetime
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    return render_agenda_markdown(header, blocks, generated_at=generated_at)


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def filter_active(projects: tuple[ProjectState, ...]):
    """Engagement: status active + not internal. Repo: status == 'Active'."""
    for p in projects:
        if p.source == "engagement":
            if is_active_status(p.status) and not p.is_internal:
                yield p
        elif p.status == "Active":
            yield p


def _load_sprint_files(sprint_dir: Path) -> tuple:
    """Parse every <code>.md sprint file in sprint_dir. Skips on parse error."""
    if not sprint_dir.is_dir():
        return ()
    out = []
    import logging
    logger = logging.getLogger("cp_engine.agenda")
    for path in sorted(sprint_dir.glob("*.md")):
        # Skip the per-week sentinel files: README.md (sprint-index), _week.md
        # (week-scope notes), _agenda.md (this command's own output), and any
        # other underscore-prefixed sentinel that's not a per-project sprint file.
        if path.name in ("README.md", "_week.md", "_agenda.md"):
            continue
        if path.name.startswith("_"):
            continue
        try:
            out.append(parse_sprint_file(path))
        except (ValueError, OSError) as exc:
            logger.warning("Skipping sprint file %s: %s", path, exc)
    return tuple(out)


def to_datetime(d: date):
    """current_sprint_week_iso wants a datetime; build one at midnight UTC."""
    from datetime import datetime, timezone
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def short_iso_date(iso: str | None) -> str | None:
    """Render '2026-05-12' as 'May 12'. Returns None on falsy input."""
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso[:10])
        return d.strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return iso


# ──────────────────────────────────────────────────────────────────────
#  v0.8.8.2: Owner normalization + summary JSON
# ──────────────────────────────────────────────────────────────────────


def normalize_owner(raw: str | None) -> str:
    """Collapse owner-name variants into a stable workload-bucket key.

    MC-2's `account_manager` field is free-form text. The same person
    appears under multiple strings:
        "Drew Fiero", "Drew", "Drew + Tony", "Drew and Tony", "Drew and Marcello"
    All five represent Drew (sometimes co-owned with another person).
    For the workload summary the plugin surfaces, we want to bucket by
    *primary* owner so the "consider splitting if X has >>others" callout
    is meaningful.

    Strategy: take the first whitespace-separated token (lowercased), strip
    common punctuation. "Drew Fiero" → "drew"; "Drew + Tony" → "drew";
    "Tony Welch" → "tony". Returns "(unowned)" for None/empty.

    The agenda's RENDERED text preserves the literal MC-2 string for fidelity
    — only the workload summary uses this normalized key.
    """
    if not raw:
        return "(unowned)"
    first = raw.strip().split()[0] if raw.strip() else ""
    return first.strip("+,").lower() or "(unowned)"


@dataclass
class AgendaSummary:
    """Brief metrics the /cp-prep plugin surfaces before/instead of the full agenda."""

    week_iso: str
    week_dates: str
    project_count: int
    estimated_minutes: int

    themes_count: int
    cross_cutting_decisions_count: int

    quick_resume_coverage: int  # how many projects have non-template Quick Resume
    recent_inbound_coverage: int  # how many have any recent inbound bullets
    cross_referenced_decisions_coverage: int  # how many have weekly-cp decisions
    urgency_flagged_count: int  # has_urgency=True
    discussion_prompt_count: int  # discussion_prompt is not None

    workload_by_owner: list[dict]  # [{owner_normalized, owner_display_strings, count, codes}]

    def to_dict(self) -> dict:
        return {
            "week_iso": self.week_iso,
            "week_dates": self.week_dates,
            "project_count": self.project_count,
            "estimated_minutes": self.estimated_minutes,
            "themes_count": self.themes_count,
            "cross_cutting_decisions_count": self.cross_cutting_decisions_count,
            "coverage": {
                "quick_resume": self.quick_resume_coverage,
                "recent_inbound": self.recent_inbound_coverage,
                "cross_referenced_decisions": self.cross_referenced_decisions_coverage,
            },
            "urgency": {
                "flagged_projects": self.urgency_flagged_count,
                "discussion_prompts": self.discussion_prompt_count,
            },
            "workload_by_owner": self.workload_by_owner,
        }


def build_agenda_summary(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    *,
    today: date,
    project_filter: tuple[str, ...] | None = None,
    last_sprint_hours_by_project: dict[str, str] | None = None,
) -> AgendaSummary:
    """Build the AgendaSummary corresponding to what build_agenda would render.

    Reuses the same internal pipeline so the summary always matches the
    rendered doc. Plugins call this when they want metrics without
    parsing the markdown back out.
    """
    active = tuple(filter_active(projects))
    if project_filter:
        wanted = {c.lower() for c in project_filter}
        active = tuple(p for p in active if p.code.lower() in wanted)
    active_sorted = tuple(sorted(active, key=lambda p: (scope_for(p.company_kind), p.code)))

    week_iso = current_sprint_week_iso(to_datetime(today))
    sprint_dir = config.root / "sprints" / week_iso
    sprint_files = _load_sprint_files(sprint_dir)

    weekly_path = config.root / "weekly-cp.md"
    weekly_decisions: tuple[WeeklyDecision, ...] = ()
    if weekly_path.is_file():
        weekly_decisions = parse_weekly_decisions(weekly_path.read_text(encoding="utf-8"))

    header = build_tenant_header(
        config,
        sprint_files=sprint_files,
        today=today,
        project_count=len(active_sorted),
    )

    blocks = tuple(
        build_project_block(
            p,
            tenant_root=config.root,
            sprint_files=sprint_files,
            weekly_decisions=weekly_decisions,
            today=today,
            last_sprint_hours=(last_sprint_hours_by_project or {}).get(p.code),
        )
        for p in active_sorted
    )

    # Bucket projects by normalized owner.
    bucket: dict[str, dict] = {}
    for p in active_sorted:
        key = normalize_owner(p.owner)
        b = bucket.setdefault(
            key,
            {"owner_normalized": key, "owner_display_strings": [], "count": 0, "codes": []},
        )
        b["count"] += 1
        b["codes"].append(p.code)
        if p.owner and p.owner not in b["owner_display_strings"]:
            b["owner_display_strings"].append(p.owner)
    workload = sorted(bucket.values(), key=lambda r: -r["count"])

    return AgendaSummary(
        week_iso=header.week_iso,
        week_dates=header.week_dates,
        project_count=header.project_count,
        estimated_minutes=header.estimated_minutes,
        themes_count=len(header.themes),
        cross_cutting_decisions_count=len(header.cross_cutting_decisions),
        quick_resume_coverage=sum(1 for b in blocks if b.quick_resume_excerpt),
        recent_inbound_coverage=sum(1 for b in blocks if b.recent_inbound),
        cross_referenced_decisions_coverage=sum(
            1 for b in blocks if b.relevant_weekly_decisions
        ),
        urgency_flagged_count=sum(1 for b in blocks if b.has_urgency),
        discussion_prompt_count=sum(1 for b in blocks if b.discussion_prompt),
        workload_by_owner=workload,
    )


# ──────────────────────────────────────────────────────────────────────
#  v0.8.8.2: Auto-sync staleness check
# ──────────────────────────────────────────────────────────────────────

# Master-cp's `last-sync-timestamp` engine region encodes the most recent sync.
# /cp-prep auto-runs sync if it's older than this threshold so the agenda
# never shows stale owner/budget/last-touched data.
SYNC_STALENESS_THRESHOLD_MINUTES = 10


def master_cp_last_sync(config: TenantConfig) -> datetime | None:
    """Read the timestamp from master-cp.md's `last-sync-timestamp` region.

    Returns None if master-cp.md doesn't exist OR the region is empty/missing.
    """
    from datetime import datetime, timezone
    master = config.root / "master-cp.md"
    if not master.is_file():
        return None
    body = master.read_text(encoding="utf-8")
    m = re.search(
        r"<!-- cp-engine:start last-sync-timestamp -->(.*?)<!-- cp-engine:end last-sync-timestamp -->",
        body,
        re.DOTALL,
    )
    if not m:
        return None
    region = m.group(1)
    ts_match = re.search(r"\*\*Last sync:\*\*\s*(\S+)", region)
    if not ts_match:
        return None
    try:
        return datetime.fromisoformat(ts_match.group(1).rstrip("Z"))
    except ValueError:
        return None


def is_sync_stale(
    config: TenantConfig, *, threshold_minutes: int = SYNC_STALENESS_THRESHOLD_MINUTES
) -> bool:
    """True if the last sync was more than threshold_minutes ago (or never).

    Conservative: returns True when the timestamp can't be parsed (run sync
    rather than risk stale data).
    """
    from datetime import datetime, timezone, timedelta
    last = master_cp_last_sync(config)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last
    return age > timedelta(minutes=threshold_minutes)


# This datetime import is used by the v0.8.8.2 helpers above. Defining it here
# at module scope (instead of inside each helper) is cleaner — the duplicate
# imports inside functions remain for backward-compat with prior call sites.
from datetime import datetime, timezone, timedelta  # noqa: E402, F401
