"""Projections of sprint-file content into durable surfaces.

Phase 1.2 (v0.8.5) introduced the engine-managed strip regions on per-project
`cp.md` files and the cross-cutting rollup. All of them aggregate sprint-file
handwritten content into durable, projectable views.

Two aggregator functions, separated by scope:

1. ``aggregate_project_strips(project_code, sprint_files, today)`` — projects
   per-project content (Inbound, Decisions, Open asks, Stakeholders) into
   the four new regions on a project's ``cp.md``.

2. ``aggregate_subtree_strips(root_code, sprint_files, themes, today,
   by_code)`` — projects cross-cutting decisions, themes and carry-forward
   for the SUBTREE rooted at ``root_code`` (``None`` = the whole tenant).
   The tenant call feeds ``master-cp.md``'s ``agenda`` region and the prep
   agenda header; the per-node calls feed an account node's or a program's
   sprint file ``carry-forward`` region (cp-engine #303, plan §3.5).
   ``aggregate_tenant_strips`` is the tenant-rooted spelling.

The existing ``_compute_agenda_rollup`` in ``render.py`` shares its core logic
with ``aggregate_tenant_strips``'s carry-forward output — same parser, same
data, different render template. The shared helper is ``_carry_forward_rollup``
below.

Time windows are pragmatic, not load-bearing:
- Project strips: last 4 weeks (28 days) for inbound + decisions; all-time
  for open asks (until closed) and stakeholders (dedupe by name).
- Tenant strips: last 2 weeks for themes; last 4 weeks for cross-cutting
  decisions; carry-forward is severity/age-gated, not time-gated.

Tunable later if these defaults turn out wrong on real tenant data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping

from cp_engine.state import (
    DecisionEntry,
    InboundUpdate,
    ProjectState,
    Risk,
    SprintFile,
    Stakeholder,
    Theme,
    descendants_of,
)

# Project-strip window: last 4 weeks for recent-decisions + inbound.
_PROJECT_RECENCY_DAYS = 28

# Tenant-strip windows.
_TENANT_DECISIONS_DAYS = 28
_TENANT_THEMES_DAYS = 14

# Stale-ask threshold: matches the existing `agenda` rule in render.py.
_STALE_ASK_DAYS = 7


# ──────────────────────────────────────────────────────────────────────
#  Project strips (project cp.md regions)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectStrips:
    """Aggregated content for one project's four cp.md engine regions.

    Each field maps 1:1 to a region:
    - ``inbound`` → ``inbound-strip``
    - ``recent_decisions`` → ``recent-decisions-strip``
    - ``open_asks`` → ``open-asks-strip``
    - ``stakeholders`` → ``stakeholders-strip``
    """

    inbound: tuple[InboundUpdate, ...]
    recent_decisions: tuple[DecisionEntry, ...]
    open_asks: tuple[dict, ...]  # {text, asked_date, who, aged_days}
    stakeholders: tuple[Stakeholder, ...]


def aggregate_project_strips(
    project_code: str,
    sprint_files: tuple[SprintFile, ...],
    today: date,
) -> ProjectStrips:
    """Aggregate per-project content from this project's sprint files.

    Only sprint files matching ``project_code`` contribute. Order: newest
    first by ``week_start`` (so a project's most recent sprint is first).
    """
    relevant = sorted(
        (sf for sf in sprint_files if sf.project_code == project_code),
        key=lambda s: s.week_start,
        reverse=True,
    )
    cutoff = today - timedelta(days=_PROJECT_RECENCY_DAYS)

    inbound: list[InboundUpdate] = []
    recent_decisions: list[DecisionEntry] = []
    open_asks: list[dict] = []
    stakeholder_by_name: dict[str, Stakeholder] = {}

    for sf in relevant:
        # Inbound: filter by date within window. Parser may emit empty `date`
        # for malformed brackets; skip those rather than guess.
        for ib in sf.client_inbound:
            d = _parse_iso_date(ib.date)
            if d is None or d < cutoff:
                continue
            inbound.append(ib)

        # Recent decisions (bracket-formatted, v0.8.5+).
        for dec in sf.decisions:
            d = _parse_iso_date(dec.date)
            if d is None or d < cutoff:
                continue
            recent_decisions.append(dec)

        # Open asks: keep all open asks regardless of age; the rendered
        # surface highlights stale ones via aged_days.
        for ask in sf.client_open_asks:
            if ask.status != "open":
                continue
            asked = _parse_iso_date(ask.asked_date)
            aged_days = (today - asked).days if asked else None
            open_asks.append(
                {
                    "text": ask.text,
                    "asked_date": ask.asked_date,
                    "who": ask.who,
                    "aged_days": aged_days,
                }
            )

        # Stakeholders: dedupe by name across all sprints. Most-recent
        # mention wins for role + context (since we iterate newest first
        # and only insert when the name is new).
        for sh in sf.stakeholders:
            if sh.name not in stakeholder_by_name:
                stakeholder_by_name[sh.name] = sh

    return ProjectStrips(
        inbound=tuple(inbound),
        recent_decisions=tuple(recent_decisions),
        open_asks=tuple(open_asks),
        stakeholders=tuple(stakeholder_by_name.values()),
    )


# ──────────────────────────────────────────────────────────────────────
#  Subtree strips (tenant root, account nodes, programs)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantStrips:
    """Aggregated cross-cutting content for one subtree of the tree.

    - ``cross_cutting_decisions``: DecisionEntry rows flagged
      ``cross_cutting=True`` across the subtree's sprint files in the window.
    - ``themes``: parsed from ``sprints/<W##>/_week.md`` ``## Themes``
      sections in the window (tenant-wide by nature; the caller passes them).
    - ``carry_forward``: open asks aged > 7 days, escalated risks, decisions
      due — the same rollup ``master-cp.md``'s ``agenda`` region renders.
    - ``root_code``: the subtree root, ``None`` for the tenant.
    - ``workstream_count``: how many sprint files fed the rollup.

    The class keeps its tenant-era name: every consumer reads the same
    three fields whether the root is the tenant or one node.
    """

    cross_cutting_decisions: tuple[dict, ...]  # {project_code, text, date}
    themes: tuple[Theme, ...]
    carry_forward: dict  # {escalated_risks, stale_asks, decisions_due}
    root_code: str | None = None
    workstream_count: int = 0


def aggregate_subtree_strips(
    root_code: str | None,
    sprint_files: tuple[SprintFile, ...],
    themes: tuple[Theme, ...],
    today: date,
    by_code: "Mapping[str, ProjectState] | None" = None,
) -> TenantStrips:
    """Aggregate cross-cutting content for the subtree rooted at ``root_code``.

    ``None`` is the tenant: every sprint file contributes. Otherwise only
    the files of ``root_code`` and its descendants (via ``parent_code`` in
    ``by_code``) contribute — an account node's rollup is its jobs and
    programs, a program's is its jobs. A root with no roster entry rolls up
    only its own file.

    ``themes`` is passed in (rather than discovered here) because the caller
    knows the tenant root and can collect ``sprints/<W##>/_week.md`` files
    across recent weeks. Aggregator filters by recency.
    """
    if root_code is not None:
        in_scope = {root_code} | {p.code for p in descendants_of(root_code, by_code or {})}
        sprint_files = tuple(sf for sf in sprint_files if sf.project_code in in_scope)

    cutoff_decisions = today - timedelta(days=_TENANT_DECISIONS_DAYS)
    cutoff_themes = today - timedelta(days=_TENANT_THEMES_DAYS)

    cross_cutting: list[dict] = []
    for sf in sprint_files:
        for dec in sf.decisions:
            if not dec.cross_cutting:
                continue
            d = _parse_iso_date(dec.date)
            if d is None or d < cutoff_decisions:
                continue
            cross_cutting.append(
                {
                    "project_code": sf.project_code,
                    "text": dec.text,
                    "date": dec.date,
                }
            )

    filtered_themes = tuple(
        t for t in themes
        if (d := _parse_iso_date(t.date)) is not None and d >= cutoff_themes
    )

    carry_forward = _carry_forward_rollup(sprint_files, today)

    return TenantStrips(
        cross_cutting_decisions=tuple(cross_cutting),
        themes=filtered_themes,
        carry_forward=carry_forward,
        root_code=root_code,
        workstream_count=len({sf.project_code for sf in sprint_files}),
    )


def aggregate_tenant_strips(
    sprint_files: tuple[SprintFile, ...],
    themes: tuple[Theme, ...],
    today: date,
) -> TenantStrips:
    """The tenant-rooted subtree: ``aggregate_subtree_strips(None, …)``."""
    return aggregate_subtree_strips(None, sprint_files, themes, today)


# ──────────────────────────────────────────────────────────────────────
#  Shared carry-forward rollup (master-cp.md agenda + subtree carry-forward)
# ──────────────────────────────────────────────────────────────────────


def carry_forward_rollup(
    sprint_files: tuple[SprintFile, ...],
    today: date,
) -> dict:
    """Public entry point for the rollup used by master-cp.md's `agenda`
    region and a parent node's sprint-file `carry-forward` region.

    Both consumers want the same three lists; only the render template
    differs. Sharing the parser means a bug fix (or a threshold change)
    lands in one place.
    """
    return _carry_forward_rollup(sprint_files, today)


def _carry_forward_rollup(
    sprint_files: tuple[SprintFile, ...],
    today: date,
) -> dict:
    escalated_risks: list[dict] = []
    stale_asks: list[dict] = []
    decisions_due: list[dict] = []

    monday = today - timedelta(days=today.weekday())
    # ISO 8601 week number (v0.10.0+); was `%W` previously, which produced
    # ISO_week - 1 for all of 2026 and disagreed with the rest of cp.
    current_week_num = monday.isocalendar().week

    for sf in sprint_files:
        for risk in sf.risks:
            if risk.severity == "escalated":
                escalated_risks.append(
                    {"project_code": sf.project_code, "text": risk.text}
                )

        for ask in sf.client_open_asks:
            if ask.status != "open":
                continue
            asked = _parse_iso_date(ask.asked_date)
            if asked is None:
                continue
            aged_days = (today - asked).days
            if aged_days > _STALE_ASK_DAYS:
                stale_asks.append(
                    {
                        "project_code": sf.project_code,
                        "text": ask.text,
                        "aged_days": aged_days,
                    }
                )

        for h in sf.horizon:
            if h.bucket != "decision":
                continue
            target_week = _parse_week_target(h.target_date)
            # Non-week targets (empty, "TBD", literal dates) → over-surface
            # (None case). Week targets → only surface if +1 or +2 sprints
            # from current (matches render.py's existing agenda behavior).
            if target_week is None or (target_week - current_week_num) in (1, 2):
                decisions_due.append(
                    {
                        "project_code": sf.project_code,
                        "text": h.text,
                        "target_date": h.target_date or "",
                    }
                )

    return {
        "escalated_risks": escalated_risks,
        "stale_asks": stale_asks,
        "decisions_due": decisions_due,
    }


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _parse_week_target(target: str | None) -> int | None:
    """Parse `W19`, `by W19`, `2026-W19` etc. into the week number.

    Returns None for non-week formats (TBD, literal date strings) so the
    caller can decide to over-surface rather than silently drop.
    """
    if not target:
        return None
    import re
    m = re.search(r"W(\d{1,2})", target)
    if not m:
        return None
    return int(m.group(1))
