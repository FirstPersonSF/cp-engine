"""Renderers for tenant CP files.

Two distinct jobs in this module:

1. **Full-file generation** (renderers below). Used when scaffolding a new
   file. Jinja templates in `cp_engine/templates/`. Replaces the entire
   file body.

2. **In-place region splice** (`splice_managed_region`). Used on every
   sync after a file already exists. Replaces *only* the body between
   `<!-- cp-engine:start <name> -->` / `<!-- cp-engine:end <name> -->`
   markers; preserves all hand-written content outside markers.

Outside the markers is sacred. The splicer raises loudly on any ambiguity
(missing markers, multiple markers for the same region, end before start).
See spec v02 §4.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Mapping

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cp_engine import __version__ as ENGINE_VERSION
from cp_engine.config import TenantConfig
from cp_engine.state import (
    Issue,
    LinkedRepo,
    ProjectState,
    SprintFile,
    children_of,
    dir_name_for,
    display_name,
    effective_label,
    parent_path_for,
    path_for,
    company_slug,
    tree_depth,
)
from cp_engine.status import is_active_status

# ──────────────────────────────────────────────────────────────────────
#  Errors
# ──────────────────────────────────────────────────────────────────────


class RenderError(Exception):
    """Base class for render errors."""


class MarkerMissing(RenderError):
    """A required `cp-engine:start` or `cp-engine:end` marker isn't in the file."""


class MarkerDuplicated(RenderError):
    """Multiple start or end markers exist for the same region — ambiguous."""


class MarkerInverted(RenderError):
    """The end marker appears before the start marker."""


# ──────────────────────────────────────────────────────────────────────
#  Section summaries (auto-generated one-liner under each active h2)
# ──────────────────────────────────────────────────────────────────────

# Per-section noun-phrase fragment. The summary helper prefixes a
# spelled-out count: f"{Count} {state_phrase}".
_SECTION_STATE_PHRASES: dict[str, str] = {
    "pipeline": "deals in flight",
    "tree": "workstreams in motion",
}

# The label words the tree and the Facts tables render. `ProjectState.label`
# is the derived vocabulary (design doc 2026-09-22, decision 3); this is
# only its capitalised spelling.
_LABEL_WORDS: dict[str, str] = {
    "account": "Account",
    "program": "Program",
    "job": "Job",
    "initiative": "Initiative",
}

# The kind word in a scaffolded cp.md's H1 ("Google — Account CP"). A job
# keeps the established "Project CP" phrase the sprint files link back to.
_CP_KIND_WORDS: dict[str, str] = {
    "account": "Account",
    "program": "Program",
    "job": "Project",
    "initiative": "Initiative",
}

# Company display order in the tree region (D9): client companies first
# (sorted by name), then First Person, then Canonic.
_COMPANY_KIND_ORDER: dict[str, int] = {"client": 0, "self-fpsf": 1, "self-canonic": 2}

# Spell out small counts; digits otherwise. Index 0 is intentionally
# lowercase "zero" since count==0 suppresses the line (the helper
# returns None), so it should never be rendered.
_SPELLED_COUNTS = (
    "zero", "One", "Two", "Three", "Four", "Five",
    "Six", "Seven", "Eight", "Nine", "Ten",
)


def _section_summary(count: int, state_phrase: str) -> str | None:
    """Format the auto-generated section summary, e.g. "Three deals in flight".

    Returns None when `count == 0` so the template can suppress the
    summary line entirely (a "Zero X in Y" sentence reads awkwardly,
    and the heading already shows "(0)").
    """
    if count <= 0:
        return None
    word = _SPELLED_COUNTS[count] if 1 <= count <= 10 else str(count)
    return f"{word} {state_phrase}"


# ──────────────────────────────────────────────────────────────────────
#  Jinja environment
# ──────────────────────────────────────────────────────────────────────


