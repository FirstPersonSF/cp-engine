"""Core tenant verbs: init, sync, render, status, brief, refresh-pristine, resolve-engine-pin, write-region, mcp.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cp_engine.config import CommittedConfigMissing, ConfigError, load
from cp_engine.init import InitAborted, run_init
from cp_engine.sync import SyncError, sync_tenant


@click.command()
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Don't prompt; mark every committed project as skipped (useful in CI).",
)
def init(non_interactive: bool) -> None:
    """Walk through populating .cp-engine.local.toml interactively."""
    try:
        run_init(Path.cwd(), interactive=not non_interactive)
    except CommittedConfigMissing as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    except InitAborted as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nAborted. Partial progress (if any) has been saved.", err=True)
        sys.exit(130)


@click.command()
def sync() -> None:
    """Run one sync cycle for the current tenant."""
    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    try:
        result = sync_tenant(config)
    except SyncError as exc:
        click.echo(f"Sync failed: {exc}", err=True)
        sys.exit(1)

    if result.no_op:
        click.echo(f"No changes ({result.projects_seen} projects checked).")
        return

    click.echo(f"Synced {result.projects_seen} projects.")
    for path in result.files_written:
        click.echo(f"  wrote    {path.relative_to(config.root)}")
    for path in result.files_deactivated:
        click.echo(f"  deactivated {path.relative_to(config.root)}")


@click.command()
def render() -> None:
    """Re-render generated files (alias for `cp sync` in v0.1)."""
    # In v0.1 there's no separate render-only path — sync already writes
    # only when content changed and never reaches out to the database
    # for unchanged regions. Future: a true `--no-network` render that
    # uses cached project state.
    click.echo("`cp render` is currently an alias for `cp sync`. Running sync…")
    ctx = click.get_current_context()
    ctx.invoke(sync)

    # Word-count discipline: warn-only Exec Summary per-field budget pass over
    # every live project cp.md. Best-effort — findings are echoed and NEVER
    # affect the exit code; any failure here degrades silently (sync already
    # ran).
    try:
        config = load(Path.cwd())
        for warning in _exec_summary_warnings(config.root):
            click.echo(warning, err=True)
    except Exception:  # noqa: BLE001 — advisory pass, never fail a render
        pass


def _exec_summary_warnings(root: Path) -> list[str]:
    """Exec-summary budget warnings across the tenant's live project CPs.

    Scans every `cp.md` below `root` (skipping `inactive/` dirs and the
    tenant's own top-level files), prefixing each finding with its project
    dir so tenant-wide output stays attributable. Pure read — never edits.
    """
    out: list[str] = []
    from cp_engine.exec_summary_lint import lint_exec_summary

    for cp_md in sorted(root.rglob("cp.md")):
        rel = cp_md.relative_to(root)
        if "inactive" in rel.parts or len(rel.parts) < 2:
            continue
        try:
            findings = lint_exec_summary(cp_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        out.extend(f"{rel.parent}: {w}" for w in findings)
    return out


@click.command("brief")
@click.argument("code")
def brief_cmd(code: str) -> None:
    """Print the composed Mode-2 context pack for one project (arch-review §3).

    Five sections to stdout, markdown, deterministic: Facts (cp.md engine
    region) · Exec Summary TRIMMED (Status/Next up/Blockers verbatim, Where
    it stands capped at 5 bullets, Updates dropped) · the standing Inputs &
    Briefing spine element's live body (MC-2) · open commitments · the
    Last-session pointer. Every section degrades to a one-line absence note
    on its own — a standalone repo (no spine, no commitments store) or an
    offline session still gets the pack. Exits non-zero only when the code
    resolves to no working dir AND MC-2 can't see it either.
    """
    from cp_engine import mc2_db
    from cp_engine.brief import (
        compose_brief,
        fetch_briefing_body,
        fetch_open_commitments,
        newest_session_capture,
    )
    from cp_engine.spine import SpineDirNotFound, find_spine_dir

    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    # Offline half: the working dir + cp.md (Facts, trimmed Exec Summary,
    # Last-session line, newest sessions/ capture).
    working_dir: Path | None
    cp_md_text: str | None = None
    try:
        working_dir = find_spine_dir(config.root, code)
        cp_md = working_dir / "cp.md"
        if cp_md.is_file():
            cp_md_text = cp_md.read_text(encoding="utf-8")
    except (SpineDirNotFound, OSError):
        working_dir = None

    # MC-2 half — best-effort throughout: a missing client or a failed read
    # degrades the section to its absence note, never the command.
    client = mc2_db.get_client(config, required=False)
    try:
        briefing_body, briefing_note = fetch_briefing_body(
            client, code,
            alt_code=working_dir.name if working_dir is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 — section degrades, pack survives
        briefing_body, briefing_note = None, f"Inputs & Briefing read failed: {exc}"
    try:
        commitments, commitments_note = fetch_open_commitments(client, code)
    except Exception as exc:  # noqa: BLE001 — section degrades, pack survives
        commitments, commitments_note = None, f"Commitments read failed: {exc}"

    if working_dir is None and briefing_body is None and commitments is None:
        click.echo(
            f"'{code}' resolves to no working dir, and MC-2 has no spine or "
            "commitments for it — nothing to brief.",
            err=True,
        )
        sys.exit(1)

    click.echo(
        compose_brief(
            code,
            cp_md_text,
            briefing_body,
            briefing_note,
            commitments,
            commitments_note,
            newest_session_capture(working_dir),
        ),
        nl=False,
    )


@click.command(name="refresh-pristine")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be refreshed without writing.",
)
def refresh_pristine(dry_run: bool) -> None:
    """Re-scaffold project CPs that have no hand-written content.

    A project CP is "pristine" if it still contains the placeholder text
    that the scaffold inserted ("<one-line description of what this
    project is and who it's for>"). Pristine CPs were generated by an
    older engine version and have outdated provenance, missing
    metadata blocks, or wrong section structures (e.g. v0.1 templates
    didn't have project-facts; v0.2 templates do).

    Edited CPs (the placeholder is gone or any other section was filled
    in) are NEVER touched — that's hand-written content the engine has
    no business overwriting.

    Use this once after upgrading cp-engine versions to refresh the
    template shape across the tenant.
    """
    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    from cp_engine.refresh import refresh_pristine_cps

    try:
        result = refresh_pristine_cps(config, dry_run=dry_run)
    except SyncError as exc:
        click.echo(f"Refresh failed: {exc}", err=True)
        sys.exit(1)

    if not result.pristine_files:
        click.echo("No pristine project CPs found. Nothing to refresh.")
        return

    if dry_run:
        click.echo(f"Would refresh {len(result.pristine_files)} pristine project CP(s):")
    else:
        click.echo(f"Refreshed {len(result.pristine_files)} pristine project CP(s):")
    for path in result.pristine_files:
        click.echo(f"  {path.relative_to(config.root)}")
    if result.skipped_files:
        click.echo(f"\nSkipped {len(result.skipped_files)} edited CP(s) (hand-written content preserved).")


@click.command()
def status() -> None:
    """Show what would change on next sync (no writes — read-only).

    Runs a dry-run sync and reports the high-signal files that would be
    created or updated (master-cp.md, CLAUDE.md, .gitignore, new project CPs)
    without touching the tenant. For the full set of writes (sprint files,
    account CPs, deactivations) run `cp sync` — it's idempotent.
    """
    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    try:
        result = sync_tenant(config, dry_run=True)
    except SyncError as exc:
        click.echo(f"Status check failed: {exc}", err=True)
        sys.exit(1)

    if result.no_op:
        click.echo(
            f"Up to date ({result.projects_seen} projects checked) — "
            "next sync would make no changes."
        )
        return

    click.echo(
        f"{len(result.files_written)} file(s) would change "
        f"({result.projects_seen} projects checked). Run `cp sync` to apply:"
    )
    for path in result.files_written:
        click.echo(f"  would write    {path.relative_to(config.root)}")


@click.command(name="resolve-engine-pin")
@click.option(
    "--tenant-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Tenant root (default: current directory).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["tag", "pip-spec", "json"]),
    default="tag",
    help="Output format. tag=v0.6.0; pip-spec=full git+url@tag; json=structured.",
)
def resolve_engine_pin_cmd(tenant_root: Path | None, output_format: str) -> None:
    """Resolve [engine].version constraint to the highest matching git tag.

    Used by the GitHub Actions runner so bumping the tenant pin is
    sufficient to pick up a new engine release — no workflow edit needed.
    """
    from cp_engine.pin_resolver import ENGINE_REPO_URL, PinResolutionError, resolve_for_tenant

    root = tenant_root or Path.cwd()
    try:
        resolution = resolve_for_tenant(root)
    except PinResolutionError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    if output_format == "tag":
        click.echo(resolution.tag)
    elif output_format == "pip-spec":
        click.echo(f"cp-engine @ git+{ENGINE_REPO_URL}@{resolution.tag}")
    else:
        import json

        click.echo(
            json.dumps(
                {
                    "constraint": resolution.constraint,
                    "tag": resolution.tag,
                    "version": str(resolution.version),
                }
            )
        )


@click.command("write-region")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("region")
@click.option("--body", help="New body content for the region.")
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read body from a file instead.",
)
def write_region_cmd(
    file: Path, region: str, body: str | None, body_file: Path | None
) -> None:
    """Splice content into an engine-managed region (escape hatch).

    Usage:
      cp write-region 1p/ggl-5168-activation/cp.md inbound-strip --body-file new.md

    Logs a warning so this is visible when used. Routine writes should
    go through `cp ingest` or its structured verbs, not through this.
    """
    import logging

    from cp_engine.render import splice_managed_region

    if body is None and body_file is None:
        click.echo("Provide --body or --body-file.", err=True)
        sys.exit(1)
    new_body = body if body is not None else body_file.read_text(encoding="utf-8")

    logger = logging.getLogger("cp_engine.cli")
    logger.warning(
        "write-region called directly on %s region %r — routine writes "
        "should go through `cp ingest`. Using this is fine but visible.",
        file, region,
    )

    existing = file.read_text(encoding="utf-8")
    updated = splice_managed_region(existing, region, new_body)
    if updated != existing:
        file.write_text(updated)
        click.echo(f"wrote {file}")
    else:
        click.echo("no change")


@click.command("mcp")
def mcp_cmd() -> None:
    """Run the local stdio MCP server exposing this tenant's project sources."""
    from cp_engine.mcp_server import run_stdio

    run_stdio()


