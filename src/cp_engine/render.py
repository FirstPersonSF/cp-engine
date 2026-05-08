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
from datetime import date, datetime
from importlib import resources

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cp_engine import __version__ as ENGINE_VERSION
from cp_engine.config import TenantConfig
from cp_engine.status import is_active_status
from cp_engine.state import Issue, ProjectState, dir_slug, scope_for

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

    def is_closed_recent(p: ProjectState) -> bool:
        if p.last_touched is None:
            return False
        if (datetime.now(p.last_touched.tzinfo) - p.last_touched).days > 30:
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
        is zero (no allocations === no row)."""
        view = _project_view(p)
        view["allocation_line"] = _allocation_line_for(p.code, allocations)
        return view

    def group(active_list: list[ProjectState]) -> dict:
        client_entries = [p for p in active_list if p.company_kind == "client"]
        pipeline = sorted(
            (p for p in client_entries if p.status == "Deal"),
            key=lambda p: (_STAGE_ORDER.get(p.deal_stage or "", 99), p.code),
        )
        client_active = [p for p in client_entries if p.status == "Open"]
        return {
            "pipeline": [to_view(p) for p in pipeline],
            "client": [to_view(p) for p in client_active],
            "self_fpsf": [to_view(p) for p in active_list if p.company_kind == "self-fpsf"],
            "self_canonic": [to_view(p) for p in active_list if p.company_kind == "self-canonic"],
        }

    template = _env().get_template("master-cp.md.j2")
    return template.render(
        tenant=config,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        last_sync_iso=last_sync.isoformat(),
        active_groups=group(active),
        workload_rollup=_rollup_view(allocations),
        workload_week=allocations.week_start if allocations else None,
        holding_projects=[_project_view(p) for p in holding],
        closed_recent=[_project_view(p) for p in closed_recent],
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


def render_project_cp(
    config: TenantConfig,
    project: ProjectState,
    tracked_issues: tuple[Issue, ...] = (),
) -> str:
    """Render a project CP from the empty template.

    Only used on first creation. Subsequent updates touch only the
    engine-managed regions via splice_managed_region.
    """
    template = _env().get_template("project-cp.md.j2")
    return template.render(
        tenant=config,
        project=_project_view(project),
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        tracked_issues=[_issue_view(i) for i in tracked_issues],
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


def render_repo_md(project: ProjectState) -> str | None:
    """Render `_repo.md` for a repo-source project working directory.

    Mirrors `_dropbox.md`'s role for engagements: a discoverable link
    from inside the working dir to the canonical artifact store. For
    engagements that's Dropbox (binary media); for repos that's GitHub
    (source code).

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
    )


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
#  Internal view-model helpers
# ──────────────────────────────────────────────────────────────────────


def _project_view(p: ProjectState) -> dict:
    """Flatten a ProjectState into the keys the templates expect.

    Includes both engagement-shape and repo-shape fields. Templates
    branch on `source` to choose which to render.
    """
    return {
        "code": p.code,
        "name": p.name,
        "source": p.source,
        "company_kind": p.company_kind,
        "scope": scope_for(p.company_kind),
        "dir_slug": dir_slug(p.code, p.name),
        "company_code": p.company_code,
        "company_name": p.company_name,
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