def _env() -> Environment:
    """Load templates from the installed package's templates/ directory.

    Uses importlib.resources so it works in both editable installs (`pip
    install -e .`) and in installed wheels.
    """
    templates_path = resources.files("cp_engine") / "templates"
    return Environment(
        loader=FileSystemLoader(str(templates_path)),
        autoescape=select_autoescape(disabled_extensions=("md", "j2")),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _today_iso() -> str:
    return date.today().isoformat()


def _short(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%d") if d else None


# ──────────────────────────────────────────────────────────────────────
#  Full-file renderers
# ──────────────────────────────────────────────────────────────────────


def render_master_cp(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    last_sync: datetime,
    allocations=None,  # WeeklyAllocations | None
    exceptions_count: int = 0,
    current_sprint_iso: str | None = None,
    prior_sprint_iso: str | None = None,
    parsed_sprint_files: tuple[SprintFile, ...] | None = None,
    today: date | None = None,
) -> str:
    """Render the full master-cp.md body.

    Two active regions (#303, plan D9):

    - `active-pipeline` — Deal-stage jobs, stage-sorted, as before.
    - `active-tree` — every other active workstream, grouped by company
      (client companies first, sorted by name, then First Person, then
      Canonic), each company a `### <Company>` block whose rows are the
      company's tree: the account node at depth 0, its children indented
      below it, programs nesting to any depth. The Label column reads the
      derived `ProjectState.label`; Workstream reads `state.display_name`.

    When `current_sprint_iso` is provided (e.g. "2026-W19"), each active
    project view dict gets a `sprint_link` pointing at the per-project
    sprint file under `sprints/<iso>/<code>.md`, and the active tables
    render an extra `[W## →]` cell next to the existing CP link. When
    None, no sprint link is rendered.
    """
    # One vocabulary for every workstream (#301): Deal ∪ Open is active,
    # `is_internal` gates nothing.
    def is_active(p: ProjectState) -> bool:
        return is_active_status(p.status)

    def is_holding(p: ProjectState) -> bool:
        return p.status == "Holding"

    ref_date = today or date.today()

    def is_closed_recent(p: ProjectState) -> bool:
        if p.last_touched is None:
            return False
        if (ref_date - p.last_touched.date()).days > 30:
            return False
        return p.status == "Closed"

    by_code = {p.code: p for p in projects}

    # Account nodes (#302) are workstreams but not jobs: they never sit in
    # the holding / closed-recent job tables. They are the roots of their
    # company's block in the tree region.
    def is_account(p: ProjectState) -> bool:
        return effective_label(p, by_code) == "account"

    active = [p for p in projects if is_active(p) and not is_account(p)]
    holding = [p for p in projects if is_holding(p) and not is_account(p)]
    closed_recent = [
        p for p in projects if is_closed_recent(p) and not is_account(p)
    ]

    # Pipeline (Deal status) keeps its own region, sorted by stage
    # progression; everything else active lands in the tree.
    _STAGE_ORDER = {"Inquiry": 0, "Negotiation": 1, "Contract": 2, "Won": 3, "Lost": 4}

    def to_view(p: ProjectState) -> dict:
        """Build view dict and attach allocation_line if allocations exist
        for this project. Per spec: skip the line entirely when total hours
        is zero (no allocations === no row).

        When `current_sprint_iso` is set, also attaches `sprint_link`
        pointing at `sprints/<iso>/<code>.md` for the per-project sprint
        file. None when no current sprint is in scope.
        """
        view = _project_view(p, by_code)
        view["allocation_line"] = _allocation_line_for(p.code, allocations)
        view["sprint_link"] = (
            f"sprints/{current_sprint_iso}/{p.code}.md"
            if current_sprint_iso
            else None
        )
        return view

    def group(active_list: list[ProjectState]) -> dict:
        # Pipeline keeps stage-progression sort (Inquiry → Negotiation →
        # Contract); deals are usually few enough that grouping by
        # account adds noise without value. Deal-stage rows never appear
        # in the tree — one row per workstream across the two regions.
        pipeline = sorted(
            (p for p in active_list if p.status == "Deal"),
            key=lambda p: (_STAGE_ORDER.get(p.deal_stage or "", 99), p.code),
        )
        return {"pipeline": [to_view(p) for p in pipeline]}

    def tree(active_list: list[ProjectState]) -> dict:
        """The `active-tree` view: companies in display order, each with
        its rows depth-first. A row is in the tree when it is active and
        not Deal-stage; an account node heads its block whenever it is
        active itself OR has a row below it."""
        in_tree = {p.code for p in active_list if p.status != "Deal"}
        for p in projects:
            if is_account(p) and is_active(p):
                in_tree.add(p.code)

        def _company_key(p: ProjectState) -> tuple:
            return (
                _COMPANY_KIND_ORDER.get(p.company_kind, 99),
                (p.company_name or company_slug(p.company_name)).lower(),
                p.company_name or "",
            )

        # Roots: nodes in the tree whose parent is not itself a tree row
        # (top-level, or under a parent that is held back / inactive), plus
        # account nodes that only head other rows.
        def _parent_in_tree(p: ProjectState) -> bool:
            return bool(p.parent_code) and p.parent_code in in_tree

        by_company: dict[tuple, list[ProjectState]] = {}
        for p in projects:
            if p.code not in in_tree:
                continue
            if _parent_in_tree(p):
                continue
            by_company.setdefault(_company_key(p), []).append(p)
        # An account node heads its company block even when inactive, as
        # long as something below it renders; add it as a root then.
        for p in projects:
            if not is_account(p) or p.code in in_tree:
                continue
            if any(c.code in in_tree for c in children_of(p.code, by_code)):
                in_tree.add(p.code)
                key = _company_key(p)
                by_company.setdefault(key, [])
                by_company[key] = [
                    r for r in by_company[key] if not (r.parent_code == p.code)
                ]
                by_company[key].append(p)

        def _rows(node: ProjectState, depth: int, out: list[dict]) -> None:
            view = to_view(node)
            view["depth"] = depth
            view["tree_prefix"] = _tree_prefix(depth)
            out.append(view)
            for child in children_of(node.code, by_code):
                if child.code in in_tree:
                    _rows(child, depth + 1, out)

        companies: list[dict] = []
        total = 0
        for key in sorted(by_company):
            roots = sorted(
                by_company[key],
                key=lambda p: (0 if is_account(p) else 1, p.code),
            )
            rows: list[dict] = []
            for root in roots:
                _rows(root, 0, rows)
            total += len(rows)
            companies.append({"name": key[2] or key[1], "rows": rows})
        return {"companies": companies, "count": total}

    # Derive the short week label ("W19") from the ISO week ("2026-W19")
    # so the template doesn't have to do string ops. Drops zero-padding
    # so week 1 reads "W1" rather than "W01".
    current_week_label: str | None = None
    if current_sprint_iso and "-W" in current_sprint_iso:
        try:
            week_num = int(current_sprint_iso.split("-W", 1)[1])
            current_week_label = f"W{week_num}"
        except ValueError:
            current_week_label = None

    today_or_now = today or date.today()
    agenda = (
        _compute_agenda_rollup(parsed_sprint_files, today_or_now)
        if parsed_sprint_files
        else None
    )
    # Phase D.6: surface the most recent Slack digest per project so
    # sprint planning + partners' review can see weekly Slack activity
    # without opening each sprint file individually. None when no
    # project has a digest this week (cron hasn't run, all channels
    # were quiet, etc.) so the template hides the section.
    slack_rollup = _compute_slack_rollup(
        config.root, active, current_sprint_iso, by_code
    )
    sprint_facts = (
        _compute_sprint_facts_strip(
            parsed_sprint_files, today_or_now, prior_sprint_iso
        )
        if parsed_sprint_files
        else None
    )

    grouped = group(active)
    active_tree = tree(active)
    section_summaries = {
        "pipeline": _section_summary(
            len(grouped["pipeline"]), _SECTION_STATE_PHRASES["pipeline"]
        ),
        "tree": _section_summary(active_tree["count"], _SECTION_STATE_PHRASES["tree"]),
    }

    template = _env().get_template("master-cp.md.j2")
    return template.render(
        tenant=config,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        last_sync_iso=last_sync.isoformat(),
        active_groups=grouped,
        active_tree=active_tree,
        section_summaries=section_summaries,
        workload_rollup=_rollup_view(allocations),
        workload_week=allocations.week_start if allocations else None,
        holding_projects=[_project_view(p, by_code) for p in holding],
        closed_recent=[_project_view(p, by_code) for p in closed_recent],
        exceptions_count=exceptions_count,
        current_week_label=current_week_label,
        agenda=agenda,
        sprint_facts=sprint_facts,
        slack_rollup=slack_rollup,
    )


def _tree_prefix(depth: int) -> str:
    """The indentation in the tree's Code cell: nothing at depth 0, a
    `└─ ` at depth 1, two non-breaking spaces more per level below that."""
    if depth <= 0:
        return ""
    return "&nbsp;&nbsp;" * (depth - 1) + "└─ "


def render_project_strip_bodies(project_strips: object | None) -> dict[str, str]:
    """Render the four project cp.md strip-region bodies as a dict.

    Returns ``{"inbound-strip": <body>, "recent-decisions-strip": <body>,
    "open-asks-strip": <body>, "stakeholders-strip": <body>}``. Each body is
    the *contents* between the engine markers (not including the markers
    themselves). Used by sync's splicer on every sync after the project
    cp.md is first scaffolded.

    Empty/None ``project_strips`` produces the same placeholder bodies the
    template emits on first scaffold — keeps the rendered output stable
    across "first sync with strips" vs. "subsequent sync with strips."
    """
    # Re-use the project-cp template's region blocks by rendering a tiny
    # ad-hoc template that mirrors the same markup. Simpler than parsing
    # the rendered project-cp.md to extract regions, and keeps the source
    # of truth singular: change the strip rendering in one place.
    bodies = _render_strip_template(_PROJECT_STRIPS_TEMPLATE, project_strips=project_strips)
    return bodies


def _render_strip_template(template_str: str, **context) -> dict[str, str]:
    """Shared helper: render an ad-hoc strip template and split by sentinel.

    Uses ``env.from_string`` with ``autoescape=False`` explicitly (the env's
    ``select_autoescape`` config only disables autoescape for *named*
    templates with `.md`/`.j2` extensions; ``from_string`` doesn't get that
    filename-based selection, and the default would HTML-escape apostrophes
    and ampersands in the output — wrong for markdown).
    """
    from jinja2 import Template
    template = Template(template_str, autoescape=False, keep_trailing_newline=True)
    rendered = template.render(**context)
    bodies: dict[str, str] = {}
    for chunk in rendered.split("===END==="):
        chunk = chunk.strip()
        if not chunk or "===START===" not in chunk:
            continue
        header, body = chunk.split("===START===", 1)
        region = header.strip()
        bodies[region] = body.strip()
    return bodies


# Ad-hoc template for the four project-cp strip bodies. Mirrors the markup
# in project-cp.md.j2's region bodies. Each region is delimited by
# `===START===` / `===END===` so render_project_strip_bodies can split.
_PROJECT_STRIPS_TEMPLATE = """\
inbound-strip===START===
## Recent inbound (auto-aggregated from sprint files, last 4 weeks)
{% if project_strips and project_strips.inbound %}
{% for ib in project_strips.inbound %}
- [{{ ib.date }} · {{ ib.who }}] {{ ib.text }}
{%- endfor %}
{%- else %}
- _No inbound captured in the last 4 weeks._
{%- endif %}
===END===
recent-decisions-strip===START===
## Recent decisions (auto-aggregated from sprint files, last 4 weeks)
{% if project_strips and project_strips.recent_decisions %}
{% for dec in project_strips.recent_decisions %}
- [{{ dec.date }}{% if dec.cross_cutting %} · cross-cutting{% endif %}] {{ dec.text }}
{%- endfor %}
{%- else %}
- _No structured decisions captured in the last 4 weeks._
{%- endif %}
===END===
open-asks-strip===START===
## Open client asks (auto-aggregated from sprint files)
{% if project_strips and project_strips.open_asks %}
{% for ask in project_strips.open_asks %}
- [{{ ask.asked_date }}{% if ask.who %} · {{ ask.who }}{% endif %}{% if ask.aged_days is not none and ask.aged_days > 7 %} · **{{ ask.aged_days }}d stale**{% endif %}] {{ ask.text }}
{%- endfor %}
{%- else %}
- _No open asks._
{%- endif %}
===END===
stakeholders-strip===START===
## Stakeholders (auto-aggregated from sprint files)
{% if project_strips and project_strips.stakeholders %}
{% for sh in project_strips.stakeholders %}
- **{{ sh.name }}**{% if sh.role %} — {{ sh.role }}{% endif %}{% if sh.context %} · _{{ sh.context }}_{% endif %}
{%- endfor %}
{%- else %}
- _No stakeholders captured yet._
{%- endif %}
===END===
"""


def render_sprint_week(
    *,
    week_iso: str,
    week_label: str,
    week_dates: str,
) -> str:
    """Render `sprints/<week>/_week.md` — week-scope handwritten notes.

    Holds tenant-wide content for the sprint week (themes, attendance,
    meta) that doesn't belong to any single project. Per-project sprint
    files hold project-specific content.

    This file is created once and never touched by sync afterward —
    handwritten content is sacred. The tenant rollup (`aggregators.
    aggregate_subtree_strips(None, …)`) reads this file's `## Themes`
    section to surface week themes at the tenant level.
    """
    template = _env().get_template("sprint-week.md.j2")
    return template.render(
        week_iso=week_iso,
        week_label=week_label,
        week_dates=week_dates,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
    )


def uses_initiative_shape(project: ProjectState) -> bool:
    """True for a workstream with no commercial envelope that is not under
    a client company — the internal-initiative shape.

    Since #303 no template is picked on this: every node renders through
    `project-cp.md.j2` / `sprint-cp.md.j2`, and the region set is derived
    per node (`project_cp_regions`). It remains the one rule the transcript
    prompt chooser (`plan_from_transcript`) reads. A client job whose
    `deal_stage` was never filled still counts as engagement-shaped — the
    client side is what the shape is about.
    """
    return not project.has_agreement and project.company_kind != "client"


# The engine-managed regions of a cp.md in template order. Every node
# carries the base set; `envelope-strip` is present iff the node has an
# agreement, `children` iff it has children in the roster (#303: the set
# is DERIVED per node, not per template).
PROJECT_CP_REGIONS: tuple[str, ...] = (
    "project-facts",
    "envelope-strip",
    "children",
    "current-sprint",
    "tracked-issues",
    "inbound-strip",
    "recent-decisions-strip",
    "open-asks-strip",
    "stakeholders-strip",
    "exec-summary",
)

# Regions earlier templates wrote into cp.md files that the unified
# template no longer carries: the account CP's two regions (their content
# folded into `project-facts` and `children`). Sync retires them on the
# next run; hand-written text around them is untouched.
PROJECT_CP_RETIRED_REGIONS: tuple[str, ...] = ("account-facts", "projects")


def project_cp_regions(
    project: ProjectState, by_code: "Mapping[str, ProjectState] | None" = None
) -> tuple[str, ...]:
    """The regions THIS node's cp.md carries, in template order."""
    roster = by_code or {}
    out: list[str] = []
    for region in PROJECT_CP_REGIONS:
        if region == "envelope-strip" and not project.has_agreement:
            continue
        if region == "children" and not children_of(project.code, roster):
            continue
        out.append(region)
    return tuple(out)


def render_project_cp(
    config: TenantConfig,
    project: ProjectState,
    tracked_issues: tuple[Issue, ...] = (),
    current_sprint_block: str | None = None,
    project_strips: object | None = None,
    by_code: "Mapping[str, ProjectState] | None" = None,
    envelope_flags: "list[dict] | None" = None,
) -> str:
    """Render a workstream's cp.md from the one template (#303).

    Used on first creation, and re-rendered on every sync so the engine
    regions (`project-facts`, `envelope-strip`, `children`) can be spliced
    from it. The region set is derived per node — see `project_cp_regions`.

    `by_code` is the roster: it resolves the node's children (the
    `children` region and the account Facts rows) and their links.
    `envelope_flags` is the open `workstream_flags` rows (each a dict with
    `project_id`, `parent_id`, `kind`, `excess`) when the caller could
    read them; None renders the flag row as "—" (unknown, not "none").

    `current_sprint_block` is the rendered "Current sprint" section
    (from `cp_engine.sprints.render_current_sprint_block`) for projects
    that have an active sprint file. Passed through to the template so
    the engine-managed `current-sprint` region is populated on first
    scaffold; on subsequent syncs the splicer rewrites it in place.
    Pass None when no sprint file exists yet — template emits a
    placeholder line.

    `project_strips` (Phase 1.2 / v0.8.5) is the aggregated content for
    the four new engine-managed regions (inbound, recent-decisions,
    open-asks, stakeholders). Type-as-object for late-binding to avoid
    a cp_engine.aggregators import cycle here; the template accesses
    attributes directly. Pass None on first scaffold (regions render
    empty placeholders); pass a ProjectStrips instance on subsequent
    syncs.
    """
    roster = by_code or {}
    template = _env().get_template("project-cp.md.j2")
    return template.render(
        tenant=config,
        project=_project_view(project, roster),
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        tracked_issues=[_issue_view(i) for i in tracked_issues],
        current_sprint_block=current_sprint_block,
        project_strips=project_strips,
        children=_children_rows(project, roster),
        envelope=(
            _envelope_view(project, roster, envelope_flags)
            if project.has_agreement
            else None
        ),
        account_facts=_account_facts(project, roster),
    )


def _relative_link(from_path: str, to_path: str) -> str:
    """`to_path/cp.md` relative to the dir `from_path` (both tenant-relative,
    POSIX). A child nested under the node needs no `../`; a child that is
    NOT nested (a re-parented job whose dir has not moved yet) still
    resolves."""
    src = [seg for seg in from_path.split("/") if seg]
    dst = [seg for seg in to_path.split("/") if seg]
    common = 0
    while common < len(src) and common < len(dst) and src[common] == dst[common]:
        common += 1
    up = [".."] * (len(src) - common)
    return "/".join(up + dst[common:] + ["cp.md"])


def _children_rows(
    project: ProjectState, roster: "Mapping[str, ProjectState]"
) -> list[dict]:
    """Rows for the `children` region: every non-Archived direct child,
    sorted by code, linked through `path_for` so grandchildren resolve."""
    here = path_for(project, roster)
    rows: list[dict] = []
    for child in children_of(project.code, roster):
        if child.status == "Archived":
            continue
        rows.append(
            {
                "code": child.code,
                "display_name": display_name(child, roster),
                "label_word": _LABEL_WORDS[effective_label(child, roster)],
                "status": child.deal_stage if child.status == "Deal" and child.deal_stage else child.status,
                "owner": child.owner,
                "last_touched_short": _short(child.last_touched),
                "cp_link": _relative_link(here, path_for(child, roster)),
            }
        )
    return rows


def _account_facts(
    project: ProjectState, roster: "Mapping[str, ProjectState]"
) -> dict:
    """The three account rows folded into `project-facts` for an account
    node (formerly the account CP's `account-facts` region): active
    descendants, their owners (first-seen order, deduped) and the newest
    `last_touched` below the node."""
    from cp_engine.state import descendants_of

    below = [
        p for p in descendants_of(project.code, roster)
        if is_active_status(p.status)
    ]
    owners: dict[str, None] = {}
    for p in below:
        if p.owner:
            owners.setdefault(p.owner, None)
    last = max((p.last_touched for p in below if p.last_touched), default=None)
    return {
        "active_count": len(below),
        "owners": ", ".join(owners) if owners else None,
        "last_activity_short": _short(last) if last else None,
    }


def _envelope_view(
    project: ProjectState,
    roster: "Mapping[str, ProjectState]",
    flags: "list[dict] | None",
) -> dict:
    """The `envelope-strip` rows for a node with an agreement (plan D7).

    Budget is the node's own; the children sum counts every non-Archived
    direct child with a budget (mirroring mc-2 mig 193's trigger, which
    checks one level); "without budget" counts the rest. The flag row
    reads the open `workstream_flags` rows: as PARENT (`parent_id` is this
    node) it names the flagged children; as CHILD (`project_id` is this
    node) it reports the breach of its parent's envelope. `flags is None`
    means the table could not be read — rendered "—", never "none".
    """
    kids = [
        c for c in children_of(project.code, roster) if c.status != "Archived"
    ]
    budgeted = [c for c in kids if c.budget is not None]
    without = len(kids) - len(budgeted)
    total = sum(c.budget for c in budgeted) if budgeted else None
    if not kids:
        children_sum_short = "—"
    else:
        children_sum_short = (
            f"{_format_budget(total) or '—'} across {len(budgeted)} of {len(kids)}"
        )
    if not kids:
        without_line = "—"
    elif without == 0:
        without_line = "none"
    else:
        without_line = f"{without} child{'ren' if without != 1 else ''} without budget"

    if flags is None:
        flag_line = "—"
    else:
        me = project.mc2_id
        by_id = {p.mc2_id: p for p in roster.values() if p.mc2_id}
        as_parent = [f for f in flags if me and f.get("parent_id") == me]
        as_child = [f for f in flags if me and f.get("project_id") == me]
        parts: list[str] = []
        if as_parent:
            excess = max(_as_float(f.get("excess")) for f in as_parent)
            names = sorted(
                (by_id[f["project_id"]].code if f.get("project_id") in by_id else str(f.get("project_id")))
                for f in as_parent
            )
            parts.append(
                f"⚠️ over envelope by {_format_budget(excess) or excess}: "
                + ", ".join(f"`{n}`" for n in names)
            )
        if as_child:
            excess = max(_as_float(f.get("excess")) for f in as_child)
            parts.append(
                f"⚠️ over parent envelope by {_format_budget(excess) or excess}"
            )
        flag_line = " · ".join(parts) if parts else "none"
    return {
        "budget_short": _format_budget(project.budget),
        "children_sum_short": children_sum_short,
        "children_without_budget_line": without_line,
        "flag_line": flag_line,
    }


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_GITIGNORE_BODY = """\
# Media — Dropbox is the convention for these
*.mp4
*.m4a
*.mov
*.wav
*.mp3
*.jpg
*.jpeg
*.png
*.gif
*.heic
*.tiff
*.pdf
*.pptx
*.ppt
*.docx
*.doc
*.xlsx
*.xls
*.zip
*.tar
*.tar.gz

# OS
.DS_Store
Thumbs.db

# Editors
*.swp
*.swo
.vscode/
.idea/

# Engine config
.cp-engine.local.toml

# Engine state — v0.8.7: holds fathom auto-poll state (last_polled_at,
# processed_ids). Per-machine; not shared via git. The path index
# (`paths.json`, cp-engine #302) IS shared: the webhook and the hosted
# server resolve working dirs from it.
.cp-engine/*
!.cp-engine/paths.json

# Bytecode cache from the engine-managed .claude/ hook script.
.claude/hooks/__pycache__/
"""


def render_gitignore() -> str:
    """Return the static `.gitignore` body for a v0.3 tenant.

    Pure static content — no template variables. Lives next to the other
    renderers so all generated files are reachable from one module.
    """
    return _GITIGNORE_BODY


def render_dropbox_md(project: ProjectState) -> str | None:
    """Render `_dropbox.md` for a project working directory.

    Returns None when the project has no Dropbox URL — the caller skips
    writing the file. Repos don't carry `dropbox_folder_url`; only
    engagements do, so this returns None for repo-source projects.
    """
    url = project.dropbox_folder_url
    if not url:
        return None
    template = _env().get_template("dropbox.md.j2")
    return template.render(
        project=_project_view(project),
        dropbox_url=url,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
    )


def render_linked_repo_md(
    project_name: str,
    repo: LinkedRepo,
    *,
    local_clones_by_user: dict[str, str] | None = None,
) -> str:
    """Render `_repo-<repo-name>.md` for a repo linked to a workstream.

    A workstream can have multiple linked repos in MC-2 (`repos.project_id`
    pointing at it). Each gets its own file in the working dir. The
    template makes the *linked* relationship explicit.
    """
    template = _env().get_template("linked-repo.md.j2")
    return template.render(
        project_name=project_name,
        repo=repo,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        local_clones_by_user=local_clones_by_user or None,
    )


def render_exceptions_readme(
    tenant_root: Path,
    *,
    now: datetime | None = None,
    days: int = 30,
) -> str:
    """Render the engine-managed `<tenant>/exceptions/README.md` body.

    The exceptions/ directory accumulates session captures from source repos
    that aren't tracked in this cp tenant. Most of the README is hand-written
    standing prose ("here's what this directory is for, register repos in
    MC-2 to graduate them out of here"). The splice region inside it lists
    the last `days` worth of exception files, newest first.

    Filename format expected: `<YYYY-MM-DD>-<repo-name>-<HHMM>-<user>.md`.
    Files that don't match are still listed but with a fallback rendering.

    The full README body is regenerated each call. The splicer is applied
    by sync.py to preserve any out-of-region hand-edits.
    """
    when = now or datetime.now()
    # Exception filenames parse to naive datetimes; an injected clock may be
    # tz-aware. Normalize to naive so the cutoff comparison stays valid.
    if when.tzinfo is not None:
        when = when.replace(tzinfo=None)
    cutoff = when - timedelta(days=days)

    exceptions_dir = tenant_root / "exceptions"
    entries = _collect_exception_entries(exceptions_dir, cutoff)

    list_body = _format_exceptions_list(entries) if entries else "_(none yet)_"

    return (
        "# Unregistered repo activity\n\n"
        "Session captures from source repos that aren't tracked in this cp\n"
        "tenant. Activity here is real work that's worth noticing — consider\n"
        "registering the repo in MC-2's `/repos` page so it gets a proper\n"
        "working directory next sync, or delete entries that aren't worth\n"
        "tracking long-term.\n\n"
        "## Recent\n\n"
        "<!-- cp-engine:start exceptions-list -->\n"
        f"{list_body}\n"
        "<!-- cp-engine:end exceptions-list -->\n"
    )


# Filename regex: <YYYY-MM-DD>-<repo>-<HHMM>-<user>(-N)?.md
_EXC_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<repo>[^/]+?)-"
    r"(?P<hhmm>\d{4})-"
    r"(?P<user>[^/]+?)"
    r"(?:-\d+)?\.md$"
)


