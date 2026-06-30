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

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cp_engine import __version__ as ENGINE_VERSION
from cp_engine.config import TenantConfig
from cp_engine.state import (
    Issue,
    LinkedRepo,
    ProjectState,
    SprintFile,
    account_scope_for,
    company_slug,
    dir_slug,
    scope_for,
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
    "client": "engagements in delivery",
    "self_fpsf": "tools in active build",
    "self_canonic": "projects in flight",
    "self_fpsf_initiatives": "initiatives in motion",
    "self_canonic_initiatives": "initiatives in motion",
}

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

    v0.2: groups entries by `companies.kind` into three sections:
    - 1P (client engagements + repos owned by client companies)
    - First Person (self-fpsf repos)
    - Canonic (self-canonic repos)

    Engagement and repo entries use different table schemas (per spec
    discussion 2026-05-08). The 1P section is engagement-shaped with
    Stage / Budget; FPSF and Canonic sections are repo-shaped with
    Description / GitHub.

    When `current_sprint_iso` is provided (e.g. "2026-W19"), each active
    project view dict gets a `sprint_link` pointing at the per-project
    sprint file under `sprints/<iso>/<code>.md`, and the active tables
    render an extra `[W## →]` cell next to the existing CP link. When
    None, no sprint link is rendered.
    """
    # Active filter: engagement is active per is_active_status + not internal;
    # repo is active per repos.status == 'Active'.
    def is_active(p: ProjectState) -> bool:
        if p.source == "engagement":
            return is_active_status(p.status) and not p.is_internal
        return p.status == "Active"

    def is_holding(p: ProjectState) -> bool:
        # Both engagement and repo use literal "Holding"
        if p.source == "engagement":
            return p.status == "Holding" and not p.is_internal
        return p.status == "Holding"

    ref_date = today or date.today()

    def is_closed_recent(p: ProjectState) -> bool:
        if p.last_touched is None:
            return False
        if (ref_date - p.last_touched.date()).days > 30:
            return False
        if p.source == "engagement":
            return p.status == "Closed" and not p.is_internal
        # Repos don't have a "Closed" lifecycle; Inactive doesn't get
        # surfaced in closed-recent (no notion of recency for inactive).
        return False

    active = [p for p in projects if is_active(p)]
    holding = [p for p in projects if is_holding(p)]
    closed_recent = [p for p in projects if is_closed_recent(p)]

    # Group active entries by company_kind. 1P bucket gets a sub-split
    # into Pipeline (Deal status) and Active Engagements (Open status),
    # with Pipeline sorted by stage progression.
    _STAGE_ORDER = {"Inquiry": 0, "Negotiation": 1, "Contract": 2, "Won": 3, "Lost": 4}

    def to_view(p: ProjectState) -> dict:
        """Build view dict and attach allocation_line if allocations exist
        for this project. Per spec: skip the line entirely when total hours
        is zero (no allocations === no row).

        When `current_sprint_iso` is set, also attaches `sprint_link`
        pointing at `sprints/<iso>/<code>.md` for the per-project sprint
        file. None when no current sprint is in scope.
        """
        view = _project_view(p)
        view["allocation_line"] = _allocation_line_for(p.code, allocations)
        view["sprint_link"] = (
            f"sprints/{current_sprint_iso}/{p.code}.md"
            if current_sprint_iso
            else None
        )
        return view

    def group(active_list: list[ProjectState]) -> dict:
        client_entries = [p for p in active_list if p.company_kind == "client"]
        # Pipeline keeps stage-progression sort (Inquiry → Negotiation →
        # Contract); deals are usually few enough that grouping by
        # account adds noise without value.
        pipeline = sorted(
            (p for p in client_entries if p.status == "Deal"),
            key=lambda p: (_STAGE_ORDER.get(p.deal_stage or "", 99), p.code),
        )
        # Active client engagements sort by (account_slug, code) so
        # projects from the same account cluster — matches the new
        # account-nested layout's grouping intent.
        client_active = sorted(
            (p for p in client_entries if p.status == "Open"),
            key=lambda p: (
                company_slug(p.company_name),
                p.code,
            ),
        )
        # Initiatives split from repos so the FPSF/Canonic sections stay
        # repo-shaped (Description / GitHub columns). Initiatives render
        # into a sibling table per scope with initiative-shaped columns.
        def _self_repos(kind: str) -> list[ProjectState]:
            return [
                p for p in active_list
                if p.company_kind == kind and p.source != "initiative"
            ]
        def _self_initiatives(kind: str) -> list[ProjectState]:
            return [
                p for p in active_list
                if p.company_kind == kind and p.source == "initiative"
            ]
        return {
            "pipeline": [to_view(p) for p in pipeline],
            "client": [to_view(p) for p in client_active],
            "self_fpsf": [to_view(p) for p in _self_repos("self-fpsf")],
            "self_canonic": [to_view(p) for p in _self_repos("self-canonic")],
            "self_fpsf_initiatives": [
                to_view(p) for p in _self_initiatives("self-fpsf")
            ],
            "self_canonic_initiatives": [
                to_view(p) for p in _self_initiatives("self-canonic")
            ],
        }

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
        config.root, active, current_sprint_iso
    )
    sprint_facts = (
        _compute_sprint_facts_strip(
            parsed_sprint_files, today_or_now, prior_sprint_iso
        )
        if parsed_sprint_files
        else None
    )

    grouped = group(active)
    section_summaries = {
        key: _section_summary(len(grouped[key]), phrase)
        for key, phrase in _SECTION_STATE_PHRASES.items()
    }

    template = _env().get_template("master-cp.md.j2")
    return template.render(
        tenant=config,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        last_sync_iso=last_sync.isoformat(),
        active_groups=grouped,
        section_summaries=section_summaries,
        workload_rollup=_rollup_view(allocations),
        workload_week=allocations.week_start if allocations else None,
        holding_projects=[_project_view(p) for p in holding],
        closed_recent=[_project_view(p) for p in closed_recent],
        exceptions_count=exceptions_count,
        current_week_label=current_week_label,
        agenda=agenda,
        sprint_facts=sprint_facts,
        slack_rollup=slack_rollup,
    )


def render_weekly_cp(config: TenantConfig) -> str:
    """Render the weekly-cp.md body (skeleton for hand-editing).

    Sync never touches this file; this renderer is used only on first
    creation or on `cp render --force-weekly`.
    """
    template = _env().get_template("weekly-cp.md.j2")
    return template.render(
        tenant=config,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
    )


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


def render_weekly_strip_bodies(tenant_strips: object | None) -> dict[str, str]:
    """Render the three weekly-cp.md strip-region bodies as a dict.

    Returns ``{"themes-strip": <body>, "decisions-strip": <body>,
    "carry-forward-strip": <body>}``. Used by sync to splice these
    regions into weekly-cp.md on every sync after the file is first
    scaffolded.

    Pass None / empty TenantStrips → renders placeholder bodies matching
    the first-scaffold template (keeps "first sync" vs. "subsequent sync"
    output stable).
    """
    return _render_strip_template(_WEEKLY_STRIPS_TEMPLATE, tenant_strips=tenant_strips)


_WEEKLY_STRIPS_TEMPLATE = """\
themes-strip===START===
## Themes (auto-aggregated from sprints/<W##>/_week.md, last 2 weeks)
{% if tenant_strips and tenant_strips.themes %}
{% for t in tenant_strips.themes %}
- [{{ t.date }}] {{ t.text }}
{%- endfor %}
{%- else %}
- _No themes captured in the last 2 weeks._
{%- endif %}
===END===
decisions-strip===START===
## Decisions (cross-cutting, auto-aggregated from sprint files, last 4 weeks)
{% if tenant_strips and tenant_strips.cross_cutting_decisions %}
{% for d in tenant_strips.cross_cutting_decisions %}
- [{{ d.date }} · `{{ d.project_code }}`] {{ d.text }}
{%- endfor %}
{%- else %}
- _No cross-cutting decisions captured in the last 4 weeks._
{%- endif %}
===END===
carry-forward-strip===START===
## Carry-forward across the tenant (auto-aggregated)
{% if tenant_strips and (tenant_strips.carry_forward.escalated_risks or tenant_strips.carry_forward.stale_asks or tenant_strips.carry_forward.decisions_due) %}
{% if tenant_strips.carry_forward.escalated_risks %}
**Escalated risks**
{% for r in tenant_strips.carry_forward.escalated_risks %}
- `{{ r.project_code }}`: {{ r.text }}
{%- endfor %}
{% endif %}
{% if tenant_strips.carry_forward.stale_asks %}
**Open asks aged > 7 days**
{% for a in tenant_strips.carry_forward.stale_asks %}
- `{{ a.project_code }}` ({{ a.aged_days }}d): {{ a.text }}
{%- endfor %}
{% endif %}
{% if tenant_strips.carry_forward.decisions_due %}
**Decisions due (next +2 sprints)**
{% for d in tenant_strips.carry_forward.decisions_due %}
- `{{ d.project_code }}`{% if d.target_date %} ({{ d.target_date }}){% endif %}: {{ d.text }}
{%- endfor %}
{% endif %}
{%- else %}
- _Nothing to carry forward._
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

    Like `weekly-cp.md`, this file is created once and never touched by
    sync afterward — handwritten content is sacred. The `weekly-cp.md`
    `themes-strip` engine-managed region reads from this file's
    `## Themes` section to surface week themes at the tenant level.
    """
    template = _env().get_template("sprint-week.md.j2")
    return template.render(
        week_iso=week_iso,
        week_label=week_label,
        week_dates=week_dates,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
    )


def render_project_cp(
    config: TenantConfig,
    project: ProjectState,
    tracked_issues: tuple[Issue, ...] = (),
    current_sprint_block: str | None = None,
    project_strips: object | None = None,
) -> str:
    """Render a project CP from the empty template.

    Only used on first creation. Subsequent updates touch only the
    engine-managed regions via splice_managed_region.

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
    # Initiatives use a slimmer template than engagements — no client
    # communication surfaces, no tracked-issues table, just decisions
    # + open asks + the standard sprint/notes structure. See
    # docs/plans/2026-05-14-internal-initiatives.md.
    template_name = (
        "initiative-cp.md.j2" if project.source == "initiative" else "project-cp.md.j2"
    )
    template = _env().get_template(template_name)
    return template.render(
        tenant=config,
        project=_project_view(project),
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        tracked_issues=[_issue_view(i) for i in tracked_issues],
        current_sprint_block=current_sprint_block,
        project_strips=project_strips,
    )


