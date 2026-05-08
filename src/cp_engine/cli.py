"""The `cp` CLI — entry point for tenant operations.

Subcommands per spec v02 §2.1:
  cp init     → walk through populating .cp-engine.local.toml
  cp sync     → run one sync cycle locally (same logic as the Action)
  cp render   → re-render generated files (master-cp.md, CLAUDE.md)
  cp status   → show what would change without writing
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cp_engine.config import ConfigError, load
from cp_engine.config import CommittedConfigMissing
from cp_engine.init import InitAborted, run_init
from cp_engine.sync import SyncError, sync_tenant


@click.group()
@click.version_option(package_name="cp-engine")
def main() -> None:
    """Context Protocol Engine — tenant operations."""


@main.command()
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


@main.command()
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
        click.echo(f"  wrote {path.relative_to(config.root)}")


@main.command()
def render() -> None:
    """Re-render generated files (alias for `cp sync` in v0.1)."""
    # In v0.1 there's no separate render-only path — sync already writes
    # only when content changed and never reaches out to the database
    # for unchanged regions. Future: a true `--no-network` render that
    # uses cached project state.
    click.echo("`cp render` is currently an alias for `cp sync`. Running sync…")
    ctx = click.get_current_context()
    ctx.invoke(sync)


@main.command()
def status() -> None:
    """Show what would change on next sync (no writes — read-only)."""
    click.echo("cp status — not implemented yet (lands in v0.2)")
    sys.exit(1)


if __name__ == "__main__":
    main()