@dataclass(frozen=True)
class _ExceptionEntry:
    path: Path
    when: datetime
    repo: str
    user: str


def _collect_exception_entries(
    exceptions_dir: Path, cutoff: datetime
) -> list[_ExceptionEntry]:
    """Return entries in exceptions_dir newer than `cutoff`, newest first.

    Falls back to file mtime if the filename doesn't match the expected
    `<YYYY-MM-DD>-<repo>-<HHMM>-<user>.md` shape; that's defensive
    handling for hand-renamed files.
    """
    if not exceptions_dir.exists():
        return []

    entries: list[_ExceptionEntry] = []
    for path in exceptions_dir.iterdir():
        if path.is_dir() or not path.name.endswith(".md"):
            continue
        if path.name == "README.md":
            continue
        match = _EXC_FILENAME_RE.match(path.name)
        if match:
            try:
                when = datetime.strptime(
                    f"{match['date']} {match['hhmm']}", "%Y-%m-%d %H%M"
                )
            except ValueError:
                when = datetime.fromtimestamp(path.stat().st_mtime)
            repo = match["repo"]
            user = match["user"]
        else:
            when = datetime.fromtimestamp(path.stat().st_mtime)
            repo = "?"
            user = "?"
        if when < cutoff:
            continue
        entries.append(_ExceptionEntry(path=path, when=when, repo=repo, user=user))

    entries.sort(key=lambda e: e.when, reverse=True)
    return entries