# ──────────────────────────────────────────────────────────────────────
#  Account CP renderers (v0.8.17+, 1p-only)
# ──────────────────────────────────────────────────────────────────────


def render_account_facts_body(
    account_display: str,
    projects_in_account: tuple[ProjectState, ...],
) -> str:
    """Body for the `account-facts` engine-managed region.

    Small at-a-glance table mirroring the project-cp `Facts` table's
    shape. Built from the projects already in scope, not a separate
    backend query — `account_display` and the active-project count are
    derivable from `projects_in_account` alone.

    Returns the inner body (no marker lines) so the caller can either
    pass it to the template on first scaffold or hand it to
    `splice_managed_region` on subsequent syncs.
    """
    active_count = len(projects_in_account)
    last_touched = max(
        (p.last_touched for p in projects_in_account if p.last_touched),
        default=None,
    )
    last_touched_short = _short(last_touched) if last_touched else "—"
    # Owners aggregate the per-project owner field. None is filtered;
    # duplicates collapse; the order is stable (first-seen) so re-renders
    # are byte-stable when the project set is unchanged.
    seen: dict[str, None] = {}
    for p in projects_in_account:
        if p.owner:
            seen.setdefault(p.owner, None)
    owners_str = ", ".join(seen) if seen else "—"
    return (
        f"## Facts\n\n"
        f"| | |\n"
        f"|---|---|\n"
        f"| **Account** | {account_display} |\n"
        f"| **Active projects** | {active_count} |\n"
        f"| **Owners** | {owners_str} |\n"
        f"| **Last project activity** | {last_touched_short} |\n"
    )


