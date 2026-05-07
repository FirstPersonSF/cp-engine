"""Renderers for tenant CP files.

Jinja2 templates live in `templates/`. Each renderer takes a
TenantConfig + per-template state and produces the file body.

Engine-managed regions are bracketed by HTML comment markers
(`<!-- cp-engine:start <name> -->` / `<!-- cp-engine:end <name> -->`).
The renderer composes a full file body; callers handling existing files
must splice between markers, never overwriting hand-written content.
See spec v02 §4.3.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.config import TenantConfig
from cp_engine.sync import ProjectState


def render_master_cp(config: TenantConfig, projects: tuple[ProjectState, ...]) -> str:
    """Render the full master-cp.md body."""
    raise NotImplementedError("render.render_master_cp lands in v0.1")


def render_weekly_cp(config: TenantConfig) -> str:
    """Render the weekly-cp.md body (skeleton for hand-editing).

    Sync never touches this file; this renderer is used only on first
    creation or on `cp render --force-weekly`.
    """
    raise NotImplementedError("render.render_weekly_cp lands in v0.1")


def render_project_cp(config: TenantConfig, project: ProjectState) -> str:
    """Render a project CP from the empty template.

    Only used on first creation. Subsequent updates touch only the
    engine-managed regions via splice_managed_region.
    """
    raise NotImplementedError("render.render_project_cp lands in v0.1")


def render_claude_md(config: TenantConfig) -> str:
    """Render CLAUDE.md from the tenant config.

    Encodes the four loading modes, gatekeeper rule, status vocabulary
    on the read side, and trigger-phrase reference. Fully generated —
    tenants never hand-edit (spec v02 §2.5).
    """
    raise NotImplementedError("render.render_claude_md lands in v0.1")


def splice_managed_region(file_path: Path, region_name: str, new_body: str) -> str:
    """Read `file_path`, replace the body between
    `<!-- cp-engine:start <region_name> -->` and the matching end marker,
    return the spliced body. Hand-written content outside markers is
    preserved verbatim.

    Raises if markers are missing or unbalanced — silent skip is the
    failure mode this prevents.
    """
    raise NotImplementedError("render.splice_managed_region lands in v0.1")