def _format_exceptions_list(entries: list[_ExceptionEntry]) -> str:
    """One-line markdown bullets per entry, newest first."""
    lines = []
    for e in entries:
        timestamp = e.when.strftime("%Y-%m-%d %H:%M")
        # Capitalize user for display: "drew" → "Drew"
        user_display = e.user.replace("-", " ").title()
        lines.append(
            f"- {timestamp} ({user_display}) — `{e.repo}` — "
            f"[`{e.path.name}`]({e.path.name})"
        )
    return "\n".join(lines)


def count_exceptions_in_window(
    tenant_root: Path, *, now: datetime | None = None, days: int = 7
) -> int:
    """Return the number of exceptions in the last `days` days. Used by
    master-cp.md to surface a small "Exceptions ({N} this week)" line.
    """
    when = now or datetime.now()
    if when.tzinfo is not None:
        when = when.replace(tzinfo=None)
    cutoff = when - timedelta(days=days)
    entries = _collect_exception_entries(tenant_root / "exceptions", cutoff)
    return len(entries)


def render_claude_md(config: TenantConfig) -> str:
    """Render CLAUDE.md from the tenant config.

    Encodes the four loading modes, gatekeeper rule, status vocabulary
    on the read side, and trigger-phrase reference. Fully generated —
    tenants never hand-edit (spec v02 §2.5).

    `has_sprint` is true when the tenant has a `canonic/sprint-cp.md` —
    today only `cp-canonic`. Inferred from tenant name; can be made
    explicit in `.cp-engine.toml` later if more tenants gain sprint CPs.
    """
    has_sprint = config.name == "canonic"
    example_code, example_name = _example_for(config)
    template = _env().get_template("CLAUDE.md.j2")
    return template.render(
        tenant=config,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        has_sprint=has_sprint,
        example_code=example_code,
        example_name=example_name,
    )