def render_account_projects_body(
    projects_in_account: tuple[ProjectState, ...],
) -> str:
    """Body for the `projects` engine-managed region.

    Lists every project under the account with code → linked dir_slug.
    Projects live one level under their account on disk, so links are
    literal relative paths (`<dir_slug>/cp.md`) — no `../` walk.

    Empty list is allowed (e.g. an account where every project just
    drained); the caller still calls this so the region stays in sync
    rather than leaving stale content from a prior sync.
    """
    if not projects_in_account:
        return "## Projects\n\n_No active projects._\n"
    rows = []
    # Stable order: by code so re-renders are byte-stable.
    for p in sorted(projects_in_account, key=lambda x: x.code):
        slug = dir_slug(p.code, p.name)
        stage_or_status = p.deal_stage or p.status
        owner = p.owner or "—"
        last_touched_short = _short(p.last_touched) if p.last_touched else "—"
        rows.append(
            f"| [`{p.code}`]({slug}/cp.md) | {p.name} | {stage_or_status} | "
            f"{owner} | {last_touched_short} |"
        )
    table = (
        "## Projects\n\n"
        "### Active\n"
        "| Code | Project | Stage | Owner | Last touched |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )
    return table


def render_account_cp(
    account_slug: str,
    account_display: str,
    projects_in_account: tuple[ProjectState, ...],
) -> str:
    """Render a full account `cp.md` from the empty template.

    Only used on first scaffold of an account dir. Subsequent syncs
    re-splice only the `account-facts` and `projects` engine regions
    via `splice_managed_region` — handwritten content outside those
    regions is byte-stable.
    """
    template = _env().get_template("account-cp.md.j2")
    return template.render(
        account={"slug": account_slug, "display": account_display},
        account_facts_block=render_account_facts_body(
            account_display, projects_in_account
        ),
        projects_block=render_account_projects_body(projects_in_account),
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
    )


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
# processed_ids). Per-machine; not shared via git.
.cp-engine/

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


def render_repo_md(
    project: ProjectState,
    *,
    local_clones_by_user: dict[str, str] | None = None,
) -> str | None:
    """Render `_repo.md` for a repo-source project working directory.

    Mirrors `_dropbox.md`'s role for engagements: a discoverable link
    from inside the working dir to the canonical artifact store. For
    engagements that's Dropbox (binary media); for repos that's GitHub
    (source code).

    When `local_clones_by_user` is non-empty (looked up from
    `.cp-engine.toml` `[local-repos.<user>]`), the rendered output
    surfaces one `**Local clone (User):** <path>` line per user who
    has the repo. Without it, only the GitHub link appears (v0.3.3
    behavior).

    Returns None for engagement-source projects (they get _dropbox.md
    instead) and for repos missing the github_org/repo_name fields
    (defensive — sync_mc2's _repo_row_is_valid blocks these, but we
    double-check at render time).
    """
    if project.source != "repo":
        return None
    if not project.github_org or not project.repo_name:
        return None
    template = _env().get_template("repo.md.j2")
    return template.render(
        project=_project_view(project),
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        local_clones_by_user=local_clones_by_user or None,
    )


def render_linked_repo_md(
    project_name: str,
    repo: LinkedRepo,
    *,
    local_clones_by_user: dict[str, str] | None = None,
) -> str:
    """Render `_repo-<repo-name>.md` for a repo linked to an engagement.

    Engagement-source projects can have multiple linked repos in MC-2
    (`repos.project_id` pointing at the engagement). Each gets its own
    file in the engagement's working dir, mirroring the standalone-repo
    `_repo.md` pattern. The template makes the *linked* relationship
    explicit so a reader doesn't confuse the file with a primary
    standalone-repo working-dir record.
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
#  Agenda rollup
# ──────────────────────────────────────────────────────────────────────


# Match either "by W##" or bare "W##" (case-insensitive on the leading
# `by`). Used to detect horizon target_dates that we can compare against
# the current sprint week numerically.
# NB: The agenda week-target regex moved to cp_engine.aggregators in v0.8.5
# (shared between master-cp.md's `agenda` region and weekly-cp.md's
# `carry-forward-strip`). This file no longer needs it directly.


_SLACK_DIGEST_RE = re.compile(
    r"^- \[(?P<week>\d{4}-W\d{1,2}) · Slack\] (?P<text>.+?)(?: <!-- cp:hash=[a-f0-9]+ -->)?\s*$",
    re.MULTILINE,
)


def _compute_slack_rollup(
    tenant_root: Path,
    active_projects: list[ProjectState],
    current_sprint_iso: str | None,
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
            # Path-building scope (includes account layer for clients) so
            # the master-cp link `<scope>/<dir_slug>/cp.md` resolves.
            "scope": account_scope_for(p),
            "dir_slug": dir_slug(p.code, p.name),
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
    # Delegate the core rollup to cp_engine.aggregators so weekly-cp.md's
    # `carry-forward-strip` (Phase 1.2 / v0.8.5) can reuse the same parser.
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


def _project_view(p: ProjectState) -> dict:
    """Flatten a ProjectState into the keys the templates expect.

    Includes both engagement-shape and repo-shape fields. Templates
    branch on `source` to choose which to render.
    """
    # Account view fields — populated for client projects so the
    # master-cp 1P tables can render the leading Account column.
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
        "source": p.source,
        "company_kind": p.company_kind,
        # Path-building scope (includes account layer for clients).
        # Templates render `{{ p.scope }}/{{ p.dir_slug }}/cp.md` links.
        "scope": account_scope_for(p),
        "dir_slug": dir_slug(p.code, p.name),
        "company_code": p.company_code,
        "company_name": p.company_name,
        "account_slug": account_slug_value,
        "account_display": account_display,
        "account_link": account_link,
        "status": p.status,
        "owner": p.owner,
        "last_touched_short": _short(p.last_touched),
        "one_line_summary": p.one_line_summary,
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
    if config.projects:
        first = config.projects[0]
        return (first.code, first.code.replace("-", " ").title())
    return ("ggl-5168", "Playbooks (Activation)")
