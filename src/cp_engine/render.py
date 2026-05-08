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
from cp_engine.sync import Issue, ProjectState

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
) -> str:
    """Render the full master-cp.md body."""
    active = [p for p in projects if is_active_status(p.status) and not p.is_internal]
    holding = [p for p in projects if p.status == "Holding" and not p.is_internal]
    closed_recent = [
        p
        for p in projects
        if p.status == "Closed"
        and not p.is_internal
        and p.last_touched is not None
        and (datetime.now(p.last_touched.tzinfo) - p.last_touched).days <= 30
    ]

    template = _env().get_template("master-cp.md.j2")
    return template.render(
        tenant=config,
        engine_version=ENGINE_VERSION,
        today=_today_iso(),
        last_sync_iso=last_sync.isoformat(),
        active_projects=[_project_view(p) for p in active],
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
    # and exactly one newline between new_body and end_marker.
    body = new_body.strip("\n")
    return f"{before}\n{body}\n{after}"


# ──────────────────────────────────────────────────────────────────────
#  Internal view-model helpers
# ──────────────────────────────────────────────────────────────────────


def _project_view(p: ProjectState) -> dict:
    """Flatten a ProjectState into the keys the templates expect."""
    return {
        "code": p.code,
        "name": p.name,
        "status": p.status,
        "owner": p.owner,
        "last_touched_short": _short(p.last_touched),
        "one_line_summary": p.one_line_summary,
    }


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