# ──────────────────────────────────────────────────────────────────────
#  Splice (the safety-critical function)
# ──────────────────────────────────────────────────────────────────────


EXEC_SUMMARY_REGION = "exec-summary"
EXEC_SUMMARY_START = f"<!-- cp-engine:start {EXEC_SUMMARY_REGION} -->"
EXEC_SUMMARY_END = f"<!-- cp-engine:end {EXEC_SUMMARY_REGION} -->"

# The migration seeds an Updates bullet `- <date> — migrated from Quick
# Resume`. This SUFFIX is the single source of truth for that wording,
# shared by the producer (sync's `_build_exec_summary_region`) and every
# reader that must treat a freshly-migrated-but-unauthored region as "no
# content". It lives here (not in sync) because render.py owns the region
# markers and is imported by sync/agenda/prep_planning/summary while
# importing none of them — the one place all copies can share without a
# circular import. `sync` re-exports it for backward compatibility.
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


# The six fields the wrap-up protocol authors, in the order they appear.
# `Last session:` is DERIVED (recomputed by capture-session / sync), so it is
# deliberately excluded — a derived field being filled says nothing about
# whether a human authored the state.
EXEC_SUMMARY_AUTHORED_FIELDS = (
    "Objective",
    "Status",
    "Where it stands",
    "Next up",
    "Blockers",
    "Updates",
)


def exec_summary_placeholder_fields(region: str) -> tuple[str, ...]:
    """The authored fields still carrying scaffold placeholders.

    ``exec_summary_is_authored`` answers "did a human touch this at all?" —
    ANY one real field makes it True. That is the right question for
    "should we show this region", but the wrong one for "can we trust its
    freshness stamp": a summary with one real field and five placeholders
    reads as authored, so its `· updated` stamp is taken at face value and
    the region renders as FRESH.

    This returns the field labels still unfilled, so callers can qualify the
    freshness verdict. Empty tuple = fully authored (or region absent).

    A field counts as a placeholder when its inline value is `_<...>_` AND
    the bullets beneath it (up to the next field or blank-line break) are all
    placeholder seeds or the auto-stamped migration bullet.
    """
    if not region:
        return ()
    filled: set[str] = set()
    seen: set[str] = set()
    current: str | None = None
    for raw in region.splitlines():
        line = raw.strip()
        if not line:
            continue
        field = re.match(r"^\*\*(?P<label>[^*]+?):\*\*\s*(?P<value>.*)$", line)
        if field is not None:
            label = field.group("label").strip()
            value = field.group("value").strip()
            if label in EXEC_SUMMARY_AUTHORED_FIELDS:
                seen.add(label)
                current = label
                if value and "_<" not in value:
                    filled.add(label)
            else:
                current = None
            continue
        if line.startswith("- ") and current is not None:
            if "_<" in line:
                continue
            if EXEC_SUMMARY_MIGRATION_BULLET_RE.match(line):
                continue
            filled.add(current)
    # Only report on fields the region actually declares — a region that
    # omits a field entirely is a different (structural) problem.
    return tuple(f for f in EXEC_SUMMARY_AUTHORED_FIELDS if f in seen and f not in filled)


def splice_managed_region(file_contents: str, region: str, new_body: str) -> str:
    """Replace the body between
    `<!-- cp-engine:start <region> -->` and the matching end marker.

    Markers themselves are preserved verbatim. Hand-written content
    outside the markers is preserved byte-for-byte.

    Raises:
        MarkerMissing: start or end marker for `region` isn't present
        MarkerDuplicated: more than one start or end marker for `region`
        MarkerInverted: end marker appears before start marker

    Args:
        file_contents: full file body, as a string
        region: region name (e.g. "tracked-issues", "active-table")
        new_body: replacement content. Should NOT include the marker
            lines themselves; they're preserved by the splicer.

    Returns:
        The spliced file body. Caller writes to disk.
    """
    start_marker = f"<!-- cp-engine:start {region} -->"
    end_marker = f"<!-- cp-engine:end {region} -->"

    # Find all occurrences. We require exactly one of each.
    start_positions = [m.start() for m in re.finditer(re.escape(start_marker), file_contents)]
    end_positions = [m.start() for m in re.finditer(re.escape(end_marker), file_contents)]

    if len(start_positions) == 0:
        raise MarkerMissing(f"No start marker for region {region!r}: expected {start_marker!r}")
    if len(end_positions) == 0:
        raise MarkerMissing(f"No end marker for region {region!r}: expected {end_marker!r}")
    if len(start_positions) > 1:
        raise MarkerDuplicated(
            f"Found {len(start_positions)} start markers for region {region!r}; expected exactly 1"
        )
    if len(end_positions) > 1:
        raise MarkerDuplicated(
            f"Found {len(end_positions)} end markers for region {region!r}; expected exactly 1"
        )

    start_pos = start_positions[0]
    end_pos = end_positions[0]

    if end_pos < start_pos:
        raise MarkerInverted(
            f"End marker for region {region!r} appears before start marker"
        )

    # Splice: keep [up to and including start_marker + newline], inject new_body
    # with newlines at boundaries, then [end_marker onwards].
    before = file_contents[: start_pos + len(start_marker)]
    after = file_contents[end_pos:]

    # Normalize: ensure exactly one newline between start_marker and new_body,
    # and exactly one newline between new_body and end_marker. When body is
    # empty after stripping, just emit a single newline between markers
    # (matches what a Jinja template with an empty `{% if %}` block produces
    # on full-write, so splice and full-write agree byte-for-byte).
    body = new_body.strip("\n")
    if body:
        return f"{before}\n{body}\n{after}"
    return f"{before}\n{after}"


# ──────────────────────────────────────────────────────────────────────
#  Region-set migration (#303): ensure / retire regions in place
# ──────────────────────────────────────────────────────────────────────


def _region_block_re(region: str) -> "re.Pattern[str]":
    """The whole marker block of `region` — start marker line through end
    marker line (inclusive of its newline when present) — plus at most ONE
    following blank line, so removing a block does not leave a double gap."""
    esc = re.escape(region)
    return re.compile(
        rf"^<!-- cp-engine:start {esc} -->\n.*?^<!-- cp-engine:end {esc} -->(?:\n)?(?:\n)?",
        re.S | re.M,
    )


def region_block(rendered: str, region: str) -> str:
    """The marker block for `region` from a freshly rendered full body —
    markers included, trailing blank line NOT included. ValueError when the
    rendered body has no such region."""
    start_marker = f"<!-- cp-engine:start {region} -->"
    end_marker = f"<!-- cp-engine:end {region} -->"
    start = rendered.find(start_marker)
    end = rendered.find(end_marker, start if start >= 0 else 0)
    if start < 0 or end < 0:
        raise ValueError(f"rendered body carries no region {region!r}")
    return rendered[start : end + len(end_marker)]


def has_region(body: str, region: str) -> bool:
    return f"<!-- cp-engine:start {region} -->" in body


def retire_regions(body: str, regions: "tuple[str, ...]") -> str:
    """Remove every listed region — markers AND body — from `body`. Text
    outside the markers is untouched byte-for-byte; the one blank line
    that followed a removed block goes with it."""
    out = body
    for region in regions:
        if not has_region(out, region):
            continue
        out = _region_block_re(region).sub("", out, count=1)
    return out


def ensure_regions(
    body: str,
    rendered: str,
    regions: "tuple[str, ...]",
    *,
    fallback_pos: int | None = None,
) -> str:
    """Insert every region of `regions` that `body` lacks, at the position
    the template gives it, with the block `rendered` carries for it.

    `regions` is the wanted set in TEMPLATE order. A missing region lands
    after the end marker of the nearest preceding wanted region that IS in
    the file; failing that, before the start marker of the nearest
    following one; failing that, at `fallback_pos` (where a retired region
    used to sit — see `migrate_regions`) or at the end of the file.
    Hand-written text is never moved or rewritten — only marker blocks are
    inserted, each separated from its neighbours by one blank line.
    Idempotent: a body carrying every region is returned unchanged.
    """
    out = body
    for idx, region in enumerate(regions):
        if has_region(out, region):
            continue
        try:
            block = region_block(rendered, region)
        except ValueError:
            continue
        # Nearest preceding present region → insert after its end marker.
        anchor_after = None
        for prev in reversed(regions[:idx]):
            if has_region(out, prev):
                anchor_after = f"<!-- cp-engine:end {prev} -->"
                break
        if anchor_after is not None:
            pos = out.index(anchor_after) + len(anchor_after)
            out = out[:pos] + "\n\n" + block + out[pos:]
            continue
        anchor_before = None
        for nxt in regions[idx + 1 :]:
            if has_region(out, nxt):
                anchor_before = f"<!-- cp-engine:start {nxt} -->"
                break
        if anchor_before is not None:
            pos = out.index(anchor_before)
            out = out[:pos] + block + "\n\n" + out[pos:]
            continue
        if fallback_pos is not None and 0 <= fallback_pos <= len(out):
            pos = fallback_pos
            out = out[:pos] + block + "\n\n" + out[pos:]
            continue
        if not out.endswith("\n"):
            out += "\n"
        out = out + "\n" + block + "\n"
    return out


def migrate_regions(
    body: str,
    rendered: str,
    wanted: "tuple[str, ...]",
    retired: "tuple[str, ...]" = (),
) -> str:
    """Retire `retired`, then ensure `wanted` (template order) — the one
    pass sync runs on an existing engine file whose region set has moved
    under it (an account CP scaffolded before #303, an initiative CP
    without `tracked-issues`, a master-cp with the five old active tables).

    When the file carries NONE of the wanted regions (the old account CP:
    only `account-facts` + `projects`), the new set lands where the first
    retired region stood — above the hand-written sections, as the
    template lays it out — rather than at the end of the file.
    """
    first_retired: int | None = None
    for region in retired:
        marker = f"<!-- cp-engine:start {region} -->"
        pos = body.find(marker)
        if pos >= 0 and (first_retired is None or pos < first_retired):
            first_retired = pos
    stripped = retire_regions(body, retired)
    fallback: int | None = None
    if first_retired is not None and not any(has_region(stripped, r) for r in wanted):
        # Retiring removed text BEFORE `first_retired` only if a retired
        # block sat earlier — impossible, `first_retired` is the earliest.
        fallback = min(first_retired, len(stripped))
    return ensure_regions(stripped, rendered, wanted, fallback_pos=fallback)


# ──────────────────────────────────────────────────────────────────────
#  Agenda rollup
# ──────────────────────────────────────────────────────────────────────


# Match either "by W##" or bare "W##" (case-insensitive on the leading
# `by`). Used to detect horizon target_dates that we can compare against
# the current sprint week numerically.
# NB: The agenda week-target regex moved to cp_engine.aggregators in v0.8.5
# (shared between master-cp.md's `agenda` region and the parent-node
# sprint rollups). This file no longer needs it directly.


_SLACK_DIGEST_RE = re.compile(
    r"^- \[(?P<week>\d{4}-W\d{1,2}) · Slack\] (?P<text>.+?)(?: <!-- cp:hash=[a-f0-9]+ -->)?\s*$",
    re.MULTILINE,
)


def _compute_slack_rollup(
    tenant_root: Path,
    active_projects: list[ProjectState],
    current_sprint_iso: str | None,
    by_code: "Mapping[str, ProjectState] | None" = None,
) -> list[dict] | None:
    """Aggregate the most recent Slack digest bullet across active projects.

    Reads each active project's current sprint file and extracts the
    last bullet matching `- [<W##> · Slack] <text>`. Returns one row
    per project that has a digest this week — projects with quiet
    weeks (no Slack activity, digest cron skipped them) don't appear.

    Returns None when nothing matches so the template can hide the
    section entirely.
    """
    if not current_sprint_iso:
        return None

    sprints_dir = tenant_root / "sprints" / current_sprint_iso
    if not sprints_dir.is_dir():
        return None

    rows: list[dict] = []
    for p in active_projects:
        sprint_file = sprints_dir / f"{p.code}.md"
        if not sprint_file.is_file():
            continue
        body = sprint_file.read_text(encoding="utf-8")
        matches = list(_SLACK_DIGEST_RE.finditer(body))
        if not matches:
            continue
        # Take the last match — if the digest cron ever runs twice for
        # the same week, the newer bullet is at the bottom (append-only).
        m = matches[-1]
        rows.append({
            "code": p.code,
            "name": p.name,
            # Path-building parent + dir name so the master-cp link
            # `<scope>/<dir_slug>/cp.md` resolves (#302).
            "scope": parent_path_for(p, by_code or {}),
            "dir_slug": dir_name_for(p, by_code or {}),
            "week": m.group("week"),
            "text": m.group("text").strip(),
            "sprint_link": f"sprints/{current_sprint_iso}/{p.code}.md",
        })

    if not rows:
        return None
    # Sort by company_code (groups projects from same client together) for
    # readability; matches the rest of master-cp's grouping convention.
    rows.sort(key=lambda r: (r["scope"], r["code"]))
    return rows


def _compute_agenda_rollup(
    parsed_sprint_files: tuple[SprintFile, ...] | None,
    today: date,
) -> dict | None:
    """Aggregate cross-project agenda items from parsed sprint files.

    Three lists, each filtered + tagged with `project_code` so the master
    CP can render them in one block:

    - `escalated_risks`: every risk with `severity == "escalated"`. Order:
      preserved within each sprint file; sprint files iterated in source
      order.
    - `stale_asks`: every open client ask whose `asked_date` is more than
      7 days before `today`. Each entry includes `aged_days` for display.
      Asks with unparseable date strings are skipped (they'd produce
      misleading age values).
    - `decisions_due`: every horizon item with `bucket == "decision"`
      whose `target_date` parses as a `W##` (or `by W##`) week within +2
      sprints of `today`'s sprint week. Items whose target_date doesn't
      match the week pattern (e.g. "TBD", a literal date) pass through
      unconditionally — better to over-surface than to silently drop.

    Returns a dict with the three lists, OR `None` when all three are
    empty (so the template's `{%- if agenda %}` guard hides the section).
    """
    # Delegate the core rollup to cp_engine.aggregators so the subtree
    # rollups (#303) can reuse the same parser.
    # This function keeps the master-cp-specific behavior of returning None
    # when all three lists are empty (so the template's `{%- if agenda %}`
    # guard hides the section).
    if not parsed_sprint_files:
        return None
    from cp_engine.aggregators import carry_forward_rollup
    rollup = carry_forward_rollup(parsed_sprint_files, today)
    if not (rollup["escalated_risks"] or rollup["stale_asks"] or rollup["decisions_due"]):
        return None
    return rollup


# ──────────────────────────────────────────────────────────────────────
#  Sprint facts strip
# ──────────────────────────────────────────────────────────────────────


def _compute_sprint_facts_strip(
    parsed_sprint_files: tuple[SprintFile, ...],
    today: date,
    prior_sprint_iso: str | None,
) -> dict | None:
    """Aggregate sprint-wide facts across all parsed sprint files.

    Eight fields surfaced at the top of master-cp.md as a one-line strip:
    total hours summed across all sprint files, per-person totals (sorted
    by hours desc), count of active sprint files this week, stale-asks
    count (>7d old), escalated risks count, decisions-due count (matching
    the agenda rollup's filter — next 2 sprints), and the prior sprint
    ISO week (rendered as plain text in v0.8.0; no master archive to link
    to yet).

    Returns `None` when `parsed_sprint_files` is empty so the template
    can hide the strip. The stale/escalated/decisions filters reuse
    `_compute_agenda_rollup` to keep the two surfaces in lockstep.
    """
    if not parsed_sprint_files:
        return None

    # Per-person totals across all sprint files. dict preserves insertion
    # order; we re-sort at the end by hours desc for stable rendering.
    by_person: dict[str, float] = {}
    for sf in parsed_sprint_files:
        for ph in sf.allocation:
            by_person[ph.person_name] = by_person.get(ph.person_name, 0.0) + ph.hours
    per_person = sorted(by_person.items(), key=lambda kv: (-kv[1], kv[0]))
    # Render hours as ints when whole — matches the test's "**Drew** 10" shape.
    per_person_view = [
        (name, int(hours) if hours == int(hours) else hours)
        for name, hours in per_person
    ]
    total = sum(by_person.values())
    total_view = int(total) if total == int(total) else total

    # Reuse the agenda rollup to count escalated risks, stale asks, and
    # decisions due. When the rollup returns None (all three lists
    # empty), the counts are all zero.
    rollup = _compute_agenda_rollup(parsed_sprint_files, today)
    if rollup is None:
        escalated_count = 0
        stale_asks_count = 0
        decisions_due_count = 0
    else:
        escalated_count = len(rollup["escalated_risks"])
        stale_asks_count = len(rollup["stale_asks"])
        decisions_due_count = len(rollup["decisions_due"])

    return {
        "total_hours": total_view,
        "per_person": per_person_view,
        "active_count": len(parsed_sprint_files),
        "stale_asks_count": stale_asks_count,
        "escalated_count": escalated_count,
        "decisions_due_count": decisions_due_count,
        "prior_sprint": prior_sprint_iso,
    }


# ──────────────────────────────────────────────────────────────────────
#  Internal view-model helpers
# ──────────────────────────────────────────────────────────────────────


def _project_view(
    p: ProjectState, by_code: "Mapping[str, ProjectState] | None" = None
) -> dict:
    """Flatten a ProjectState into the keys the templates expect.

    Includes both engagement-shape and repo-shape fields. Templates
    branch on `engagement_shape` (has an agreement, or under a client
    company) to choose which to render (#301).

    `by_code` is the roster `state.path_for` resolves parents from (#302);
    without it a program child renders with its pre-#302 link, which is why
    every multi-project renderer passes it.
    """
    roster = by_code or {}
    label = effective_label(p, roster)
    # Account view fields — populated for client projects so the
    # master-cp pipeline table can render the leading Account column.
    # account_link is a literal relative path from the tenant root to
    # the account cp.md (`1p/<slug>/cp.md`). For non-client projects
    # both are None and the template's account column simply renders
    # empty if it ever lands there.
    if p.company_kind == "client":
        account_slug_value = company_slug(p.company_name)
        account_display = p.company_name or account_slug_value
        account_link = f"1p/{account_slug_value}/cp.md"
    else:
        account_slug_value = None
        account_display = None
        account_link = None
    return {
        "code": p.code,
        "name": p.name,
        # MC-2 row uuid — stamped into cp.md frontmatter so dir-location
        # can anchor on the stable id instead of the (renameable) code.
        "mc2_id": p.mc2_id,
        # Workstream shape (#301/#303). Templates branch on `has_agreement`,
        # `label` and `company_kind` directly; nothing picks a template.
        "has_agreement": p.has_agreement,
        "parent_code": p.parent_code,
        "label": label,
        "label_word": _LABEL_WORDS[label],
        "cp_kind": _CP_KIND_WORDS[label],
        "display_name": display_name(p, roster),
        "depth": tree_depth(p, roster),
        "company_kind": p.company_kind,
        # Path-building parent + dir name (#302: the tree is recursive).
        # Templates render `{{ p.scope }}/{{ p.dir_slug }}/cp.md` links.
        "scope": parent_path_for(p, roster),
        "dir_slug": dir_name_for(p, roster),
        "company_code": p.company_code,
        "company_name": p.company_name,
        "account_slug": account_slug_value,
        "account_display": account_display,
        "account_link": account_link,
        "status": p.status,
        "owner": p.owner,
        "last_touched_short": _short(p.last_touched),
        # When the WORK last moved, from the newest human-dated sprint bullet
        # (#260). `last_touched_short` above dates the job RECORD and is kept
        # for the surfaces that mean that. "—" when no dated bullet exists:
        # showing no signal beats inventing one.
        "last_activity_short": (
            p.last_activity.isoformat() if p.last_activity else "—"
        ),
        "one_line_summary": p.one_line_summary,
        # None when current/undatable; an int (days) when the hand-written
        # Exec Summary this one-liner comes from trails real activity.
        "summary_stale_days": p.summary_stale_days,
        # (text, ISO date) of the newest decision since the summary was
        # written — shown beside a stale summary, never in place of it.
        "latest_signal": p.latest_signal,
        # Engagement-only
        "deal_stage": p.deal_stage,
        "budget": p.budget,
        "budget_short": _format_budget(p.budget),
        "dropbox_folder_url": p.dropbox_folder_url,
        # Repo-only
        "github_org": p.github_org,
        "repo_name": p.repo_name,
        "description": p.description,
    }


def _format_budget(b: float | None) -> str | None:
    """Compact budget for table rendering. None → None (template renders —)."""
    if b is None:
        return None
    if b >= 1000:
        return f"${b / 1000:.0f}k"
    return f"${b:.0f}"


def _format_hours(h: float) -> str:
    """Render hours compactly. 8.0 → '8h'; 7.5 → '7.5h'."""
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:g}h"


def _allocation_line_for(code: str, allocations) -> str | None:
    """Build the per-project allocation line, or None to suppress the row.

    Returns format: "Last week: Tony 4h, Marcello 8h (12h total)."
    Skips entirely (returns None) when:
    - allocations is None (no data fetched)
    - project has no allocation entry
    - total hours is zero
    """
    if allocations is None:
        return None
    alloc = allocations.by_project.get(code)
    if alloc is None or alloc.total_hours == 0:
        return None
    parts = ", ".join(
        f"{e.person_name.split()[0]} {_format_hours(e.hours)}" for e in alloc.entries
    )
    return f"Last week: {parts} ({_format_hours(alloc.total_hours)} total)."


def _rollup_view(allocations) -> list[dict]:
    """Per-person workload rollup as a list of dicts for the template."""
    if allocations is None:
        return []
    return [
        {
            "person_name": r.person_name,
            "total_hours_short": _format_hours(r.total_hours),
            "engagement_hours_short": _format_hours(r.engagement_hours)
                if r.engagement_hours
                else "—",
            "engagement_project_count": r.engagement_project_count,
            "internal_hours_short": _format_hours(r.internal_hours)
                if r.internal_hours
                else "—",
        }
        for r in allocations.rollup
    ]


def _issue_view(i: Issue) -> dict:
    return {
        "number": i.number,
        "title": i.title,
        "status": i.status,
        "owner": i.owner,
        "updated_short": _short(i.updated),
    }


def _example_for(config: TenantConfig) -> tuple[str, str]:
    """Pick a representative project for the CLAUDE.md "always use code+name"
    section. Falls back to a generic example if the tenant has no projects yet.
    """
    from cp_engine.codes import parse_code
    from cp_engine.state import short_code

    if config.projects:
        first = config.projects[0]
        parsed = parse_code(first.code)
        name = parsed.slug.replace("-", " ").title() if parsed and parsed.slug else "Activation"
        return (short_code(first.code), name)
    return ("ggl-5168", "Activation")
